from __future__ import annotations

import os
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from ai_chat import openai_chat_answer
from app.routers.prompts import categories as prompts_categories
from app.routers.prompts import groups as prompts_groups
from app.routers.prompts import items as prompts_items
from app.routers.tts import ALLOWED_VOICE_IDS, ALLOWED_VOICES
from app.services.eleven_tts import ElevenTTS
from app.services.telegram_webauth import TelegramWebAuthError, validate_telegram_login_data
from app.services.workspace_auth import WORKSPACE_SESSION_TTL_SEC, create_access_token, get_current_workspace_user, get_optional_workspace_user
from billing_db import add_tokens, ensure_user_row, get_balance
from db_supabase import supabase, track_user_activity
from kling3_flow import Kling3Error, create_kling3_task, get_kling3_task
from kling3_pricing import calculate_kling3_price
from songwriter_prompt import SONGWRITER_SYSTEM_PROMPT

router = APIRouter(prefix="/api/workspace", tags=["workspace"])
OPENAI_CHAT_MODEL = (os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini") or "gpt-4o-mini").strip()
PROMPT_BUILDER_MODEL = (os.getenv("PROMPT_BUILDER_MODEL", "gpt-5.4") or "gpt-5.4").strip()
_tts_client: ElevenTTS | None = None


def _chat_models() -> List[str]:
    out: List[str] = []
    for m in [OPENAI_CHAT_MODEL, PROMPT_BUILDER_MODEL]:
        m = (m or "").strip()
        if m and m not in out:
            out.append(m)
    return out or ["gpt-4o-mini"]


def _get_tts() -> ElevenTTS:
    global _tts_client
    if _tts_client is None:
        api_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
        if not api_key:
            raise HTTPException(status_code=500, detail="ELEVENLABS_API_KEY is not set")
        _tts_client = ElevenTTS(api_key=api_key)
    return _tts_client


def _find_existing_bot_user(telegram_user_id: int) -> Optional[Dict[str, Any]]:
    if supabase is None:
        return {"telegram_user_id": telegram_user_id}
    try:
        r = supabase.table("bot_users").select("telegram_user_id,username,first_name,last_name,language_code,is_premium").eq("telegram_user_id", int(telegram_user_id)).limit(1).execute()
        if getattr(r, "data", None):
            return r.data[0]
    except Exception:
        return {"telegram_user_id": telegram_user_id}
    return None


def _workspace_user_payload(user: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "telegram_user_id": int(user.get("telegram_user_id") or user.get("id") or 0),
        "username": user.get("username"),
        "first_name": user.get("first_name"),
        "last_name": user.get("last_name"),
        "language_code": user.get("language_code"),
        "photo_url": user.get("photo_url"),
        "is_premium": bool(user.get("is_premium", False)),
    }


def _songwriter_prompt_with_context(p: "SongwriterPayload") -> str:
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


class TelegramAuthPayload(BaseModel):
    auth_data: Dict[str, Any]


class ChatTurn(BaseModel):
    role: str
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


@router.get("/health")
async def workspace_health() -> Dict[str, Any]:
    return {"ok": True, "service": "workspace"}


@router.get("/bootstrap")
async def workspace_bootstrap(user: Optional[Dict[str, Any]] = Depends(get_optional_workspace_user)) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "ok": True,
        "chat_models": _chat_models(),
        "live_integrations": ["workspace_chat", "balance", "kling3", "tts", "songwriter", "prompts"],
        "auth_required": True,
    }
    if user:
        uid = int(user["telegram_user_id"])
        ensure_user_row(uid)
        payload["user"] = _workspace_user_payload(user)
        payload["balance_tokens"] = int(get_balance(uid) or 0)
    return payload


@router.post("/auth/telegram")
async def workspace_auth_telegram(payload: TelegramAuthPayload) -> Dict[str, Any]:
    try:
        verified = validate_telegram_login_data(payload.auth_data)
    except TelegramWebAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))

    uid = int(verified["id"])
    existing = _find_existing_bot_user(uid)
    if existing is None:
        raise HTTPException(status_code=403, detail="Пользователь не найден в AstraBot. Сначала открой Telegram-бота и запусти его хотя бы один раз.")

    tg_user = {
        "id": uid,
        "username": verified.get("username") or existing.get("username"),
        "first_name": verified.get("first_name") or existing.get("first_name"),
        "last_name": verified.get("last_name") or existing.get("last_name"),
        "language_code": existing.get("language_code"),
        "photo_url": verified.get("photo_url"),
        "is_premium": existing.get("is_premium", False),
    }
    ensure_user_row(uid)
    track_user_activity(tg_user)
    access_token = create_access_token(user=tg_user)
    balance = int(get_balance(uid) or 0)
    return {
        "ok": True,
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": WORKSPACE_SESSION_TTL_SEC,
        "user": _workspace_user_payload(tg_user),
        "balance_tokens": balance,
    }


@router.post("/logout")
async def workspace_logout() -> Dict[str, Any]:
    return {"ok": True}


