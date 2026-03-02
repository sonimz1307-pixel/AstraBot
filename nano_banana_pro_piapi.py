"""
nano_banana_pro_piapi.py

Nano Banana Pro (PiAPI / Gemini) — supports:
- Text-to-Image (no input.image_urls)
- Image-to-Image (input.image_urls) with default 2K

IMPORTANT:
PiAPI OpenAPI for nano-banana-pro expects input.image_urls = list of PUBLIC URLs for i2i.
For t2i, we omit image_urls entirely and provide prompt + generation params.

This module supports 2 ways to build a public URL (for i2i only):
1) Telegram File URL (recommended): provide telegram_file_id (best).
   - We call Telegram getFile API to obtain file_path
   - Then build URL: https://api.telegram.org/file/bot<TOKEN>/<file_path>
2) Data-URL fallback (base64) if you don't pass telegram_file_id.
   - Not guaranteed by spec, but can work with some backends.

Product rules you requested:
- resolutions: 1K / 2K only (default 2K)
- cost: handled in main (2 tokens) — this file only does generation.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
import os
import json
import time
import base64

import httpx


PIAPI_BASE_URL = os.getenv("PIAPI_BASE_URL", "https://api.piapi.ai").rstrip("/")
PIAPI_API_KEY = (os.getenv("PIAPI_API_KEY") or "").strip()

TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_API_BASE = os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")

ALLOWED_RESOLUTIONS = ("1K", "2K")
DEFAULT_RESOLUTION = "2K"

ALLOWED_OUTPUT_FORMATS = ("png", "jpg", "jpeg")
DEFAULT_OUTPUT_FORMAT = "png"

# PiAPI accepts aspect_ratio like "16:9", "9:16", "1:1" (depends on backend).
DEFAULT_ASPECT_RATIO = "16:9"

# PiAPI safety_level: "high"/"medium"/"low" (your example uses "high")
DEFAULT_SAFETY_LEVEL = "high"


class NanoBananaProError(RuntimeError):
    pass


def _piapi_headers() -> Dict[str, str]:
    if not PIAPI_API_KEY:
        raise NanoBananaProError("PIAPI_API_KEY is not set")
    return {"X-API-Key": PIAPI_API_KEY, "Content-Type": "application/json"}


def _status_lower(task_json: Dict[str, Any]) -> str:
    data = task_json.get("data") if isinstance(task_json, dict) else None
    s = ""
    if isinstance(data, dict):
        s = str(data.get("status") or "")
    return s.strip().lower()


def _extract_output_urls(task_json: Dict[str, Any]) -> List[str]:
    data = task_json.get("data") if isinstance(task_json, dict) else None
    if not isinstance(data, dict):
        return []
    out = data.get("output")
    if not isinstance(out, dict):
        return []
    urls: List[str] = []
    one = out.get("image_url")
    many = out.get("image_urls")
    if isinstance(one, str) and one.strip():
        urls.append(one.strip())
    if isinstance(many, list):
        for u in many:
            if isinstance(u, str) and u.strip():
                urls.append(u.strip())
    # de-dup preserve order
    seen = set()
    uniq: List[str] = []
    for u in urls:
        if u not in seen:
            uniq.append(u)
            seen.add(u)
    return uniq


def _norm_output_format(fmt: str) -> str:
    f = (fmt or DEFAULT_OUTPUT_FORMAT).strip().lower()
    if f == "jpeg":
        f = "jpg"
    if f not in ("png", "jpg"):
        f = DEFAULT_OUTPUT_FORMAT
    return f


def _norm_resolution(resolution: str) -> str:
    r = (resolution or DEFAULT_RESOLUTION).strip().upper()
    if r not in ALLOWED_RESOLUTIONS:
        r = DEFAULT_RESOLUTION
    return r


def _norm_aspect_ratio(ar: str) -> str:
    s = (ar or DEFAULT_ASPECT_RATIO).strip()
    return s or DEFAULT_ASPECT_RATIO


def _norm_safety_level(level: str) -> str:
    s = (level or DEFAULT_SAFETY_LEVEL).strip().lower()
    if s not in ("high", "medium", "low"):
        s = DEFAULT_SAFETY_LEVEL
    return s


async def _piapi_create_task(
    *,
    prompt: str,
    image_urls: Optional[List[str]] = None,
    resolution: str = DEFAULT_RESOLUTION,
    output_format: str = DEFAULT_OUTPUT_FORMAT,
    aspect_ratio: str = DEFAULT_ASPECT_RATIO,
    safety_level: str = DEFAULT_SAFETY_LEVEL,
) -> Dict[str, Any]:
    r = _norm_resolution(resolution)
    out_fmt = _norm_output_format(output_format)
    ar = _norm_aspect_ratio(aspect_ratio)
    safe = _norm_safety_level(safety_level)

    input_payload: Dict[str, Any] = {
        "prompt": prompt,
        "output_format": out_fmt,
        "resolution": r,
        "safety_level": safe,
        "aspect_ratio": ar,
    }

    # i2i only: pass image_urls when provided
    if image_urls:
        input_payload["image_urls"] = image_urls

    payload: Dict[str, Any] = {
        "model": "gemini",
        "task_type": "nano-banana-pro",
        "input": input_payload,
    }

    url = f"{PIAPI_BASE_URL}/api/v1/task"
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(url, headers=_piapi_headers(), json=payload)

    data = r.json() if (r.headers.get("content-type", "").startswith("application/json")) else {"raw": r.text}
    if r.status_code >= 400:
        raise NanoBananaProError(f"PiAPI HTTP {r.status_code}: {json.dumps(data, ensure_ascii=False)[:1500]}")
    if isinstance(data, dict) and data.get("code") not in (None, 200):
        raise NanoBananaProError(f"PiAPI error code={data.get('code')}: {json.dumps(data, ensure_ascii=False)[:1500]}")
    return data


async def _piapi_get_task(task_id: str) -> Dict[str, Any]:
    url = f"{PIAPI_BASE_URL}/api/v1/task/{task_id}"
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(url, headers=_piapi_headers())

    data = r.json() if (r.headers.get("content-type", "").startswith("application/json")) else {"raw": r.text}
    if r.status_code >= 400:
        raise NanoBananaProError(f"PiAPI HTTP {r.status_code}: {json.dumps(data, ensure_ascii=False)[:1500]}")
    if isinstance(data, dict) and data.get("code") not in (None, 200):
        raise NanoBananaProError(f"PiAPI error code={data.get('code')}: {json.dumps(data, ensure_ascii=False)[:1500]}")
    return data


async def _piapi_wait(task_id: str, *, timeout_s: float = 240.0, poll_s: float = 5.0) -> Dict[str, Any]:
    """
    Waits for PiAPI task to finish with polite polling.

    - Default poll_s=5s to reduce PiAPI rate limits (HTTP 429).
    - Uses gradual backoff up to 10s.
    - If PiAPI returns 429, backs off and retries.
    """
    import asyncio

    deadline = time.time() + float(timeout_s)
    last: Dict[str, Any] = {}

    # Start with user-provided poll_s (min 3s), then backoff gently up to 10s.
    poll = max(3.0, float(poll_s))
    poll_max = 10.0

    while time.time() < deadline:
        try:
            last = await _piapi_get_task(task_id)
        except NanoBananaProError as e:
            msg = str(e)

            # Rate limit: backoff and retry
            if "HTTP 429" in msg:
                await asyncio.sleep(poll)
                poll = min(poll + 1.0, poll_max)
                continue

            # Transient server errors / timeouts: backoff and retry
            if "HTTP 5" in msg or "Timeout" in msg:
                await asyncio.sleep(poll)
                poll = min(poll + 1.0, poll_max)
                continue

            raise

        s = _status_lower(last)
        if s in ("completed", "failed", "canceled", "cancelled"):
            return last

        await asyncio.sleep(poll)
        poll = min(poll + 0.5, poll_max)

    raise NanoBananaProError("Timeout waiting PiAPI task")


async def _download_bytes(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=180) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.content


# ---------- Telegram URL builder (recommended) ----------

async def _tg_get_file_path(file_id: str) -> str:
    """
    Calls Telegram getFile and returns file_path.
    Requires TELEGRAM_BOT_TOKEN.
    """
    if not TELEGRAM_BOT_TOKEN:
        raise NanoBananaProError("TELEGRAM_BOT_TOKEN is not set (needed to build Telegram file URL).")

    url = f"{TELEGRAM_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/getFile"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, params={"file_id": file_id})
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise NanoBananaProError(f"Telegram getFile failed: {data}")
    fp = ((data.get("result") or {}) if isinstance(data.get("result"), dict) else {}).get("file_path") or ""
    if not fp:
        raise NanoBananaProError(f"Telegram getFile: missing file_path: {data}")
    return fp


async def _tg_file_url(file_id: str) -> str:
    fp = await _tg_get_file_path(file_id)
    return f"{TELEGRAM_API_BASE}/file/bot{TELEGRAM_BOT_TOKEN}/{fp}"


def _data_url_from_bytes(img_bytes: bytes) -> str:
    # fallback; not guaranteed by spec
    b64 = base64.b64encode(img_bytes).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


# ---------- Public API used by nano_banana_pro.py ----------

async def handle_nano_banana_pro(
    source_image_bytes: Optional[bytes],
    prompt: str,
    *,
    resolution: str = DEFAULT_RESOLUTION,   # default 2K
    output_format: str = "jpg",
    aspect_ratio: str = DEFAULT_ASPECT_RATIO,
    safety_level: str = DEFAULT_SAFETY_LEVEL,
    telegram_file_id: Optional[str] = None,  # preferred for i2i: gives PiAPI a proper URL
) -> Tuple[bytes, str]:
    """
    Returns (out_bytes, ext).

    - If source_image_bytes provided -> Image→Image (uses input.image_urls)
    - If source_image_bytes is None -> Text→Image (omits image_urls)
    """
    prompt = (prompt or "").strip()
    if not prompt:
        raise NanoBananaProError("Empty prompt")

    res = _norm_resolution(resolution)
    out_fmt = _norm_output_format(output_format)

    image_urls: Optional[List[str]] = None

    # Build a URL PiAPI can fetch (ONLY when we have an input image)
    if source_image_bytes:
        if telegram_file_id:
            src_url = await _tg_file_url(telegram_file_id)
        else:
            src_url = _data_url_from_bytes(source_image_bytes)
        image_urls = [src_url]

    created = await _piapi_create_task(
        prompt=prompt,
        image_urls=image_urls,
        resolution=res,
        output_format=out_fmt,
        aspect_ratio=aspect_ratio,
        safety_level=safety_level,
    )

    task_id = ((created.get("data") or {}) if isinstance(created, dict) else {}).get("task_id")
    if not task_id:
        raise NanoBananaProError(f"PiAPI didn't return task_id: {json.dumps(created, ensure_ascii=False)[:800]}")

    done = await _piapi_wait(task_id, timeout_s=240, poll_s=5)

    if _status_lower(done) == "failed":
        err = (((done.get("data") or {}) if isinstance(done, dict) else {}) or {}).get("error") or {}
        msg = ""
        if isinstance(err, dict):
            msg = err.get("message") or ""
        raise NanoBananaProError(msg or "PiAPI task failed")

    urls = _extract_output_urls(done)
    if not urls:
        raise NanoBananaProError("PiAPI completed but no output image_url(s)")

    out_bytes = await _download_bytes(urls[0])

    # ext returned (used for downloads / naming)
    ext = "png" if out_fmt == "png" else "jpg"
    return out_bytes, ext
