from __future__ import annotations

import os
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.services.eleven_tts import ElevenTTS
from app.routers.prompts_admin import _check_telegram_init_data
from free_plan_limits import FEATURE_TTS, FreePlanLimitError, consume_free_usage, free_limit_http_detail, release_free_usage, validate_free_tts_text

router = APIRouter(prefix="/api/tts", tags=["tts"])

# ---- Your curated catalog (shown to users) ----
ALLOWED_VOICES: List[Dict[str, Any]] = [
    {"voice_id": "huXlXYhtMIZkTYxM93t6", "name": "Масон"},
    {"voice_id": "IO0VLmDxIb8N5msewtV4", "name": "Анна"},
    {"voice_id": "gCqVHuQpLDMkHrGiG95I", "name": "Татьяна"},
    {"voice_id": "OowtKaZH9N7iuGbsd00l", "name": "Вероника"},
    {"voice_id": "kwajW3Xh5svCeKU5ky2S", "name": "Дмитрий"},
    {"voice_id": "gJEfHTTiifXEDmO687lC", "name": "Принц Нур"},
    {"voice_id": "oKxkBkm5a8Bmrd1Whf2c", "name": "Нур"},
    {"voice_id": "3EuKHIEZbSzrHGNmdYsx", "name": "Николай"},
]
ALLOWED_VOICE_IDS = {v["voice_id"] for v in ALLOWED_VOICES}

# Lazy init: don't crash on import if env isn't set (tests, etc.)
_tts: Optional[ElevenTTS] = None


def _get_tts() -> ElevenTTS:
    global _tts
    if _tts is None:
        api_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
        if not api_key:
            raise HTTPException(status_code=500, detail="ELEVENLABS_API_KEY is not set")
        _tts = ElevenTTS(api_key=api_key)
    return _tts


def _safe_uid(*values: Any) -> int:
    for value in values:
        try:
            text = str(value or "").strip()
            if text and text.isdigit():
                uid = int(text)
                if uid > 0:
                    return uid
        except Exception:
            continue
    return 0


def _uid_from_initdata(x_tg_initdata: Optional[str]) -> int:
    init_data = str(x_tg_initdata or "").strip()
    if not init_data:
        return 0
    verified = _check_telegram_init_data(init_data)
    user = verified.get("user") if isinstance(verified, dict) else {}
    return _safe_uid((user or {}).get("id"))


def _consume_tts_limit(user_id: int, text: str) -> None:
    if user_id <= 0:
        raise HTTPException(
            status_code=401,
            detail={"code": "tts_auth_required", "message": "Озвучка доступна только авторизованным пользователям."},
        )
    try:
        validate_free_tts_text(user_id, text)
        consume_free_usage(user_id, FEATURE_TTS)
    except FreePlanLimitError as exc:
        status_code = 413 if exc.code == "free_tts_text_too_long" else 429
        raise HTTPException(status_code=status_code, detail=free_limit_http_detail(exc))


@router.get("/voices")
async def list_voices():
    """Return ONLY the curated voices list (for UI dropdown/buttons)."""
    return ALLOWED_VOICES


class TTSGenerateIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=3000)
    voice_id: str = Field(..., min_length=10, max_length=64)

    # Defaults: safe for Russian too
    model_id: str = Field(default="eleven_multilingual_v2")
    output_format: str = Field(default="mp3_44100_128")
    uid: Optional[int] = Field(default=None, ge=1)


@router.post("/generate")
async def generate(
    payload: TTSGenerateIn,
    uid: Optional[str] = Query(default=None),
    x_uid: Optional[str] = Header(default=None, alias="X-UID"),
    x_tg_initdata: Optional[str] = Header(default=None, alias="X-TG-INITDATA"),
):
    """Generate TTS audio bytes (mp3 by default)."""
    if payload.voice_id not in ALLOWED_VOICE_IDS:
        raise HTTPException(status_code=400, detail="voice_id is not allowed")

    user_id = _uid_from_initdata(x_tg_initdata) if str(x_tg_initdata or "").strip() else _safe_uid(payload.uid, uid, x_uid)
    _consume_tts_limit(user_id, payload.text)

    tts = _get_tts()
    try:
        audio_bytes = await tts.tts(
            text=payload.text,
            voice_id=payload.voice_id,
            model_id=payload.model_id,
            output_format=payload.output_format,
        )
    except Exception:
        release_free_usage(user_id, FEATURE_TTS)
        raise

    # mp3 -> audio/mpeg
    media_type = "audio/mpeg" if payload.output_format.startswith("mp3") else "application/octet-stream"
    return Response(content=audio_bytes, media_type=media_type)
