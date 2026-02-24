from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.services.eleven_tts import ElevenTTS

# If you have auth middleware, add dependencies here.
router = APIRouter(prefix="/api/tts", tags=["tts"])

# Lazy init so import-time doesn't crash if env isn't set during tests
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
    """Return voices available to your ElevenLabs API key."""
    tts = _get_tts()
    return await tts.list_voices()


class TTSGenerateIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=3000)
    voice_id: str = Field(..., min_length=10, max_length=64)
    model_id: str = Field(default="eleven_multilingual_v2")
    output_format: str = Field(default="mp3_44100_128")


@router.post("/generate")
async def generate(payload: TTSGenerateIn):
    """Generate TTS audio bytes (mp3 by default)."""
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
