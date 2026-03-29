"""
nano_banana_2_piapi.py

PiAPI Gemini Nano Banana 2 client.
Supports:
- Text → Image
- Image → Image

Used by:
- Telegram bot worker_gen.py (via telegram_file_id for i2i)
- Workspace image worker (via public source_image_url)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
import base64
import json
import os
import time

import httpx

PIAPI_BASE_URL = os.getenv("PIAPI_BASE_URL", "https://api.piapi.ai").rstrip("/")
PIAPI_API_KEY = (os.getenv("PIAPI_API_KEY") or "").strip()

TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_API_BASE = os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")

ALLOWED_RESOLUTIONS = ("2K", "4K")
DEFAULT_RESOLUTION = "2K"

ALLOWED_OUTPUT_FORMATS = ("png", "jpg", "jpeg", "webp")
DEFAULT_OUTPUT_FORMAT = "jpg"
DEFAULT_ASPECT_RATIO = "9:16"
ALLOWED_ASPECT_RATIOS = {
    "1:1", "16:9", "9:16", "4:3", "3:4", "21:9", "3:2", "2:3",
    "5:4", "4:5", "1:4", "1:8", "4:1", "8:1",
}


class NanoBanana2Error(RuntimeError):
    pass


def _piapi_headers() -> Dict[str, str]:
    if not PIAPI_API_KEY:
        raise NanoBanana2Error("PIAPI_API_KEY is not set")
    return {"X-API-Key": PIAPI_API_KEY, "Content-Type": "application/json"}


def _status_lower(task_json: Dict[str, Any]) -> str:
    data = task_json.get("data") if isinstance(task_json, dict) else None
    return str((data or {}).get("status") or "").strip().lower() if isinstance(data, dict) else ""


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
    uniq: List[str] = []
    seen = set()
    for u in urls:
        if u not in seen:
            uniq.append(u)
            seen.add(u)
    return uniq


def _norm_output_format(fmt: str) -> str:
    value = (fmt or DEFAULT_OUTPUT_FORMAT).strip().lower()
    if value == "jpeg":
        value = "jpg"
    if value not in ("png", "jpg", "webp"):
        value = DEFAULT_OUTPUT_FORMAT
    return value


def _norm_resolution(resolution: str) -> str:
    value = (resolution or DEFAULT_RESOLUTION).strip().upper()
    if value not in ALLOWED_RESOLUTIONS:
        value = DEFAULT_RESOLUTION
    return value


def _norm_aspect_ratio(aspect_ratio: Optional[str], *, has_source: bool) -> Optional[str]:
    value = str(aspect_ratio or "").strip()
    if has_source:
        if not value or value == "match_input_image":
            return None
    else:
        if not value or value == "match_input_image":
            value = DEFAULT_ASPECT_RATIO
    if value not in ALLOWED_ASPECT_RATIOS:
        return DEFAULT_ASPECT_RATIO if not has_source else None
    return value


async def _piapi_create_task(
    *,
    prompt: str,
    resolution: str,
    output_format: str,
    aspect_ratio: Optional[str],
    image_urls: Optional[List[str]] = None,
) -> Dict[str, Any]:
    input_payload: Dict[str, Any] = {
        "prompt": str(prompt or "").strip(),
        "output_format": _norm_output_format(output_format),
        "resolution": _norm_resolution(resolution),
    }
    ar = _norm_aspect_ratio(aspect_ratio, has_source=bool(image_urls))
    if ar:
        input_payload["aspect_ratio"] = ar
    if image_urls:
        input_payload["image_urls"] = image_urls

    payload: Dict[str, Any] = {
        "model": "gemini",
        "task_type": "nano-banana-2",
        "input": input_payload,
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(f"{PIAPI_BASE_URL}/api/v1/task", headers=_piapi_headers(), json=payload)

    data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {"raw": response.text}
    if response.status_code >= 400:
        raise NanoBanana2Error(f"PiAPI HTTP {response.status_code}: {json.dumps(data, ensure_ascii=False)[:1500]}")
    if isinstance(data, dict) and data.get("code") not in (None, 200):
        raise NanoBanana2Error(f"PiAPI error code={data.get('code')}: {json.dumps(data, ensure_ascii=False)[:1500]}")
    return data


async def _piapi_get_task(task_id: str) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(f"{PIAPI_BASE_URL}/api/v1/task/{task_id}", headers=_piapi_headers())
    data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {"raw": response.text}
    if response.status_code >= 400:
        raise NanoBanana2Error(f"PiAPI HTTP {response.status_code}: {json.dumps(data, ensure_ascii=False)[:1500]}")
    if isinstance(data, dict) and data.get("code") not in (None, 200):
        raise NanoBanana2Error(f"PiAPI error code={data.get('code')}: {json.dumps(data, ensure_ascii=False)[:1500]}")
    return data


async def _piapi_wait(task_id: str, *, timeout_s: float = 600.0, poll_s: float = 5.0) -> Dict[str, Any]:
    import asyncio

    deadline = time.time() + float(timeout_s)
    last: Dict[str, Any] = {}
    poll = max(3.0, float(poll_s))
    poll_max = 10.0

    while time.time() < deadline:
        try:
            last = await _piapi_get_task(task_id)
        except NanoBanana2Error as e:
            msg = str(e)
            if "HTTP 429" in msg or "HTTP 5" in msg or "Timeout" in msg:
                await asyncio.sleep(poll)
                poll = min(poll + 1.0, poll_max)
                continue
            raise

        status = _status_lower(last)
        if status in {"completed", "failed", "canceled", "cancelled"}:
            return last

        await asyncio.sleep(poll)
        poll = min(poll + 0.5, poll_max)

    raise NanoBanana2Error("Timeout waiting PiAPI task")


async def _download_bytes(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=180.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.content


async def _tg_get_file_path(file_id: str) -> str:
    if not TELEGRAM_BOT_TOKEN:
        raise NanoBanana2Error("TELEGRAM_BOT_TOKEN is not set")
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(f"{TELEGRAM_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/getFile", params={"file_id": file_id})
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise NanoBanana2Error(f"Telegram getFile failed: {data}")
    file_path = str(((data.get("result") or {}) if isinstance(data.get("result"), dict) else {}).get("file_path") or "").strip()
    if not file_path:
        raise NanoBanana2Error(f"Telegram getFile: missing file_path: {data}")
    return file_path


async def _tg_file_url(file_id: str) -> str:
    file_path = await _tg_get_file_path(file_id)
    return f"{TELEGRAM_API_BASE}/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"


def _data_url_from_bytes(img_bytes: bytes) -> str:
    b64 = base64.b64encode(img_bytes).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


async def handle_nano_banana_2(
    source_image_bytes: Optional[bytes],
    prompt: str,
    *,
    resolution: str = DEFAULT_RESOLUTION,
    output_format: str = DEFAULT_OUTPUT_FORMAT,
    aspect_ratio: Optional[str] = None,
    telegram_file_id: Optional[str] = None,
    source_image_url: Optional[str] = None,
) -> Tuple[bytes, str]:
    clean_prompt = str(prompt or "").strip()
    if not clean_prompt:
        raise NanoBanana2Error("Empty prompt")

    image_urls: Optional[List[str]] = None
    ready_source_url = str(source_image_url or "").strip()
    if ready_source_url:
        image_urls = [ready_source_url]
    elif source_image_bytes:
        if telegram_file_id:
            image_urls = [await _tg_file_url(telegram_file_id)]
        else:
            image_urls = [_data_url_from_bytes(source_image_bytes)]

    created = await _piapi_create_task(
        prompt=clean_prompt,
        resolution=resolution,
        output_format=output_format,
        aspect_ratio=aspect_ratio,
        image_urls=image_urls,
    )
    task_id = str(((created.get("data") or {}) if isinstance(created, dict) else {}).get("task_id") or "").strip()
    if not task_id:
        raise NanoBanana2Error(f"PiAPI didn't return task_id: {json.dumps(created, ensure_ascii=False)[:800]}")

    done = await _piapi_wait(task_id, timeout_s=600.0, poll_s=5.0)
    if _status_lower(done) == "failed":
        err = (((done.get("data") or {}) if isinstance(done, dict) else {}) or {}).get("error") or {}
        msg = err.get("message") if isinstance(err, dict) else ""
        raise NanoBanana2Error(msg or "PiAPI task failed")

    urls = _extract_output_urls(done)
    if not urls:
        raise NanoBanana2Error("PiAPI completed but no output image_url(s)")

    out_bytes = await _download_bytes(urls[0])
    ext = "png" if _norm_output_format(output_format) == "png" else ("webp" if _norm_output_format(output_format) == "webp" else "jpg")
    return out_bytes, ext
