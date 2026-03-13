from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from typing import Any, Deque, Dict, List, Optional
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from ai_chat import openai_chat_answer
from app.routers.prompts import categories as prompts_categories
from app.routers.prompts import groups as prompts_groups
from app.routers.prompts import items as prompts_items
from app.routers.tts import ALLOWED_VOICE_IDS, ALLOWED_VOICES
from app.services.eleven_tts import ElevenTTS
from app.services.telegram_webauth import TelegramWebAuthError, validate_telegram_init_data
from app.services.workspace_auth import (
    WORKSPACE_SESSION_TTL_SEC,
    create_access_token,
    get_current_workspace_user,
    get_optional_workspace_user,
)
from billing_db import add_tokens, ensure_user_row, get_balance
from db_supabase import track_user_activity
from kling3_flow import Kling3Error, create_kling3_task, get_kling3_task
from kling3_pricing import calculate_kling3_price
from songwriter_prompt import SONGWRITER_SYSTEM_PROMPT


router = APIRouter(prefix="/api/workspace", tags=["workspace"])

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_CHAT_MODEL = (os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini") or "gpt-4o-mini").strip()
PROMPT_BUILDER_MODEL = (os.getenv("PROMPT_BUILDER_MODEL", OPENAI_CHAT_MODEL) or OPENAI_CHAT_MODEL).strip()
WORKSPACE_BOT_URL = os.getenv("WORKSPACE_BOT_URL", "").strip()
WORKSPACE_AUTH_RATE = int(os.getenv("WORKSPACE_AUTH_RATE_LIMIT_PER_MIN", "12") or 12)
WORKSPACE_CHAT_RATE = int(os.getenv("WORKSPACE_CHAT_RATE_LIMIT_PER_MIN", "8") or 8)
WORKSPACE_KLING_RATE = int(os.getenv("WORKSPACE_KLING_RATE_LIMIT_PER_MIN", "4") or 4)
WORKSPACE_TTS_RATE = int(os.getenv("WORKSPACE_TTS_RATE_LIMIT_PER_MIN", "8") or 8)
WORKSPACE_SONGWRITER_RATE = int(os.getenv("WORKSPACE_SONGWRITER_RATE_LIMIT_PER_MIN", "8") or 8)

_rate_buckets: Dict[str, Deque[float]] = defaultdict(deque)
_tts_client: ElevenTTS | None = None


def _chat_models() -> List[str]:
    out: List[str] = []
    for model in (OPENAI_CHAT_MODEL, PROMPT_BUILDER_MODEL):
        m = (model or "").strip()
        if m and m not in out:
            out.append(m)
    return out or ["gpt-4o-mini"]


def _rate_limit(bucket: str, *, limit: int, window_sec: int = 60) -> None:
    now_ts = time.time()
    q = _rate_buckets[bucket]
    while q and now_ts - q[0] > window_sec:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again in a minute.")
    q.append(now_ts)


async def _openai_chat(*, user_text: str, history: Optional[List[Dict[str, str]]], model: str, system_prompt: str, temperature: float, max_tokens: int) -> str:
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY is not set")

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

    async with httpx.AsyncClient(timeout=180) as client:
        r = await client.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)

    if r.status_code != 200:
        raise HTTPException(status_code=500, detail=f"OpenAI error ({r.status_code}): {r.text[:1500]}")

    data = r.json()
    try:
        return (data["choices"][0]["message"]["content"] or "").strip() or "Пустой ответ от модели."
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Malformed OpenAI response: {e}")


def _system_prompt(mode: str) -> str:
    if mode == "prompt_builder":
        return (
            "Ты — AstraBot Prompt Builder. "
            "Отвечай как сильный AI prompt engineer и creative strategist. "
            "Строй ответ структурно: идея, основной промпт, улучшенная версия, опции под video/image/music. "
            "Если запрос расплывчатый — делай лучшую рабочую версию без лишних вопросов."
        )
    return (
        "Ты — AstraBot Workspace Assistant. "
        "Помогай как product-minded AI co-pilot: сценарии, промпты, creative direction, тексты, планы и упаковка идей в рабочий пайплайн. "
        "Пиши по делу, понятно и удобно для дальнейшего запуска в video/image/voice/music студиях."
    )


class TelegramAuthIn(BaseModel):
    init_data: str = Field(..., min_length=10)


class ChatTurn(BaseModel):
    role: str = Field(..., description="user|assistant")
    content: str = Field(..., min_length=1, max_length=8000)


class WorkspaceChatIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=12000)
    history: Optional[List[ChatTurn]] = None
    model: Optional[str] = None
    mode: str = Field(default="chat", pattern="^(chat|prompt_builder)$")
    temperature: float = Field(default=0.6, ge=0.0, le=1.5)
    max_tokens: int = Field(default=900, ge=150, le=4000)


class WorkspaceKlingCreateIn(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=4000)
    duration: int = Field(..., ge=3, le=15)
    resolution: str = Field(..., pattern="^(720|1080)$")
    enable_audio: bool = False
    aspect_ratio: str = Field(default="16:9", pattern="^(16:9|9:16|1:1)$")


class TTSGenerateIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=3000)
    voice_id: str = Field(..., min_length=10, max_length=64)
    model_id: str = Field(default="eleven_multilingual_v2")
    output_format: str = Field(default="mp3_44100_128")


class SongwriterPayload(BaseModel):
    text: str = Field("", description="Current user message")
    history: Optional[List[ChatTurn]] = None
    language: Optional[str] = None
    genre: Optional[str] = None
    mood: Optional[str] = None
    references: Optional[str] = None


def _songwriter_prompt_with_context(p: SongwriterPayload) -> str:
    ctx_parts = []
    if p.language:
        ctx_parts.append(f"Язык: {p.language}")
    if p.genre:
        ctx_parts.append(f"Жанр: {p.genre}")
    if p.mood:
        ctx_parts.append(f"Настроение: {p.mood}")
    if p.references:
        ctx_parts.append(f"Референсы/вайб: {p.references}")

    if not ctx_parts:
        return SONGWRITER_SYSTEM_PROMPT
    return SONGWRITER_SYSTEM_PROMPT + "\n\nКонтекст:\n- " + "\n- ".join(ctx_parts)


def _get_tts() -> ElevenTTS:
    global _tts_client
    if _tts_client is None:
        api_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
        if not api_key:
            raise HTTPException(status_code=500, detail="ELEVENLABS_API_KEY is not set")
        _tts_client = ElevenTTS(api_key=api_key)
    return _tts_client


@router.get("/health")
async def workspace_health() -> Dict[str, Any]:
    return {"ok": True, "service": "workspace"}


@router.get("/bootstrap")
async def workspace_bootstrap(user: Optional[Dict[str, Any]] = Depends(get_optional_workspace_user)) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "ok": True,
        "chat_models": _chat_models(),
        "live_integrations": [
            "workspace_chat",
            "balance",
            "kling3",
            "tts",
            "songwriter",
            "prompts",
        ],
        "bot_url": WORKSPACE_BOT_URL,
        "auth_required": True,
    }
    if user:
        ensure_user_row(int(user["telegram_user_id"]))
        payload["user"] = {
            "telegram_user_id": int(user["telegram_user_id"]),
            "username": user.get("username"),
            "first_name": user.get("first_name"),
            "last_name": user.get("last_name"),
            "language_code": user.get("language_code"),
            "is_premium": bool(user.get("is_premium", False)),
        }
        payload["balance_tokens"] = int(get_balance(int(user["telegram_user_id"])) or 0)
    return payload


@router.post("/auth/telegram")
async def workspace_auth_telegram(payload: TelegramAuthIn) -> Dict[str, Any]:
    _rate_limit("auth:telegram", limit=WORKSPACE_AUTH_RATE)
    try:
        validated = validate_telegram_init_data(payload.init_data)
    except TelegramWebAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))

    tg_user = validated["user"]
    tg_user_id = int(tg_user["id"])
    ensure_user_row(tg_user_id)
    track_user_activity(tg_user)

    access_token = create_access_token(user=tg_user)
    balance = int(get_balance(tg_user_id) or 0)

    return {
        "ok": True,
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": WORKSPACE_SESSION_TTL_SEC,
        "user": {
            "telegram_user_id": tg_user_id,
            "username": tg_user.get("username"),
            "first_name": tg_user.get("first_name"),
            "last_name": tg_user.get("last_name"),
            "language_code": tg_user.get("language_code"),
            "photo_url": tg_user.get("photo_url"),
            "is_premium": bool(tg_user.get("is_premium", False)),
        },
        "balance_tokens": balance,
    }


