from __future__ import annotations

import os
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.services.eleven_tts import ElevenTTS

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


@router.post("/generate")
async def generate(payload: TTSGenerateIn):
    """Generate TTS audio bytes (mp3 by default)."""
    if payload.voice_id not in ALLOWED_VOICE_IDS:
        raise HTTPException(status_code=400, detail="voice_id is not allowed")

    tts = _get_tts()
    audio_bytes = await tts.tts(
        text=payload.text,
        voice_id=payload.voice_id,
        model_id=payload.model_id,
        output_format=payload.output_format,
    )

    # mp3 -> audio/mpeg
    media_type = "audio/mpeg" if payload.output_format.startswith("mp3") else "application/octet-stream"
    return Response(content=audio_bytes, media_type=media_type)
