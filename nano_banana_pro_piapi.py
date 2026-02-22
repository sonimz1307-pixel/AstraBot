"""
nano_banana_pro_piapi.py

Nano Banana Pro (PiAPI / Gemini) — Image-to-Image (input.image_urls) with default 2K.

IMPORTANT:
PiAPI OpenAPI for nano-banana-pro expects input.image_urls = list of PUBLIC URLs.
So we must provide PiAPI a URL it can fetch.

This module supports 2 ways to build a public URL:
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

from typing import Any, Dict, List, Optional, Tuple, Literal
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


async def _piapi_create_task(*, prompt: str, image_urls: List[str], resolution: str = DEFAULT_RESOLUTION) -> Dict[str, Any]:
    if resolution not in ALLOWED_RESOLUTIONS:
        raise NanoBananaProError(f"resolution must be one of {ALLOWED_RESOLUTIONS}")
    if not image_urls:
        raise NanoBananaProError("image_urls is required for i2i")

    payload: Dict[str, Any] = {
        "model": "gemini",
        "task_type": "nano-banana-pro",
        "input": {
            "prompt": prompt,
            "image_urls": image_urls,
            "output_format": "png",
            "resolution": resolution,
            "safety_level": "high",
        },
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


async def _piapi_wait(task_id: str, *, timeout_s: float = 240.0, poll_s: float = 2.0) -> Dict[str, Any]:
    deadline = time.time() + timeout_s
    last: Dict[str, Any] = {}
    while time.time() < deadline:
        last = await _piapi_get_task(task_id)
        s = _status_lower(last)
        if s in ("completed", "failed", "canceled", "cancelled"):
            return last
        await _sleep(poll_s)
    raise NanoBananaProError("Timeout waiting PiAPI task")


async def _download_bytes(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=180) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.content


async def _sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)


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


# ---------- Public API used by main.py ----------

async def handle_nano_banana_pro(
    source_image_bytes: bytes,
    prompt: str,
    *,
    resolution: str = DEFAULT_RESOLUTION,   # default 2K
    output_format: str = "jpg",
    telegram_file_id: Optional[str] = None,  # preferred: gives PiAPI a proper URL
) -> Tuple[bytes, str]:
    """
    Returns (out_bytes, ext).
    """

    res = (resolution or DEFAULT_RESOLUTION).strip().upper()
    if res not in ALLOWED_RESOLUTIONS:
        res = DEFAULT_RESOLUTION

    prompt = (prompt or "").strip()
    if not prompt:
        raise NanoBananaProError("Empty prompt")

    # Build a URL PiAPI can fetch
    if telegram_file_id:
        src_url = await _tg_file_url(telegram_file_id)
    else:
        # fallback: data URL (may or may not work on PiAPI side)
        src_url = _data_url_from_bytes(source_image_bytes)

    created = await _piapi_create_task(prompt=prompt, image_urls=[src_url], resolution=res)
    task_id = ((created.get("data") or {}) if isinstance(created, dict) else {}).get("task_id")
    if not task_id:
        raise NanoBananaProError(f"PiAPI didn't return task_id: {json.dumps(created, ensure_ascii=False)[:800]}")

    done = await _piapi_wait(task_id, timeout_s=240, poll_s=2)

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

    ext = "png" if output_format.lower() == "png" else "jpg"
    return out_bytes, ext
