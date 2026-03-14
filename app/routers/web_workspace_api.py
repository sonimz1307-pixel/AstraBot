from __future__ import annotations

import os
from datetime import datetime, timezone
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



def _first_nonempty(*values: Any) -> Optional[str]:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _normalize_kling3_task(task: Dict[str, Any]) -> Dict[str, Any]:
    payload = task.get("data") if isinstance(task.get("data"), dict) else task
    output = payload.get("output") if isinstance(payload, dict) and isinstance(payload.get("output"), dict) else {}
    error = payload.get("error") if isinstance(payload, dict) and isinstance(payload.get("error"), dict) else {}

    provider_status = str(
        (payload.get("status") if isinstance(payload, dict) else None)
        or task.get("status")
        or task.get("state")
        or ""
    ).strip().lower()

    video_url = _first_nonempty(
        output.get("video"),
        output.get("video_url"),
        output.get("url"),
        payload.get("video") if isinstance(payload, dict) else None,
        payload.get("video_url") if isinstance(payload, dict) else None,
        task.get("video"),
        task.get("video_url"),
    )
    download_url = _first_nonempty(
        output.get("download_url"),
        payload.get("download_url") if isinstance(payload, dict) else None,
        task.get("download_url"),
    )
    cover_url = _first_nonempty(
        output.get("cover_url"),
        payload.get("cover_url") if isinstance(payload, dict) else None,
        task.get("cover_url"),
    )
    output_url = video_url or download_url

    percent_raw = output.get("percent")
    percent: Optional[int] = None
    if percent_raw not in (None, ""):
        try:
            percent = max(0, min(100, int(round(float(percent_raw)))))
        except Exception:
            percent = None

    error_message = _first_nonempty(
        error.get("message"),
        error.get("raw_message"),
        payload.get("error_message") if isinstance(payload, dict) else None,
        output.get("message"),
        task.get("message"),
        task.get("detail"),
    )

    if provider_status in {"failed", "error", "cancelled", "canceled"}:
        status = "failed"
    elif output_url:
        status = "succeeded"
    elif provider_status in {"queued", "pending"}:
        status = "queued"
    else:
        status = "processing"

    return {
        "task_id": _first_nonempty(
            payload.get("task_id") if isinstance(payload, dict) else None,
            task.get("task_id"),
        ),
        "model": payload.get("model") if isinstance(payload, dict) else task.get("model"),
        "task_type": payload.get("task_type") if isinstance(payload, dict) else task.get("task_type"),
        "status": status,
        "provider_status": provider_status or "unknown",
        "percent": percent,
        "video_url": video_url,
        "download_url": download_url,
        "output_url": output_url,
        "cover_url": cover_url,
        "error_message": error_message,
        "finished": bool(output_url or provider_status in {"failed", "error", "cancelled", "canceled"}),
    }


_WORKSPACE_VIDEO_GENERATIONS_TABLE = "workspace_video_generations"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_generation_status(normalized_status: Optional[str]) -> str:
    status = str(normalized_status or "").strip().lower()
    if status == "succeeded":
        return "completed"
    if status in {"failed", "queued", "processing"}:
        return status
    return "processing"


def _insert_workspace_generation(row: Dict[str, Any]) -> str:
    generation_id = str(row.get("id") or uuid4())
    payload = dict(row)
    payload["id"] = generation_id
    if supabase is None:
        return generation_id
    resp = supabase.table(_WORKSPACE_VIDEO_GENERATIONS_TABLE).insert(payload).execute()
    data = getattr(resp, "data", None) or []
    if data and isinstance(data[0], dict):
        saved_id = data[0].get("id")
        if saved_id:
            return str(saved_id)
    return generation_id


def _update_workspace_generation(generation_id: Optional[str], patch: Dict[str, Any]) -> None:
    if not generation_id or not patch or supabase is None:
        return
    supabase.table(_WORKSPACE_VIDEO_GENERATIONS_TABLE).update(patch).eq("id", str(generation_id)).execute()


