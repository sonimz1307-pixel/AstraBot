"""
KIE Claude Sonnet chat helper for AstraBot.

Default product mode:
- Claude Sonnet 4.6 via KIE
- no internet/web search
- thinkingFlag enabled for normal answers
- history is supplied by caller (recommended: last 10 messages + compact summary)
"""

from __future__ import annotations

import base64
import os
from typing import Any, Dict, List, Optional, Tuple

import httpx

KIE_CLAUDE_MODEL_ID = (os.getenv("KIE_CLAUDE_MODEL", "claude-sonnet-4-6") or "claude-sonnet-4-6").strip()
KIE_CLAUDE_DISPLAY_NAME = "Claude Sonnet 4.6"
KIE_CLAUDE_OPUS_MODEL_ID = (os.getenv("KIE_CLAUDE_OPUS_MODEL", "claude-opus-4-7") or "claude-opus-4-7").strip()
KIE_CLAUDE_OPUS_DISPLAY_NAME = "Claude Opus 4.7"
KIE_CLAUDE_API_URL = (
    os.getenv("KIE_CLAUDE_API_URL", "https://api.kie.ai/claude/v1/messages")
    or "https://api.kie.ai/claude/v1/messages"
).strip()
KIE_CLAUDE_TIMEOUT_SEC = float(os.getenv("KIE_CLAUDE_TIMEOUT_SEC", "120") or "120")
KIE_CLAUDE_MAX_TOKENS = int(os.getenv("KIE_CLAUDE_MAX_TOKENS", "1500") or "1500")
KIE_CLAUDE_SUMMARY_MAX_CHARS = int(os.getenv("KIE_CLAUDE_SUMMARY_MAX_CHARS", "5000") or "5000")
KIE_CLAUDE_HISTORY_MESSAGES = int(os.getenv("KIE_CLAUDE_HISTORY_MESSAGES", "10") or "10")


def _unique_nonempty(values: List[str]) -> List[str]:
    out: List[str] = []
    for value in values:
        clean = str(value or "").strip()
        if clean and clean not in out:
            out.append(clean)
    return out


def kie_claude_model_ids() -> List[str]:
    return _unique_nonempty([KIE_CLAUDE_MODEL_ID, KIE_CLAUDE_OPUS_MODEL_ID])


def normalize_kie_claude_model(model: Any) -> str:
    value = str(model or "").strip()
    low = value.lower()
    if not low:
        return ""

    sonnet_aliases = {
        KIE_CLAUDE_MODEL_ID.lower(),
        "claude-sonnet-4-6",
        "claude sonnet 4.6",
        "sonnet-4-6",
        "sonnet 4.6",
        "claude",
        "claude-sonnet",
    }
    opus_aliases = {
        KIE_CLAUDE_OPUS_MODEL_ID.lower(),
        "claude-opus-4-7",
        "claude opus 4.7",
        "claude_opus_4_7",
        "opus-4-7",
        "opus 4.7",
        "claude-opus",
        "opus",
        "claude_opus",
    }
    if low in opus_aliases:
        return KIE_CLAUDE_OPUS_MODEL_ID
    if low in sonnet_aliases:
        return KIE_CLAUDE_MODEL_ID
    # Не пропускаем произвольные claude-* из client-side запроса.
    # Бесплатными должны быть только явно разрешённые модели выше.
    return ""


def is_kie_claude_model(model: Any) -> bool:
    return bool(normalize_kie_claude_model(model))


def kie_claude_display_name(model: Any) -> str:
    resolved = normalize_kie_claude_model(model) or KIE_CLAUDE_MODEL_ID
    if resolved.lower() == KIE_CLAUDE_OPUS_MODEL_ID.lower():
        return KIE_CLAUDE_OPUS_DISPLAY_NAME
    if resolved.lower() == KIE_CLAUDE_MODEL_ID.lower():
        return KIE_CLAUDE_DISPLAY_NAME
    cleaned = resolved.replace("claude-", "Claude ").replace("-", " ").strip()
    return cleaned[:1].upper() + cleaned[1:]

def _api_key() -> str:
    return (os.getenv("KIE_API_KEY") or os.getenv("KIE_AI_API_KEY") or "").strip()


def _clean_text(value: Any, limit: int = 12000) -> str:
    text = str(value or "").replace("\x00", "").strip()
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def sanitize_claude_history(history: Optional[List[Dict[str, Any]]], *, max_messages: int = KIE_CLAUDE_HISTORY_MESSAGES) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if not isinstance(history, list):
        return out
    for item in history:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        content = _clean_text(item.get("content"), 12000)
        if role in ("user", "assistant") and content:
            out.append({"role": role, "content": content})
    if max_messages > 0:
        out = out[-max_messages:]
    return out


def build_claude_system_prompt(base_prompt: str, summary: str = "") -> str:
    parts = [
        (base_prompt or "Ты полезный ассистент. Отвечай кратко и по делу.").strip(),
        "Интернет-поиск выключен. Не утверждай, что проверил актуальные данные в интернете.",
        "Если пользователь приложил файлы, анализируй только текст/содержимое, которое передал backend.",
    ]
    summary = _clean_text(summary, KIE_CLAUDE_SUMMARY_MAX_CHARS)
    if summary:
        parts.append("Краткая выжимка старого диалога:\n" + summary)
    return "\n\n".join(p for p in parts if p).strip()


