"""
Shared OpenAI chat helper for AstraBot.

Moved out of main.py to:
- keep main.py slim
- avoid circular imports when using FastAPI routers

Exports:
    async def openai_chat_answer(user_text, system_prompt, image_bytes=None, temperature=0.5, max_tokens=800, history=None, model="gpt-4o-mini") -> str
"""

from __future__ import annotations

import base64
import os
from typing import Any, Dict, List, Optional, Tuple

import httpx


OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
PROMPT_BUILDER_MAX_IMAGES = 4


def _detect_image_type(b: bytes) -> Tuple[str, str]:
    if not b:
        return ("jpg", "image/jpeg")
    if b.startswith(b"\xFF\xD8\xFF"):
        return ("jpg", "image/jpeg")
    if b.startswith(b"\x89PNG\r\n\x1a\n"):
        return ("png", "image/png")
    if b.startswith(b"RIFF") and len(b) >= 12 and b[8:12] == b"WEBP":
        return ("webp", "image/webp")
    return ("jpg", "image/jpeg")


async def openai_chat_answer(
    user_text: str,
    system_prompt: str,
    image_bytes: Optional[bytes] = None,
    temperature: float = 0.5,
    max_tokens: int = 800,
    history: Optional[List[Dict[str, str]]] = None,
    model: str = "gpt-4o-mini",
    image_bytes_list: Optional[List[bytes]] = None,
) -> str:
    if not OPENAI_API_KEY:
        return "OPENAI_API_KEY не задан в переменных окружения."

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    images_to_send: List[bytes] = []
    if image_bytes_list:
        images_to_send.extend([bytes(b) for b in image_bytes_list if isinstance(b, (bytes, bytearray))])
    elif image_bytes is not None:
        images_to_send.append(image_bytes)

    if images_to_send:
        user_content: List[Dict[str, Any]] = []
        if user_text:
            user_content.append({"type": "text", "text": user_text})
        for img in images_to_send[:PROMPT_BUILDER_MAX_IMAGES]:
            _ext, mime = _detect_image_type(img)
            b64 = base64.b64encode(img).decode("utf-8")
            user_content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            })

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": temperature,
            "max_completion_tokens": max_tokens,
        }
    else:
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
            "model": model,
            "messages": msgs,
            "temperature": temperature,
            "max_completion_tokens": max_tokens,
        }

    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)

    if r.status_code != 200:
        return f"Ошибка OpenAI ({r.status_code}): {r.text[:1600]}"

    data = r.json()
    return (data["choices"][0]["message"]["content"] or "").strip() or "Пустой ответ от модели."
