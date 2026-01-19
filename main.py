import os
import base64
from typing import Optional

import httpx
from fastapi import FastAPI, Request, Response

app = FastAPI()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change_me")

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
    async with httpx.AsyncClient(timeout=40) as client:
        r = await client.get(url)
    r.raise_for_status()
    return r.content


# Вариант 2: Unicode-математика для Telegram (без LaTeX)
UNICODE_MATH_SYSTEM_PROMPT = (
    "Ты решаешь математические задачи для Telegram.\n"
    "НЕ используй LaTeX/TeX и команды вида \\frac, \\pi, \\[ \\], \\], \\( \\), \\mathbb и т.п.\n"
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


async def openai_answer(user_text: str, image_bytes: Optional[bytes] = None) -> str:
    # Без истории: один prompt -> один ответ
    if not OPENAI_API_KEY:
        return "OPENAI_API_KEY не задан в переменных окружения."

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    # Если есть картинка — multimodal content (text + image_url data:...base64)
    if image_bytes is not None:
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

    # Команды
    if text.startswith("/start"):
        await tg_send_message(
            chat_id,
            "Привет! Можешь написать текст или прислать фото с задачей. Я отвечу и оформлю математику так, чтобы было читабельно в Telegram.",
        )
        return {"ok": True}

    # Фото
    photos = message.get("photo") or []
    if photos:
        largest = photos[-1]  # обычно самый большой
        file_id = largest.get("file_id")

        if not file_id:
            await tg_send_message(chat_id, "Фото получил, но не смог прочитать file_id. Попробуй отправить ещё раз.")
            return {"ok": True}

        await tg_send_message(chat_id, "Фото получил. Разбираю задачу...")

        try:
            file_path = await tg_get_file_path(file_id)
            img_bytes = await tg_download_file_bytes(file_path)
            prompt = "Реши задачу с картинки. Пиши без LaTeX, используй Unicode-математику. Дай решение по шагам и строку 'Ответ: ...'."
            answer = await openai_answer(prompt, image_bytes=img_bytes)
        except Exception as e:
            await tg_send_message(chat_id, f"Ошибка при обработке фото: {e}")
            return {"ok": True}

        await tg_send_message(chat_id, answer)
        return {"ok": True}

    # Текст
    if text:
        answer = await openai_answer(text)
        await tg_send_message(chat_id, answer)
        return {"ok": True}

    # Прочее
    await tg_send_message(chat_id, "Я понимаю текст и фото. Пришли задачу текстом или фото.")
    return {"ok": True}
