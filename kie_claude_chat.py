"""
KIE Claude Sonnet chat helper for AstraBot.

Default product mode:
- Claude Sonnet 4.6 via KIE
- no internet/web search
- thinkingFlag enabled for normal answers
- history is supplied by caller (recommended: last 10 messages + compact summary)
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import httpx

KIE_CLAUDE_MODEL_ID = (os.getenv("KIE_CLAUDE_MODEL", "claude-sonnet-4-6") or "claude-sonnet-4-6").strip()
KIE_CLAUDE_DISPLAY_NAME = "Claude Sonnet 4.6"
KIE_CLAUDE_API_URL = (
    os.getenv("KIE_CLAUDE_API_URL", "https://api.kie.ai/claude/v1/messages")
    or "https://api.kie.ai/claude/v1/messages"
).strip()
KIE_CLAUDE_TIMEOUT_SEC = float(os.getenv("KIE_CLAUDE_TIMEOUT_SEC", "120") or "120")
KIE_CLAUDE_MAX_TOKENS = int(os.getenv("KIE_CLAUDE_MAX_TOKENS", "1500") or "1500")
KIE_CLAUDE_SUMMARY_MAX_CHARS = int(os.getenv("KIE_CLAUDE_SUMMARY_MAX_CHARS", "5000") or "5000")
KIE_CLAUDE_HISTORY_MESSAGES = int(os.getenv("KIE_CLAUDE_HISTORY_MESSAGES", "10") or "10")


def is_kie_claude_model(model: Any) -> bool:
    value = str(model or "").strip().lower()
    return value in {
        KIE_CLAUDE_MODEL_ID.lower(),
        "claude-sonnet-4-6",
        "claude sonnet 4.6",
        "sonnet-4-6",
        "sonnet 4.6",
    }


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


async def kie_claude_answer(
    *,
    user_text: str,
    system_prompt: str,
    history: Optional[List[Dict[str, Any]]] = None,
    summary: str = "",
    max_tokens: int = KIE_CLAUDE_MAX_TOKENS,
    thinking: bool = True,
    timeout_sec: float = KIE_CLAUDE_TIMEOUT_SEC,
) -> str:
    api_key = _api_key()
    if not api_key:
        return "KIE_API_KEY не задан в переменных окружения."

    messages = sanitize_claude_history(history, max_messages=KIE_CLAUDE_HISTORY_MESSAGES)
    text = _clean_text(user_text, 70000)
    if text:
        messages.append({"role": "user", "content": text})
    if not messages:
        return "Пустой запрос."

    payload: Dict[str, Any] = {
        "model": KIE_CLAUDE_MODEL_ID,
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
