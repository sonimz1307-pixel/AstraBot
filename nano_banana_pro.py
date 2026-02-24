"""nano_banana_pro.py

Provider router for Nano Banana Pro.

Purpose:
- Keep main.py changes minimal: main imports handle_nano_banana_pro from this module.
- Primary provider: PiAPI (existing implementation)
- Fallback provider: Replicate (google/nano-banana-pro)

Failover rules (minimal, practical):
- If PiAPI disabled (breaker) -> go Replicate
- Otherwise try PiAPI once.
- If PiAPI fails with capacity/rate-limit style error -> switch to Replicate.

Breaker:
- If PiAPI returns capacity error, we disable PiAPI for 5 hours in-memory.
  (If you want persistence across restarts, we can store disabled_until in Supabase later.)

Env:
- NANO_BANANA_PRIMARY: "piapi" (default) or "replicate"
- NANO_BANANA_DISABLE_HOURS: default 5
"""

from __future__ import annotations

from typing import Optional, Tuple
import os
import time

from nano_banana_pro_piapi import handle_nano_banana_pro as _piapi_handle, NanoBananaProError
from nano_banana_pro_replicate import handle_nano_banana_pro_replicate as _replicate_handle, NanoBananaProReplicateError


_PRIMARY = (os.getenv("NANO_BANANA_PRIMARY") or "piapi").strip().lower()
_DISABLE_HOURS = float(os.getenv("NANO_BANANA_DISABLE_HOURS") or "5")

# in-memory breaker
_piapi_disabled_until_ts: float = 0.0


def _now() -> float:
    return time.time()


def _disable_piapi(hours: float, reason: str = "") -> None:
    global _piapi_disabled_until_ts
    _piapi_disabled_until_ts = max(_piapi_disabled_until_ts, _now() + hours * 3600.0)


def _piapi_is_disabled() -> bool:
    return _now() < _piapi_disabled_until_ts


def _looks_like_capacity_error(msg: str) -> bool:
    m = (msg or "").lower()
    # Common PiAPI overload / rate limit signals
    return (
        "http 429" in m
        or "too many requests" in m
        or "high demand" in m
        or "service is currently unavailable" in m
        or "http 503" in m
        or "http 504" in m
        or "timeout" in m
    )


async def handle_nano_banana_pro(
    source_image_bytes: bytes,
    prompt: str,
    *,
    resolution: str = "2K",
    output_format: str = "jpg",
    telegram_file_id: Optional[str] = None,
) -> Tuple[bytes, str]:
    """Main entrypoint used by main.py: returns (out_bytes, ext)."""

    # If primary is replicate, just use it.
    if _PRIMARY == "replicate":
        return await _replicate_handle(
            source_image_bytes,
            prompt,
            resolution=resolution,
            output_format=output_format,
            telegram_file_id=telegram_file_id,
        )

    # primary = piapi
    if _piapi_is_disabled():
        return await _replicate_handle(
            source_image_bytes,
            prompt,
            resolution=resolution,
            output_format=output_format,
            telegram_file_id=telegram_file_id,
        )

    try:
        return await _piapi_handle(
            source_image_bytes,
            prompt,
            resolution=resolution,
            output_format=output_format,
            telegram_file_id=telegram_file_id,
        )
    except NanoBananaProError as e:
        msg = str(e)
        if _looks_like_capacity_error(msg):
            _disable_piapi(_DISABLE_HOURS, reason=msg)
            # fallback immediately
            return await _replicate_handle(
                source_image_bytes,
                prompt,
                resolution=resolution,
                output_format=output_format,
                telegram_file_id=telegram_file_id,
            )
        raise
    except Exception as e:
        # any unexpected piapi errors -> do NOT blindly failover (could be a bug)
        raise
