"""
Shared OpenAI chat helper for AstraBot.

Moved out of main.py to:
- keep main.py slim
- avoid circular imports when using FastAPI routers

Exports:
    async def openai_chat_answer(user_text, system_prompt, image_bytes=None, temperature=0.5, max_tokens=800, history=None) -> str
"""

from __future__ import annotations

import os
import base64
from typing import Any, Dict, List, Optional

import httpx


OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")


async def openai_chat_answer(
    user_text: str,
    system_prompt: str,
    image_bytes: Optional[bytes] = None,
    temperature: float = 0.5,
    max_tokens: int = 800,
    history: Optional[List[Dict[str, str]]] = None,
) -> str:
    if not OPENAI_API_KEY:
        return "OPENAI_API_KEY не задан в переменных окружения."

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}

    # IMAGE + TEXT (Vision)
    if image_bytes is not None:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        user_content: List[Dict[str, Any]] = []
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
        # TEXT ONLY (Chat) + optional history
        msgs: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
        if history:
            for m in history:
                if (
                    isinstance(m, dict)
                    and m.get("role") in ("system", "user", "assistant")
                    and isinstance(m.get("content"), str)
                    and m["content"].strip()
                ):
                    msgs.append({"role": m["role"], "content": m["content"]})

        msgs.append({"role": "user", "content": user_text})

        payload = {
            "model": "gpt-4o-mini",
            "messages": msgs,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)

    if r.status_code != 200:
        return f"Ошибка OpenAI ({r.status_code}): {r.text[:1600]}"

    data = r.json()
    return (data["choices"][0]["message"]["content"] or "").strip() or "Пустой ответ от модели."