def _extract_claude_text(data: Dict[str, Any]) -> str:
    content = data.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
                elif "text" in block and isinstance(block.get("text"), str):
                    parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p).strip()
    # Defensive fallback for OpenAI-like wrappers, if KIE changes the shape.
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        msg = (choices[0] or {}).get("message") or {}
        text = msg.get("content")
        if isinstance(text, str):
            return text.strip()
    return ""




def _detect_image_type(data: bytes) -> Tuple[str, str]:
    if not data:
        return ("jpg", "image/jpeg")
    if data.startswith(b"\xFF\xD8\xFF"):
        return ("jpg", "image/jpeg")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ("png", "image/png")
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        return ("webp", "image/webp")
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return ("gif", "image/gif")
    return ("jpg", "image/jpeg")


def _build_claude_user_content(user_text: str, image_bytes_list: Optional[List[bytes]] = None) -> Any:
    images: List[bytes] = []
    if isinstance(image_bytes_list, list):
        for item in image_bytes_list:
            if isinstance(item, (bytes, bytearray)) and item:
                images.append(bytes(item))

    text = _clean_text(user_text, 70000)
    if not images:
        return text

    content: List[Dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})
    for img in images[:4]:
        _ext, media_type = _detect_image_type(img)
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.b64encode(img).decode("utf-8"),
            },
        })
    return content or text

async def kie_claude_answer(
    *,
    user_text: str,
    system_prompt: str,
    history: Optional[List[Dict[str, Any]]] = None,
    summary: str = "",
    max_tokens: int = KIE_CLAUDE_MAX_TOKENS,
    thinking: bool = True,
    timeout_sec: float = KIE_CLAUDE_TIMEOUT_SEC,
    image_bytes_list: Optional[List[bytes]] = None,
    model: Optional[str] = None,
) -> str:
    api_key = _api_key()
    if not api_key:
        return "KIE_API_KEY не задан в переменных окружения."

    messages = sanitize_claude_history(history, max_messages=KIE_CLAUDE_HISTORY_MESSAGES)
    user_content = _build_claude_user_content(user_text, image_bytes_list)
    if user_content:
        messages.append({"role": "user", "content": user_content})
    if not messages:
        return "Пустой запрос."

    resolved_model = normalize_kie_claude_model(model) or KIE_CLAUDE_MODEL_ID

    payload: Dict[str, Any] = {
        "model": resolved_model,
        "system": build_claude_system_prompt(system_prompt, summary),
        "messages": messages,
        "thinkingFlag": bool(thinking),
        "stream": False,
        "max_tokens": max(150, min(int(max_tokens or KIE_CLAUDE_MAX_TOKENS), 1500)),
    }

    async with httpx.AsyncClient(timeout=timeout_sec) as client:
        response = await client.post(
            KIE_CLAUDE_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )

    if response.status_code >= 400:
        return f"Ошибка KIE Claude ({response.status_code}): {response.text[:1600]}"

    data = response.json()
    answer = _extract_claude_text(data)
    return answer or "Пустой ответ от Claude."


async def kie_claude_summarize_dialogue(
    *,
    messages: List[Dict[str, Any]],
    previous_summary: str = "",
    max_chars: int = KIE_CLAUDE_SUMMARY_MAX_CHARS,
) -> str:
    api_key = _api_key()
    if not api_key:
        return _clean_text(previous_summary, max_chars)

    lines: List[str] = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        content = _clean_text(item.get("content"), 1200)
        if role in ("user", "assistant") and content:
            lines.append(f"{role}: {content}")
    if not lines:
        return _clean_text(previous_summary, max_chars)

    prompt = (
        "Обнови краткую выжимку диалога для будущего контекста.\n"
        "Сохраняй только важное: цель пользователя, договорённости, текущие решения, ограничения, названия моделей/файлов/проектов.\n"
        "Не добавляй новых фактов. Не повторяй одно и то же. Верни только выжимку без вступлений.\n\n"
        f"Предыдущая выжимка:\n{_clean_text(previous_summary, max_chars) or '—'}\n\n"
        "Новые сообщения:\n" + "\n".join(lines)
    )

    payload: Dict[str, Any] = {
        "model": KIE_CLAUDE_MODEL_ID,
        "system": "Ты сжимаешь историю диалога для памяти ассистента. Пиши коротко, точно, без воды.",
        "messages": [{"role": "user", "content": prompt}],
        "thinkingFlag": False,
        "stream": False,
        "max_tokens": 900,
    }

    async with httpx.AsyncClient(timeout=KIE_CLAUDE_TIMEOUT_SEC) as client:
        response = await client.post(
            KIE_CLAUDE_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )

    if response.status_code >= 400:
        return _clean_text(previous_summary, max_chars)
    summary = _extract_claude_text(response.json())
    return _clean_text(summary or previous_summary, max_chars)
