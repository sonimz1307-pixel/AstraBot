import os
import base64
from typing import Optional, Literal

import httpx
from fastapi import FastAPI, Request, Response

app = FastAPI()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change_me")

# Для афиш удобнее вертикальный формат; при проблемах поставьте 1024x1024 в Render Environment
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

    async with httpx.AsyncClient(timeout=90) as client:
        await client.post(f"{TELEGRAM_API_BASE}/sendPhoto", data=data, files=files)


async def tg_get_file_path(file_id: str) -> str:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"{TELEGRAM_API_BASE}/getFile", params={"file_id": file_id})
    r.raise_for_status()
    data = r.json()
    return data["result"]["file_path"]


async def tg_download_file_bytes(file_path: str) -> bytes:
    url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.get(url)
    r.raise_for_status()
    return r.content


# ------------- PROMPTS -------------

# Вариант 2: Unicode-математика (без LaTeX)
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

# Универсальный vision-помощник (машина/цветок/товар/объект)
VISION_GENERAL_SYSTEM_PROMPT = (
    "Ты анализируешь изображения для Telegram.\n"
    "Если пользователь просит определить объект (машина, цветок, товар и т.д.) — опиши, что на фото, "
    "и предложи наиболее вероятные варианты идентификации.\n"
    "Если уверенность низкая — честно скажи и попроси уточняющие детали (доп. фото, угол, шильдик, листья, VIN, упаковка).\n"
    "НЕ используй LaTeX/TeX.\n"
    "Отвечай кратко, структурировано.\n\n"
    "Формат:\n"
    "1) Что на фото\n"
    "2) Возможная идентификация (1–3 варианта)\n"
    "3) Что нужно, чтобы уточнить (если нужно)"
)

# Если пользователь ничего не написал к фото — делаем “описание + попытку идентификации”
VISION_DEFAULT_USER_PROMPT = (
    "Опиши, что на фото. Если это объект (машина/цветок/товар), попытайся определить что это. "
    "Если по фото нельзя уверенно определить — скажи, что нужно для уточнения."
)


# ------------- OPENAI CALLS -------------

