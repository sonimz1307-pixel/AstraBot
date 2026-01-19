import os
import base64
from typing import Optional

import httpx
from fastapi import FastAPI, Request, Response

app = FastAPI()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change_me")

# Размер картинки по умолчанию для афиш (можно поменять в Render Environment)
# Поддерживаемые значения зависят от модели; часто работают: 1024x1024, 1024x1536, 1536x1024
IMG_SIZE_DEFAULT = os.getenv("IMG_SIZE_DEFAULT", "1024x1536")

TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


@app.get("/health")
def health():
    return {"status": "ok"}


async def tg_send_message(chat_id: int, text: str):
    if not TELEGRAM_BOT_TOKEN:
        return
    async with httpx.AsyncClient(timeout=20) as client:
        await client.post(
            f"{TELEGRAM_API_BASE}/sendMessage",
            json={"chat_id": chat_id, "text": text},
        )


async def tg_send_photo_bytes(chat_id: int, image_bytes: bytes, caption: Optional[str] = None):
    if not TELEGRAM_BOT_TOKEN:
        return
    files = {"photo": ("image.png", image_bytes, "image/png")}
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption

    async with httpx.AsyncClient(timeout=60) as client:
        await client.post(f"{TELEGRAM_API_BASE}/sendPhoto", data=data, files=files)


async def tg_get_file_path(file_id: str) -> str:
    """Получить file_path по file_id через getFile."""
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"{TELEGRAM_API_BASE}/getFile", params={"file_id": file_id})
    r.raise_for_status()
    data = r.json()
    return data["result"]["file_path"]


async def tg_download_file_bytes(file_path: str) -> bytes:
    """Скачать файл по file_path с серверов Telegram и вернуть байты."""
    url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(url)
    r.raise_for_status()
    return r.content


# Вариант 2: Unicode-математика для Telegram (без LaTeX)
UNICODE_MATH_SYSTEM_PROMPT = (
    "Ты решаешь математические задачи для Telegram.\n"
    "НЕ используй LaTeX/TeX и команды вида \\frac, \\pi, \\[ \\], \\( \\), \\mathbb и т.п.\n"
    "Пиши только обычным текстом и Unicode-символами.\n\n"
    "Используй символы: π, ℤ, ⇒, −, ×, ÷, ≤, ≥, ∈.\n"
    "Формулы пиши в одну строку, чтобы в Telegram всё читалось.\n"
    "Примеры:\n"
    "x = −2π/3 + 4πk, k ∈ ℤ\n"
    "cos(x/2 + π/3) = 1 ⇒ x/2 + π/3 = 2πk\n\n"
    "Оформление ответа:\n"
    "1) Коротко: что делаем\n"
    "2) Решение по шагам\n"
    "3) В конце отдельной строкой: 'Ответ: ...'\n\n"
    "Если текст на фото плохо читается — попроси прислать фото ближе и ровнее."
)


async def openai_text_answer(user_text: str, image_bytes: Optional[bytes] = None) -> str:
    """
    Текстовый ответ (в т.ч. анализ фото задач) через Chat Completions.
    Без истории: один запрос -> один ответ.
    """
    if not OPENAI_API_KEY:
        return "OPENAI_API_KEY не задан в переменных окружения."

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    if image_bytes is not None:
        # Telegram фото -> base64 data URL -> multimodal content
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        user_content = []
        if user_text:
            user_content.append({"type": "text", "text": user_text})
        user_content.append(
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
        )

        payload = {
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": UNICODE_MATH_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.3,
            "max_tokens": 900,
        }
    else:
        payload = {
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": UNICODE_MATH_SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
            ],
            "temperature": 0.5,
            "max_tokens": 700,
        }

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json=payload,
        )

    if r.status_code != 200:
        return f"Ошибка OpenAI ({r.status_code}): {r.text[:1000]}"

    data = r.json()
    return (data["choices"][0]["message"]["content"] or "").strip() or "Пустой ответ от модели."