@router.get("/me")
async def workspace_me(user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    tg_user_id = int(user["telegram_user_id"])
    ensure_user_row(tg_user_id)
    return {
        "ok": True,
        "user": {
            "telegram_user_id": tg_user_id,
            "username": user.get("username"),
            "first_name": user.get("first_name"),
            "last_name": user.get("last_name"),
            "language_code": user.get("language_code"),
            "is_premium": bool(user.get("is_premium", False)),
        },
        "balance_tokens": int(get_balance(tg_user_id) or 0),
    }


@router.get("/balance")
async def workspace_balance(user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    tg_user_id = int(user["telegram_user_id"])
    ensure_user_row(tg_user_id)
    return {"ok": True, "telegram_user_id": tg_user_id, "balance_tokens": int(get_balance(tg_user_id) or 0)}


@router.post("/chat")
async def workspace_chat(payload: WorkspaceChatIn, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    tg_user_id = int(user["telegram_user_id"])
    _rate_limit(f"chat:{tg_user_id}", limit=WORKSPACE_CHAT_RATE)

    if payload.model and payload.model not in _chat_models():
        model = PROMPT_BUILDER_MODEL if payload.mode == "prompt_builder" else OPENAI_CHAT_MODEL
    else:
        model = (payload.model or "").strip() or (PROMPT_BUILDER_MODEL if payload.mode == "prompt_builder" else OPENAI_CHAT_MODEL)

    history = []
    if payload.history:
        history = [
            {"role": item.role, "content": item.content}
            for item in payload.history
            if item.role in ("user", "assistant") and (item.content or "").strip()
        ][-20:]

    answer = await _openai_chat(
        user_text=payload.text.strip(),
        history=history,
        model=model,
        system_prompt=_system_prompt(payload.mode),
        temperature=payload.temperature,
        max_tokens=payload.max_tokens,
    )
    return {"ok": True, "answer": answer, "mode": payload.mode, "model": model}


@router.post("/kling3/create")
async def workspace_kling3_create(payload: WorkspaceKlingCreateIn, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    tg_user_id = int(user["telegram_user_id"])
    _rate_limit(f"kling:{tg_user_id}", limit=WORKSPACE_KLING_RATE)

    request_id = str(uuid4())
    tokens_required = 0
    try:
        tokens_required = calculate_kling3_price(
            resolution=payload.resolution,
            enable_audio=payload.enable_audio,
            duration=payload.duration,
        )
        add_tokens(
            tg_user_id,
            -tokens_required,
            reason="workspace_kling3_create",
            ref_id=request_id,
            meta={
                "duration": payload.duration,
                "resolution": payload.resolution,
                "enable_audio": payload.enable_audio,
                "aspect_ratio": payload.aspect_ratio,
            },
        )

        task = await create_kling3_task(
            prompt=payload.prompt,
            duration=payload.duration,
            resolution=payload.resolution,
            enable_audio=payload.enable_audio,
            aspect_ratio=payload.aspect_ratio,
        )
        provider_task_id = None
        if isinstance(task, dict):
            provider_task_id = (task.get("data") or {}).get("task_id") or task.get("task_id")
        return {
            "ok": True,
            "request_id": request_id,
            "tokens_required": tokens_required,
            "provider_task_id": provider_task_id,
            "task": task,
            "balance_tokens": int(get_balance(tg_user_id) or 0),
        }
    except (ValueError, RuntimeError, Kling3Error) as e:
        if tokens_required > 0:
            try:
                add_tokens(
                    tg_user_id,
                    tokens_required,
                    reason="workspace_kling3_refund",
                    ref_id=request_id,
                    meta={"error": str(e)[:400]},
                )
            except Exception:
                pass
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        if tokens_required > 0:
            try:
                add_tokens(
                    tg_user_id,
                    tokens_required,
                    reason="workspace_kling3_refund",
                    ref_id=request_id,
                    meta={"error": str(e)[:400]},
                )
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=f"Internal error: {e}")


@router.get("/kling3/task/{task_id}")
async def workspace_kling3_task(task_id: str, _user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    try:
        task = await get_kling3_task(task_id)
        return {"ok": True, "task": task}
    except (ValueError, Kling3Error) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal error: {e}")


@router.get("/tts/voices")
async def workspace_tts_voices() -> Dict[str, Any]:
    return {"ok": True, "items": ALLOWED_VOICES}


@router.post("/tts/generate")
async def workspace_tts_generate(payload: TTSGenerateIn, user: Dict[str, Any] = Depends(get_current_workspace_user)):
    tg_user_id = int(user["telegram_user_id"])
    _rate_limit(f"tts:{tg_user_id}", limit=WORKSPACE_TTS_RATE)

    if payload.voice_id not in ALLOWED_VOICE_IDS:
        raise HTTPException(status_code=400, detail="voice_id is not allowed")
    tts = _get_tts()
    audio_bytes = await tts.tts(
        text=payload.text,
        voice_id=payload.voice_id,
        model_id=payload.model_id,
        output_format=payload.output_format,
    )
    media_type = "audio/mpeg" if payload.output_format.startswith("mp3") else "application/octet-stream"
    return Response(content=audio_bytes, media_type=media_type)


@router.post("/songwriter")
async def workspace_songwriter(payload: SongwriterPayload, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    tg_user_id = int(user["telegram_user_id"])
    _rate_limit(f"songwriter:{tg_user_id}", limit=WORKSPACE_SONGWRITER_RATE)
    user_text = (payload.text or "").strip()
    if not user_text:
        raise HTTPException(status_code=400, detail="Missing 'text'")

    history = None
    if payload.history:
        history = []
        for t in payload.history[-20:]:
            role = (t.role or "").strip().lower()
            content = (t.content or "").strip()
            if role not in ("user", "assistant") or not content:
                continue
            history.append({"role": role, "content": content})

    try:
        answer = await openai_chat_answer(
            user_text=user_text,
            system_prompt=_songwriter_prompt_with_context(payload),
            history=history,
            temperature=0.6,
            max_tokens=900,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Songwriter GPT error: {e}")

    return {"ok": True, "answer": answer}


@router.get("/prompts/categories")
async def workspace_prompts_categories() -> Dict[str, Any]:
    return prompts_categories()


@router.get("/prompts/groups")
async def workspace_prompts_groups(category: str) -> Dict[str, Any]:
    return prompts_groups(category=category)


@router.get("/prompts/items")
async def workspace_prompts_items(group_id: str) -> Dict[str, Any]:
    return prompts_items(group_id=group_id)
