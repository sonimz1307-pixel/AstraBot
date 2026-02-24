from __future__ import annotations

from typing import Any, Dict, List

from elevenlabs.client import ElevenLabs


class ElevenTTS:
    """Thin async-friendly wrapper around ElevenLabs SDK.

    SDK methods are sync, but they're fast; if you need strict async, run in threadpool.
    """

    def __init__(self, *, api_key: str):
        self.client = ElevenLabs(api_key=api_key)

    async def list_voices(self) -> List[Dict[str, Any]]:
        # Using SDK list voices. Field set can vary by SDK version.
        voices = self.client.voices.get_all()
        out: List[Dict[str, Any]] = []
        for v in getattr(voices, "voices", voices) or []:
            out.append({
                "voice_id": getattr(v, "voice_id", None) or getattr(v, "id", None),
                "name": getattr(v, "name", None),
                "category": getattr(v, "category", None),
                "labels": getattr(v, "labels", None),
                "preview_url": getattr(v, "preview_url", None),
            })
        # Keep only entries with ids
        return [x for x in out if x.get("voice_id")]

    async def tts(self, *, text: str, voice_id: str, model_id: str, output_format: str) -> bytes:
        audio = self.client.text_to_speech.convert(
            text=text,
            voice_id=voice_id,
            model_id=model_id,
            output_format=output_format,
        )
        # SDK returns bytes-like
        return bytes(audio)