async def openai_generate_image(prompt: str, size: str = "1024x1536") -> bytes:
    """
    Генерация изображения через OpenAI Images API (b64_json).
    Возвращает байты PNG/JPEG (в зависимости от модели/настроек; отправим как PNG).
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY не задан в переменных окружения.")

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": "gpt-image-1",
        "prompt": prompt,
        "size": size,
        "n": 1,
    }

    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            "https://api.openai.com/v1/images/generations",
            headers=headers,
            json=payload,
        )

    if r.status_code != 200:
        # Текст ошибки наружу — полезно для дебага лимитов/доступа
        raise RuntimeError(f"Ошибка Images API ({r.status_code}): {r.text[:1200]}")

    data = r.json()
    b64_img = data["data"][0].get("b64_json")
    if not b64_img:
        raise RuntimeError("Images API вернул ответ без b64_json.")
    return base64.b64decode(b64_img)


def _extract_img_prompt(text: str) -> Optional[str]:
    """
    Поддерживаем команды:
      /img <prompt>
      /image <prompt>
    """
    t = (text or "").strip()
    for cmd in ("/img", "/image"):
        if t == cmd:
            return ""
        if t.startswith(cmd + " "):
            return t[len(cmd) + 1 :].strip()
    return None


@app.post("/webhook/{secret}")
async def webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        return Response(status_code=403)

    update = await request.json()
    message = update.get("message") or update.get("edited_message")
    if not message:
        return {"ok": True}

    chat = message.get("chat", {})
    chat_id = chat.get("id")
    text = (message.get("text") or "").strip()

    if not chat_id:
        return {"ok": True}

    # /start
    if text.startswith("/start"):
        await tg_send_message(
            chat_id,
            "Привет!\n"
            "• Текст — отвечу через GPT.\n"
            "• Фото с задачей — решу и оформлю математику читабельно.\n"
            "• Картинка: /img <описание> (например: /img зимняя яркая афиша, оранжевый цвет, цена 1000₽).",
        )
        return {"ok": True}

    # Генерация картинок: /img ...
    img_prompt = _extract_img_prompt(text)
    if img_prompt is not None:
        if not img_prompt:
            await tg_send_message(
                chat_id,
                "Напиши описание после команды.\n"
                "Пример: /img зимняя яркая афиша для магазина, оранжевый стиль, текст: «Одноразка за 1000»",
            )
            return {"ok": True}

        await tg_send_message(chat_id, "Генерирую картинку...")

        # Небольшое усиление промпта под афишу/соцсети (по желанию можно убрать)
        prompt = (
            "Сгенерируй изображение по описанию пользователя.\n"
            "Требования: высокое качество, выразительная композиция, читаемый дизайн.\n"
            "Описание пользователя:\n"
            f"{img_prompt}"
        )

        try:
            image_bytes = await openai_generate_image(prompt=prompt, size=IMG_SIZE_DEFAULT)
            await tg_send_photo_bytes(chat_id, image_bytes, caption="Готово.")
        except Exception as e:
            await tg_send_message(chat_id, f"Не получилось сгенерировать изображение: {e}")
        return {"ok": True}

    # Фото-задача (или любая картинка) -> текстовый разбор
    photos = message.get("photo") or []
    if photos:
        largest = photos[-1]  # обычно самый большой
        file_id = largest.get("file_id")

        if not file_id:
            await tg_send_message(chat_id, "Фото получил, но не смог прочитать file_id. Попробуй отправить ещё раз.")
            return {"ok": True}

        await tg_send_message(chat_id, "Фото получил. Разбираю...")

        try:
            file_path = await tg_get_file_path(file_id)
            img_bytes = await tg_download_file_bytes(file_path)
            prompt = (
                "Реши задачу с картинки. Пиши без LaTeX, используй Unicode-математику. "
                "Дай решение по шагам и строку 'Ответ: ...'."
            )
            answer = await openai_text_answer(prompt, image_bytes=img_bytes)
        except Exception as e:
            await tg_send_message(chat_id, f"Ошибка при обработке фото: {e}")
            return {"ok": True}

        await tg_send_message(chat_id, answer)
        return {"ok": True}

    # Обычный текст
    if text:
        answer = await openai_text_answer(text)
        await tg_send_message(chat_id, answer)
        return {"ok": True}

    # Прочее (стикер/голос и т.п.)
    await tg_send_message(chat_id, "Я понимаю текст, фото задач и команду /img для генерации картинок.")
    return {"ok": True}