@router.get("/me")
async def workspace_me(user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    ensure_user_row(uid)
    return {"ok": True, "user": _workspace_user_payload(user), "balance_tokens": int(get_balance(uid) or 0)}


@router.get("/balance")
async def workspace_balance(user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    ensure_user_row(uid)
    balance = int(get_balance(uid) or 0)
    return {"ok": True, "balance_tokens": balance}


@router.post("/chat")
async def workspace_chat(payload: WorkspaceChatIn, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    model = (payload.model or "").strip() or (PROMPT_BUILDER_MODEL if payload.mode == "prompt_builder" else OPENAI_CHAT_MODEL)
    hist = []
    if payload.history:
        hist = [{"role": item.role, "content": item.content} for item in payload.history if item.role in ("user", "assistant")]
    answer = await openai_chat_answer(
        user_text=payload.text.strip(),
        system_prompt=(
            "Ты — AstraBot Prompt Builder. Отвечай как сильный AI prompt engineer и creative strategist. Строй ответ структурно: идея, основной промпт, улучшенная версия, опции под video/image/music. Если запрос расплывчатый — делай лучшую рабочую версию без лишних вопросов."
            if payload.mode == "prompt_builder"
            else "Ты — AstraBot Workspace Assistant. Помогай как product-minded AI co-pilot: сценарии, промпты, creative direction, тексты, планы и упаковка идей в рабочий пайплайн. Пиши по делу, понятно и удобно для дальнейшего запуска в video/image/voice/music студиях."
        ),
        history=hist,
        temperature=payload.temperature,
        max_tokens=payload.max_tokens,
    )
    return {"ok": True, "answer": answer, "mode": payload.mode, "model": model}


@router.post("/kling3/create")
async def workspace_kling3_create(payload: WorkspaceKlingCreateIn, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    request_id = str(uuid4())
    tokens_required = 0
    try:
        tokens_required = calculate_kling3_price(resolution=payload.resolution, enable_audio=payload.enable_audio, duration=payload.duration)
        add_tokens(uid, -tokens_required, reason="kling3_create", ref_id=request_id, meta={"duration": payload.duration, "resolution": payload.resolution, "enable_audio": payload.enable_audio, "aspect_ratio": payload.aspect_ratio})
        task = await create_kling3_task(prompt=payload.prompt, duration=payload.duration, resolution=payload.resolution, enable_audio=payload.enable_audio, aspect_ratio=payload.aspect_ratio)
        provider_task_id = None
        if isinstance(task, dict):
            provider_task_id = (task.get("data") or {}).get("task_id") or task.get("task_id")
        return {"ok": True, "request_id": request_id, "tokens_required": tokens_required, "provider_task_id": provider_task_id, "task": task}
    except (ValueError, Kling3Error) as e:
        if tokens_required > 0:
            try:
                add_tokens(uid, tokens_required, reason="kling3_refund", ref_id=request_id, meta={"error": str(e)})
            except Exception:
                pass
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        if tokens_required > 0:
            try:
                add_tokens(uid, tokens_required, reason="kling3_refund", ref_id=request_id, meta={"error": str(e)})
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=f"Internal error: {e}")


@router.get("/kling3/task/{task_id}")
async def workspace_kling3_task(task_id: str, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    try:
        task = await get_kling3_task(task_id)
        return {"ok": True, "task": task}
    except (ValueError, Kling3Error) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal error: {e}")


@router.get("/tts/voices")
async def workspace_tts_voices(user: Dict[str, Any] = Depends(get_current_workspace_user)) -> List[Dict[str, Any]]:
    return ALLOWED_VOICES


@router.post("/tts/generate")
async def workspace_tts_generate(payload: TTSGenerateIn, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Response:
    if payload.voice_id not in ALLOWED_VOICE_IDS:
        raise HTTPException(status_code=400, detail="voice_id is not allowed")
    tts = _get_tts()
    audio_bytes = await tts.tts(text=payload.text, voice_id=payload.voice_id, model_id=payload.model_id, output_format=payload.output_format)
    media_type = "audio/mpeg" if payload.output_format.startswith("mp3") else "application/octet-stream"
    return Response(content=audio_bytes, media_type=media_type)


@router.post("/songwriter")
async def workspace_songwriter(payload: SongwriterPayload, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    user_text = (payload.text or "").strip()
    if not user_text:
        raise HTTPException(status_code=400, detail="Missing 'text'")
    history = []
    if payload.history:
        history = [{"role": t.role, "content": t.content} for t in payload.history if t.role in ("user", "assistant") and (t.content or "").strip()]
    answer = await openai_chat_answer(user_text=user_text, system_prompt=_songwriter_prompt_with_context(payload), history=history, temperature=0.6, max_tokens=900)
    return {"answer": answer}


@router.get("/prompts/categories")
async def workspace_prompts_categories(user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    return prompts_categories()


@router.get("/prompts/groups")
async def workspace_prompts_groups(category: str, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    return prompts_groups(category=category)


@router.get("/prompts/items")
async def workspace_prompts_items(group_id: str, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    return prompts_items(group_id=group_id)