async def openai_chat_answer(
    user_text: str,
    system_prompt: str,
    image_bytes: Optional[bytes] = None,
    temperature: float = 0.4,
    max_tokens: int = 900,
) -> str:
    if not OPENAI_API_KEY:
        return "OPENAI_API_KEY не задан в переменных окружения."

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}

    if image_bytes is not None:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        user_content = []
        if user_text:
            user_content.append({"type": "text", "text": user_text})
        user_content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})

        payload = {
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
    else:
        payload = {
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)

    if r.status_code != 200:
        return f"Ошибка OpenAI ({r.status_code}): {r.text[:1200]}"

    data = r.json()
    return (data["choices"][0]["message"]["content"] or "").strip() or "Пустой ответ от модели."


async def openai_generate_image(prompt: str, size: str) -> bytes:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY не задан в переменных окружения.")

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": "gpt-image-1", "prompt": prompt, "size": size, "n": 1}

    async with httpx.AsyncClient(timeout=180) as client:
        r = await client.post("https://api.openai.com/v1/images/generations", headers=headers, json=payload)

    if r.status_code != 200:
        raise RuntimeError(f"Ошибка Images API ({r.status_code}): {r.text[:1600]}")

    data = r.json()
    b64_img = data["data"][0].get("b64_json")
    if not b64_img:
        raise RuntimeError("Images API вернул ответ без b64_json.")
    return base64.b64decode(b64_img)


# ------------- ROUTING / INTENT -------------

def _extract_img_prompt(text: str) -> Optional[str]:
    t = (text or "").strip()
    for cmd in ("/img", "/image"):
        if t == cmd:
            return ""
        if t.startswith(cmd + " "):
            return t[len(cmd) + 1 :].strip()
    return None


Intent = Literal["math", "identify", "general"]


def _infer_intent_from_text(text: str) -> Intent:
    """
    Очень простая эвристика:
      - если явные слова про решение задач/уравнений -> math
      - если "что за/какая модель/определи/что это" -> identify
      - иначе general
    """
    t = (text or "").strip().lower()
    if not t:
        return "identify"

    math_markers = [
        "реши", "решить", "задач", "уравнен", "найди", "вычисл", "докажи",
        "sin", "cos", "tg", "ctg", "лог", "ln", "π", "пи", "интеграл", "производн",
    ]
    identify_markers = [
        "что за", "что это", "определи", "какая модель", "модель", "марка",
        "какой цветок", "что за цветок", "что за машина", "что за авто",
        "что за товар", "что за устройство", "что на фото", "что изображено",
    ]

    if any(m in t for m in math_markers):
        return "math"
    if any(m in t for m in identify_markers):
        return "identify"
    return "general"


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
            "• Пиши текст — отвечу через GPT.\n"
            "• Пришли фото и напиши, что сделать (например: «что за машина?» / «что за цветок?» / «реши задачу»).\n"
            "• Генерация картинок: /img <описание> (например: /img яркая зимняя афиша, оранжевый стиль, «Одноразка за 1000»).",
        )
        return {"ok": True}

    # Команда генерации изображений /img
    img_prompt = _extract_img_prompt(text)
    if img_prompt is not None:
        if not img_prompt:
            await tg_send_message(
                chat_id,
                "Напиши описание после команды.\n"
                "Пример: /img яркая зимняя афиша, оранжевый стиль, текст «Одноразка за 1000», цена 1000₽",
            )
            return {"ok": True}

        await tg_send_message(chat_id, "Генерирую картинку...")

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

    # Фото (с текстом или без)
    photos = message.get("photo") or []
    if photos:
        largest = photos[-1]
        file_id = largest.get("file_id")
        if not file_id:
            await tg_send_message(chat_id, "Фото получил, но не смог прочитать file_id. Попробуй отправить ещё раз.")
            return {"ok": True}

        await tg_send_message(chat_id, "Фото получил. Анализирую...")

        try:
            file_path = await tg_get_file_path(file_id)
            img_bytes = await tg_download_file_bytes(file_path)

            intent = _infer_intent_from_text(text)

            if intent == "math":
                # Математика по фото
                prompt = text if text else "Реши задачу с картинки. Дай решение по шагам и строку 'Ответ: ...'."
                answer = await openai_chat_answer(
                    user_text=prompt,
                    system_prompt=UNICODE_MATH_SYSTEM_PROMPT,
                    image_bytes=img_bytes,
                    temperature=0.3,
                    max_tokens=900,
                )
            else:
                # Определение/описание объекта по фото
                prompt = text if text else VISION_DEFAULT_USER_PROMPT
                answer = await openai_chat_answer(
                    user_text=prompt,
                    system_prompt=VISION_GENERAL_SYSTEM_PROMPT,
                    image_bytes=img_bytes,
                    temperature=0.4,
                    max_tokens=700,
                )

        except Exception as e:
            await tg_send_message(chat_id, f"Ошибка при обработке фото: {e}")
            return {"ok": True}

        await tg_send_message(chat_id, answer)
        return {"ok": True}

    # Обычный текст без фото
    if text:
        # Универсально: отвечаем как обычный ассистент (без LaTeX)
        answer = await openai_chat_answer(
            user_text=text,
            system_prompt=(
                "Ты полезный ассистент для Telegram. Не используй LaTeX/TeX. "
                "Если нужна математика — пиши формулы обычным текстом, можно с символом π."
            ),
            image_bytes=None,
            temperature=0.6,
            max_tokens=700,
        )
        await tg_send_message(chat_id, answer)
        return {"ok": True}

    # Прочее
    await tg_send_message(chat_id, "Я понимаю текст, фото и команду /img для генерации картинок.")
    return {"ok": True}
