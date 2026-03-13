from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from billing_db import ensure_user_row, get_balance


router = APIRouter(prefix="/api/workspace", tags=["workspace"])


OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()


def _split_models(raw: str) -> List[str]:
    out: List[str] = []
    for part in (raw or "").split(","):
        model = (part or "").strip()
        if model and model not in out:
            out.append(model)
    return out


DEFAULT_CHAT_MODELS = _split_models(
    os.getenv("WORKSPACE_CHAT_MODELS", "gpt-4o-mini,gpt-5.4")
) or ["gpt-4o-mini", "gpt-5.4"]

DEFAULT_CHAT_MODEL = (
    os.getenv("WORKSPACE_DEFAULT_CHAT_MODEL", DEFAULT_CHAT_MODELS[0]) or DEFAULT_CHAT_MODELS[0]
).strip()
DEFAULT_PROMPT_BUILDER_MODEL = (
    os.getenv("WORKSPACE_PROMPT_BUILDER_MODEL", "gpt-5.4") or "gpt-5.4"
).strip()


class ChatTurn(BaseModel):
    role: str = Field(..., description="user|assistant")
    content: str = Field(..., min_length=1, max_length=8000)


class WorkspaceChatIn(BaseModel):
    telegram_user_id: Optional[int] = None
    text: str = Field(..., min_length=1, max_length=12000)
    history: Optional[List[ChatTurn]] = None
    model: Optional[str] = None
    mode: str = Field(default="chat", pattern="^(chat|prompt_builder)$")
    temperature: float = Field(default=0.6, ge=0.0, le=1.5)
    max_tokens: int = Field(default=900, ge=150, le=4000)


@router.get("/health")
async def workspace_health() -> Dict[str, Any]:
    return {"ok": True}


@router.get("/bootstrap")
async def workspace_bootstrap() -> Dict[str, Any]:
    return {
        "ok": True,
        "chat_models": DEFAULT_CHAT_MODELS,
        "live_integrations": [
            "workspace_chat",
            "balance",
            "kling3",
            "tts",
            "songwriter",
            "prompts",
        ],
    }


@router.get("/balance/{telegram_user_id}")
async def workspace_balance(telegram_user_id: int) -> Dict[str, Any]:
    uid = int(telegram_user_id)
    ensure_user_row(uid)
    return {
        "ok": True,
        "telegram_user_id": uid,
        "balance_tokens": int(get_balance(uid) or 0),
    }


def _resolve_chat_model(requested_model: Optional[str], mode: str) -> str:
    requested = (requested_model or "").strip()
    if requested in DEFAULT_CHAT_MODELS:
        return requested

    if mode == "prompt_builder":
        if DEFAULT_PROMPT_BUILDER_MODEL in DEFAULT_CHAT_MODELS:
            return DEFAULT_PROMPT_BUILDER_MODEL
        return DEFAULT_CHAT_MODELS[-1]

    if DEFAULT_CHAT_MODEL in DEFAULT_CHAT_MODELS:
        return DEFAULT_CHAT_MODEL
    return DEFAULT_CHAT_MODELS[0]


def _system_prompt(mode: str) -> str:
    if mode == "prompt_builder":
        return (
            "Ты — AstraBot Prompt Builder. "
            "Пишешь как сильный AI prompt engineer. "
            "На расплывчатые запросы всё равно собираешь рабочий промпт без лишних вопросов. "
            "Структура ответа: 1) краткая идея, 2) основной промпт, 3) улучшенная версия, 4) при необходимости короткие настройки под video/image/music."
        )
    return (
        "Ты — AstraBot Workspace Assistant. "
        "Помогаешь собирать идеи, тексты, сценарии, промпты и рабочие пайплайны для креативных задач. "
        "Отвечай понятно, по делу и в удобном для дальнейшего использования виде."
    )


async def _openai_chat(
    *,
    user_text: str,
    history: Optional[List[Dict[str, str]]],
    model: str,
    system_prompt: str,
    temperature: float,
    max_tokens: int,
) -> str:
    if not OPENAI_API_KEY:
        return "OPENAI_API_KEY не задан в переменных окружения."

    messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
    for item in history or []:
        role = (item.get("role") or "").strip().lower()
        content = (item.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        messages.append({"role": role, "content": content[:8000]})
    messages.append({"role": "user", "content": user_text[:12000]})

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=180) as client:
            r = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers=headers,
                json=payload,
            )
    except Exception as e:
        return f"Ошибка соединения с OpenAI: {e}"

    if r.status_code != 200:
        return f"Ошибка OpenAI ({r.status_code}): {r.text[:1500]}"

    try:
        data = r.json()
        return (data["choices"][0]["message"]["content"] or "").strip() or "Пустой ответ от модели."
    except Exception as e:
        return f"Не удалось разобрать ответ OpenAI: {e}"


@router.post("/chat")
async def workspace_chat(payload: WorkspaceChatIn) -> Dict[str, Any]:
    tg_user_id = payload.telegram_user_id
    if tg_user_id is not None:
        try:
            ensure_user_row(int(tg_user_id))
        except Exception:
            # Чат не должен падать только из-за проблемы с wallet-строкой.
            pass

    history: List[Dict[str, str]] = []
    if payload.history:
        history = [
            {"role": item.role, "content": item.content}
            for item in payload.history
            if item.role in ("user", "assistant") and (item.content or "").strip()
        ][-20:]

    model = _resolve_chat_model(payload.model, payload.mode)
    answer = await _openai_chat(
        user_text=payload.text.strip(),
        history=history,
        model=model,
        system_prompt=_system_prompt(payload.mode),
        temperature=payload.temperature,
        max_tokens=payload.max_tokens,
    )

    return {
        "ok": True,
        "answer": answer,
        "model": model,
        "mode": payload.mode,
        "telegram_user_id": tg_user_id,
    }
