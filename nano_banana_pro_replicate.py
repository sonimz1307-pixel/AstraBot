"""nano_banana_pro_replicate.py

Nano Banana Pro via Replicate (google/nano-banana-pro).

Provides a drop-in compatible function for main.py:
  handle_nano_banana_pro_replicate(source_image_bytes, prompt, resolution=..., output_format=..., telegram_file_id=...)

Design goals:
- Minimal changes to existing codebase
- Prefer passing a public Telegram file URL (so we don't upload binaries)
- Default 2K; enforce your product rule (1K/2K only)

Env:
- REPLICATE_API_TOKEN (already in your env)
- REPLICATE_NANO_BANANA_PRO_MODEL (optional, default google/nano-banana-pro)
- REPLICATE_NANO_SAFETY (optional, default block_only_high)
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
import os
import base64

import aiohttp
import httpx

from replicate_http import (
    post_prediction,
    get_prediction_get_url,
    wait_for_result_url,
    ReplicateHTTPError,
)


REPLICATE_MODEL_SLUG = (os.getenv("REPLICATE_NANO_BANANA_PRO_MODEL") or "google/nano-banana-pro").strip() or "google/nano-banana-pro"

TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_API_BASE = os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")

# Your product rule: support only 1K/2K (default 2K)
ALLOWED_RESOLUTIONS = ("1K", "2K")
DEFAULT_RESOLUTION = "2K"


class NanoBananaProReplicateError(RuntimeError):
    pass


async def _tg_get_file_path(file_id: str) -> str:
    if not TELEGRAM_BOT_TOKEN:
        raise NanoBananaProReplicateError("TELEGRAM_BOT_TOKEN is not set (needed to build Telegram file URL).")

    url = f"{TELEGRAM_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/getFile"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, params={"file_id": file_id})
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise NanoBananaProReplicateError(f"Telegram getFile failed: {data}")

    fp = ((data.get("result") or {}) if isinstance(data.get("result"), dict) else {}).get("file_path") or ""
    if not fp:
        raise NanoBananaProReplicateError(f"Telegram getFile: missing file_path: {data}")
    return fp


async def _tg_file_url(file_id: str) -> str:
    fp = await _tg_get_file_path(file_id)
    return f"{TELEGRAM_API_BASE}/file/bot{TELEGRAM_BOT_TOKEN}/{fp}"


def _data_url_from_bytes(img_bytes: bytes) -> str:
    # fallback if you don't have telegram_file_id. Not guaranteed everywhere.
    b64 = base64.b64encode(img_bytes).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


async def _download_bytes(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=180) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.content


def _normalize_resolution(resolution: str) -> str:
    r = (resolution or DEFAULT_RESOLUTION).strip().upper()
    if r not in ALLOWED_RESOLUTIONS:
        return DEFAULT_RESOLUTION
    return r


async def handle_nano_banana_pro_replicate(
    source_image_bytes: bytes,
    prompt: str,
    *,
    resolution: str = DEFAULT_RESOLUTION,
    output_format: str = "jpg",
    telegram_file_id: Optional[str] = None,
    aspect_ratio: str = "1:1",
) -> Tuple[bytes, str]:
    """Returns (out_bytes, ext) using Replicate."""

    p = (prompt or "").strip()
    if not p:
        raise NanoBananaProReplicateError("Empty prompt")

    res = _normalize_resolution(resolution)

    # Build a URL Replicate can fetch. Prefer Telegram file URL.
    if telegram_file_id:
        src_url = await _tg_file_url(telegram_file_id)
    else:
        src_url = _data_url_from_bytes(source_image_bytes)

    # Replicate input schema (per your snippet)
    inp: Dict[str, Any] = {
        "prompt": p,
        "resolution": res,
        "image_input": [src_url],
        "aspect_ratio": aspect_ratio,
        "output_format": "png" if output_format.lower() == "png" else "jpg",
        "safety_filter_level": os.getenv("REPLICATE_NANO_SAFETY", "block_only_high"),
        "allow_fallback_model": False,
    }

    payload: Dict[str, Any] = {"input": inp}

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
            pred = await post_prediction(session, REPLICATE_MODEL_SLUG, payload)
            get_url = get_prediction_get_url(pred)
            if not get_url:
                raise NanoBananaProReplicateError(f"Replicate: missing prediction get url: {pred}")
            out_url = await wait_for_result_url(session, get_url)
    except ReplicateHTTPError as e:
        raise NanoBananaProReplicateError(str(e)) from e

    out_bytes = await _download_bytes(out_url)
    ext = "png" if output_format.lower() == "png" else "jpg"
    return out_bytes, ext
