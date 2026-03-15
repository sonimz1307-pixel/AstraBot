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
MAX_VISION_IMAGES = 4


def _detect_image_type(data: bytes) -> Tuple[str, str]:
    if not data:
        return ("jpg", "image/jpeg")
    if data.startswith(b"\xFF\xD8\xFF"):
        return ("jpg", "image/jpeg")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ("png", "image/png")
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        return ("webp", "image/webp")
    return ("jpg", "image/jpeg")


def _supports_temperature(model: str) -> bool:
    name = str(model or "").strip().lower()
    if not name:
        return True
    blocked_prefixes = ("gpt-5", "o1", "o3", "o4")
    return not any(name.startswith(prefix) for prefix in blocked_prefixes)


def _extract_text_content(message_content: Any) -> str:
    if isinstance(message_content, str):
        return message_content.strip()
    if isinstance(message_content, list):
        parts: List[str] = []
        for item in message_content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = str(item.get("text") or "").strip()
                if text:
                    parts.append(text)
        return "\n".join(parts).strip()
    return str(message_content or "").strip()


async def openai_chat_answer(
    user_text: str,
    system_prompt: str,
    image_bytes: Optional[bytes] = None,
    temperature: float = 0.5,
    max_tokens: int = 800,
    history: Optional[List[Dict[str, str]]] = None,
    model: Optional[str] = None,
    image_bytes_list: Optional[List[bytes]] = None,
) -> str:
    if not OPENAI_API_KEY:
        return "OPENAI_API_KEY не задан в переменных окружения."

    resolved_model = (model or "gpt-4o-mini").strip() or "gpt-4o-mini"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}

    images_to_send: List[bytes] = []
    if image_bytes_list:
        for item in image_bytes_list:
            if isinstance(item, (bytes, bytearray)) and item:
                images_to_send.append(bytes(item))
    elif image_bytes is not None:
        images_to_send.append(image_bytes)

    payload: Dict[str, Any] = {
        "model": resolved_model,
        "messages": [],
        "max_completion_tokens": max_tokens,
    }
    if _supports_temperature(resolved_model):
        payload["temperature"] = temperature

    if images_to_send:
        user_content: List[Dict[str, Any]] = []
        if user_text:
            user_content.append({"type": "text", "text": user_text})
        for img in images_to_send[:MAX_VISION_IMAGES]:
            _ext, mime = _detect_image_type(img)
            b64 = base64.b64encode(img).decode("utf-8")
            user_content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            })
        payload["messages"] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
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
                    msgs.append({"role": str(m["role"]), "content": str(m["content"])})
        msgs.append({"role": "user", "content": user_text})
        payload["messages"] = msgs

    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)

    if response.status_code != 200:
        return f"Ошибка OpenAI ({response.status_code}): {response.text[:1600]}"

    data = response.json()
    message = (((data.get("choices") or [{}])[0]).get("message") or {})
    return _extract_text_content(message.get("content")) or "Пустой ответ от модели."