def _mark_workspace_generation_failed(generation_id: Optional[str], error_message: str, error_code: Optional[str] = None) -> None:
    if not generation_id:
        return
    patch: Dict[str, Any] = {
        "status": "failed",
        "error_message": (error_message or "").strip()[:4000] or "Unknown error",
    }
    if error_code:
        patch["error_code"] = str(error_code)[:255]
    try:
        _update_workspace_generation(generation_id, patch)
    except Exception:
        pass


def _sync_workspace_generation_by_task(user_id: int, task_id: Optional[str], normalized: Dict[str, Any]) -> None:
    task_id_text = str(task_id or normalized.get("task_id") or "").strip()
    if supabase is None or not task_id_text:
        return

    db_status = _db_generation_status(normalized.get("status"))
    patch: Dict[str, Any] = {
        "status": db_status,
    }

    provider_video_url = _first_nonempty(
        normalized.get("video_url"),
        normalized.get("download_url"),
        normalized.get("output_url"),
    )
    if provider_video_url:
        patch["provider_video_url"] = provider_video_url

    error_message = _first_nonempty(normalized.get("error_message"))
    if db_status == "completed":
        patch["completed_at"] = _utc_now_iso()
        patch["error_code"] = None
        patch["error_message"] = None
    elif db_status == "failed":
        patch["completed_at"] = _utc_now_iso()
        patch["error_code"] = "provider_error"
        patch["error_message"] = (error_message or "Provider task failed")[:4000]

    supabase.table(_WORKSPACE_VIDEO_GENERATIONS_TABLE).update(patch).eq("user_id", str(user_id)).eq("task_id", task_id_text).execute()


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
    generation_id: Optional[str] = None
    tokens_required = 0
    try:
        tokens_required = calculate_kling3_price(
            resolution=payload.resolution,
            enable_audio=payload.enable_audio,
            duration=payload.duration,
        )
        add_tokens(
            uid,
            -tokens_required,
            reason="kling3_create",
            ref_id=request_id,
            meta={
                "duration": payload.duration,
                "resolution": payload.resolution,
                "enable_audio": payload.enable_audio,
                "aspect_ratio": payload.aspect_ratio,
            },
        )

        generation_id = _insert_workspace_generation(
            {
                "user_id": str(uid),
                "provider": "kling",
                "model": "3.0",
                "mode": "text_to_video",
                "prompt": payload.prompt.strip(),
                "status": "processing",
                "aspect_ratio": payload.aspect_ratio,
                "duration_sec": int(payload.duration),
                "resolution": payload.resolution,
                "enable_audio": bool(payload.enable_audio),
                "origin": "workspace",
            }
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

        if generation_id and provider_task_id:
            _update_workspace_generation(
                generation_id,
                {
                    "task_id": str(provider_task_id),
                },
            )

        return {
            "ok": True,
            "generation_id": generation_id,
            "request_id": request_id,
            "tokens_required": tokens_required,
            "provider_task_id": provider_task_id,
            "task": task,
        }
    except (ValueError, Kling3Error) as e:
        _mark_workspace_generation_failed(generation_id, str(e), error_code="provider_error")
        if tokens_required > 0:
            try:
                add_tokens(uid, tokens_required, reason="kling3_refund", ref_id=request_id, meta={"error": str(e)})
            except Exception:
                pass
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _mark_workspace_generation_failed(generation_id, str(e), error_code="internal_error")
        if tokens_required > 0:
            try:
                add_tokens(uid, tokens_required, reason="kling3_refund", ref_id=request_id, meta={"error": str(e)})
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=f"Internal error: {e}")


@router.get("/kling3/task/{task_id}")
async def workspace_kling3_task(task_id: str, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    try:
        task = await get_kling3_task(task_id)
        normalized = _normalize_kling3_task(task if isinstance(task, dict) else {"raw": task})
        try:
            _sync_workspace_generation_by_task(uid, task_id, normalized)
        except Exception:
            pass
        return {"ok": True, "task": task, "normalized": normalized}
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
