import os
import json
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


async def openai_answer(user_text: str) -> str:
    # Без истории: один prompt -> один ответ
    if not OPENAI_API_KEY:
        return "OPENAI_API_KEY не задан в переменных окружения."

    # Универсальный вызов через Chat Completions (подходит большинству аккаунтов)
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}

    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "Ты полезный ассистент. Отвечай по делу."},
            {"role": "user", "content": user_text},
        ],
        "temperature": 0.7,
        "max_tokens": 500,
    }

    async with httpx.AsyncClient(timeout=40) as client:
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
    text = message.get("text", "")

    if not chat_id or not text:
        return {"ok": True}

    # Команды
    if text.startswith("/start"):
        await tg_send_message(chat_id, "Привет! Напиши сообщение — отвечу через GPT.")
        return {"ok": True}

    # GPT ответ
    answer = await openai_answer(text)
    await tg_send_message(chat_id, answer)
    return {"ok": True}
