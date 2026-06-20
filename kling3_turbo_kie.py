from __future__ import annotations

import json
import logging
import mimetypes
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from uuid import uuid4

import httpx

from db_supabase import supabase as sb

ulog = logging.getLogger("uvicorn.error")

KIE_API_TOKEN = (
    os.getenv("KIE_API_TOKEN")
    or os.getenv("KIE_API_KEY")
    or os.getenv("KIE_TOKEN")
    or ""
).strip()
KIE_API_BASE = (os.getenv("KIE_API_BASE") or "https://api.kie.ai").strip().rstrip("/")
KIE_KLING3_TURBO_TEXT_MODEL = (os.getenv("KIE_KLING3_TURBO_TEXT_MODEL") or "kling/v3-turbo-text-to-video").strip() or "kling/v3-turbo-text-to-video"
KIE_KLING3_TURBO_IMAGE_MODEL = (os.getenv("KIE_KLING3_TURBO_IMAGE_MODEL") or "kling/v3-turbo-image-to-video").strip() or "kling/v3-turbo-image-to-video"

CREATE_PATH = "/api/v1/jobs/createTask"
STATUS_PATH = "/api/v1/jobs/recordInfo"

KLING3_TURBO_DISPLAY_NAME = "Kling 3.0 Turbo"
KLING3_TURBO_MODEL_SLUG = "kling-3.0-turbo"

_ALLOWED_RESOLUTIONS = {"720p", "1080p"}
_ALLOWED_ASPECT_RATIOS = {"16:9", "9:16", "1:1"}
_ALLOWED_DURATIONS = {5, 10, 15}

# Retail tokens fixed with the user:
# 720p: 5/10/15 sec = 6/11/17 tokens
# 1080p: 5/10/15 sec = 7/14/21 tokens
KLING3_TURBO_PRICE_TABLE: Dict[str, Dict[int, int]] = {
    "720p": {5: 6, 10: 11, 15: 17},
    "1080p": {5: 7, 10: 14, 15: 21},
}


class Kling3TurboKieError(Exception):
    pass


def _headers() -> Dict[str, str]:
    if not KIE_API_TOKEN:
        raise Kling3TurboKieError("KIE_API_TOKEN is not set")
    return {
        "Authorization": f"Bearer {KIE_API_TOKEN}",
        "Content-Type": "application/json",
    }


def normalize_kling3_turbo_mode(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"i2v", "image", "image_to_video", "image2video", "image->video", "img2vid"}:
        return "image_to_video"
    return "text_to_video"


def normalize_kling3_turbo_resolution(value: Any) -> str:
    text = str(value or "720p").strip().lower()
    if text in {"1080", "1080p", "pro"}:
        return "1080p"
    return "720p"


def normalize_kling3_turbo_duration(value: Any, *, default: int = 5) -> int:
    try:
        duration = int(float(str(value or default).strip()))
    except Exception:
        duration = int(default)
    if duration <= 5:
        return 5
    if duration <= 10:
        return 10
    return 15


def normalize_kling3_turbo_aspect_ratio(value: Any) -> str:
    text = str(value or "16:9").strip()
    return text if text in _ALLOWED_ASPECT_RATIOS else "16:9"


def calculate_kling3_turbo_price(resolution: Any, duration: Any) -> int:
    normalized_resolution = normalize_kling3_turbo_resolution(resolution)
    normalized_duration = normalize_kling3_turbo_duration(duration)
    return int(KLING3_TURBO_PRICE_TABLE[normalized_resolution][normalized_duration])


def _detect_content_type_and_ext(data: bytes, filename: Optional[str] = None, content_type: Optional[str] = None) -> Tuple[str, str]:
    ct = str(content_type or "").strip().lower()
    ext = Path(str(filename or "")).suffix.lower().lstrip(".")
    if not ct and filename:
        ct = mimetypes.guess_type(filename)[0] or ""
    if data:
        if data.startswith(b"\x89PNG"):
            return "image/png", "png"
        if data[:3] == b"\xff\xd8\xff":
            return "image/jpeg", "jpg"
        if data[:12].startswith(b"RIFF") and data[8:12] == b"WEBP":
            return "image/webp", "webp"
    if ct:
        guessed = mimetypes.guess_extension(ct) or ""
        return ct, (ext or guessed.lstrip(".") or "bin")
    return "application/octet-stream", (ext or "bin")


def upload_kling3_turbo_input_bytes(
    data: bytes,
    *,
    filename: Optional[str] = None,
    content_type: Optional[str] = None,
    prefix: str = "kling3-turbo/inputs",
) -> str:
    if not data:
        raise Kling3TurboKieError("Empty upload data")
    if sb is None:
        raise Kling3TurboKieError("Supabase client is not configured")
    ct, ext = _detect_content_type_and_ext(data, filename=filename, content_type=content_type)
    if ct == "image/webp" or ext == "webp":
        raise Kling3TurboKieError("Kling 3.0 Turbo supports only JPG/PNG input images")
    if ct not in {"image/jpeg", "image/png"}:
        raise Kling3TurboKieError("Kling 3.0 Turbo input image must be JPG or PNG")
    bucket = (os.getenv("SB_MEDIA_BUCKET") or os.getenv("SUPABASE_MEDIA_BUCKET") or "media").strip() or "media"
    safe_prefix = str(prefix or "kling3-turbo/inputs").strip("/") or "kling3-turbo/inputs"
    path = f"{safe_prefix}/{int(time.time())}_{uuid4().hex[:12]}.{ext}"
    try:
        sb.storage.from_(bucket).upload(
            path=path,
            file=data,
            file_options={"content-type": ct, "upsert": "true"},
        )
        public = sb.storage.from_(bucket).get_public_url(path)
        if isinstance(public, str):
            return public
        if isinstance(public, dict):
            url = public.get("publicUrl") or public.get("public_url") or ""
            if url:
                return str(url)
    except Exception as exc:
        raise Kling3TurboKieError(f"Supabase upload failed: {exc}")
    raise Kling3TurboKieError("Supabase upload failed: public url missing")


def _extract_task_id(payload: Dict[str, Any]) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    for key in ("taskId", "task_id", "id"):
        value = data.get(key) or payload.get(key)
        if value:
            return str(value)
    return None


def _safe_json(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            return value
    return value


def _first_nonempty(*values: Any) -> Optional[str]:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            text = value.strip()
            if text:
                return text
        elif value:
            return str(value)
    return None


def _extract_video_from_result(result: Any) -> Optional[str]:
    result = _safe_json(result)
    if isinstance(result, str):
        return result if result.startswith(("http://", "https://")) else None
    if isinstance(result, list):
        for item in result:
            found = _extract_video_from_result(item)
            if found:
                return found
        return None
    if not isinstance(result, dict):
        return None
    output = result.get("output") if isinstance(result.get("output"), dict) else {}
    candidates = [
        result.get("video_url"), result.get("videoUrl"), result.get("url"), result.get("download_url"), result.get("downloadUrl"),
        output.get("video"), output.get("video_url"), output.get("videoUrl"), output.get("url"), output.get("download_url"),
    ]
    videos = result.get("videos") or result.get("video_urls") or result.get("resultUrls") or output.get("videos")
    if isinstance(videos, list):
        candidates.extend(videos)
    return _first_nonempty(*candidates)


def normalize_kling3_turbo_task(task: Dict[str, Any]) -> Dict[str, Any]:
    data = task.get("data") if isinstance(task.get("data"), dict) else task
    if not isinstance(data, dict):
        data = {}
    state = str(data.get("state") or data.get("status") or task.get("state") or task.get("status") or "").strip().lower()
    result_json = data.get("resultJson") or data.get("result_json") or data.get("result") or data.get("response") or {}
    fail_msg = _first_nonempty(data.get("failMsg"), data.get("errorMessage"), data.get("error_message"), data.get("msg"), task.get("msg"))
    video_url = _extract_video_from_result(result_json) or _extract_video_from_result(data)
    if state in {"success", "succeeded", "completed", "complete", "done", "finish", "finished"} or video_url:
        status = "succeeded" if video_url else "processing"
    elif state in {"fail", "failed", "error", "cancel", "cancelled", "canceled"}:
        status = "failed"
    elif state in {"queue", "queued", "pending", "waiting"}:
        status = "queued"
    else:
        status = "processing"
    return {
        "task_id": _extract_task_id(task) or _first_nonempty(data.get("taskId"), data.get("task_id")),
        "status": status,
        "provider_status": state or "unknown",
        "video_url": video_url,
        "download_url": video_url,
        "output_url": video_url,
        "error_message": fail_msg,
        "finished": bool(video_url or status == "failed"),
        "raw": task,
    }


async def create_kling3_turbo_task(
    *,
    prompt: str,
    duration: int = 5,
    resolution: str = "720p",
    aspect_ratio: str = "16:9",
    mode: str = "text_to_video",
    image_url: Optional[str] = None,
    image_bytes: Optional[bytes] = None,
    image_filename: Optional[str] = None,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    normalized_mode = normalize_kling3_turbo_mode(mode)
    normalized_duration = normalize_kling3_turbo_duration(duration)
    normalized_resolution = normalize_kling3_turbo_resolution(resolution)
    normalized_aspect = normalize_kling3_turbo_aspect_ratio(aspect_ratio)
    clean_prompt = str(prompt or "").strip()[:2500]
    if not clean_prompt:
        raise Kling3TurboKieError("Prompt is required")

    if normalized_mode == "image_to_video":
        if image_bytes and not image_url:
            image_url = upload_kling3_turbo_input_bytes(image_bytes, filename=image_filename, prefix="kling3-turbo/frames")
        if not str(image_url or "").strip():
            raise Kling3TurboKieError("Image → Video requires one input image")
        model = KIE_KLING3_TURBO_IMAGE_MODEL
        input_obj: Dict[str, Any] = {
            "prompt": clean_prompt,
            "image_urls": [str(image_url).strip()],
            "duration": int(normalized_duration),
            "resolution": normalized_resolution,
        }
    else:
        model = KIE_KLING3_TURBO_TEXT_MODEL
        input_obj = {
            "prompt": clean_prompt,
            "duration": int(normalized_duration),
            "aspect_ratio": normalized_aspect,
            "resolution": normalized_resolution,
        }

    payload = {"model": model, "input": input_obj}
    headers = _headers()
    if request_id:
        headers["x-request-id"] = str(request_id)

    try:
        safe = json.loads(json.dumps(payload, ensure_ascii=False))
        if safe.get("input", {}).get("image_urls"):
            safe["input"]["image_urls"] = ["<url>" for _ in safe["input"]["image_urls"]]
        ulog.warning("KIE_KLING3_TURBO_CREATE -> %s payload=%s", f"{KIE_API_BASE}{CREATE_PATH}", safe)
    except Exception:
        pass

    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            response = await client.post(f"{KIE_API_BASE}{CREATE_PATH}", headers=headers, json=payload)
        except Exception as exc:
            raise Kling3TurboKieError(f"KIE request failed: {exc}")
    if not (200 <= response.status_code < 300):
        raise Kling3TurboKieError(f"KIE error {response.status_code}: {response.text[:2000]}")
    try:
        data = response.json()
    except Exception:
        data = {"raw": response.text}
    if isinstance(data, dict):
        code = data.get("code")
        if code not in (None, 0, 200, "0", "200"):
            raise Kling3TurboKieError(f"KIE create failed: {data}")
    task_id = _extract_task_id(data if isinstance(data, dict) else {})
    if not task_id:
        raise Kling3TurboKieError(f"KIE did not return taskId: {data}")
    return data


async def get_kling3_turbo_task(task_id: str) -> Dict[str, Any]:
    task_id = str(task_id or "").strip()
    if not task_id:
        raise Kling3TurboKieError("taskId is required")
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            response = await client.get(f"{KIE_API_BASE}{STATUS_PATH}", headers=_headers(), params={"taskId": task_id})
        except Exception as exc:
            raise Kling3TurboKieError(f"KIE status request failed: {exc}")
    if not (200 <= response.status_code < 300):
        raise Kling3TurboKieError(f"KIE status error {response.status_code}: {response.text[:2000]}")
    try:
        return response.json()
    except Exception:
        return {"raw": response.text}


async def run_kling3_turbo_task_and_wait(
    *,
    prompt: str,
    duration: int = 5,
    resolution: str = "720p",
    aspect_ratio: str = "16:9",
    mode: str = "text_to_video",
    image_url: Optional[str] = None,
    image_bytes: Optional[bytes] = None,
    image_filename: Optional[str] = None,
    timeout_sec: int = 3600,
    poll_interval_sec: float = 5.0,
    request_id: Optional[str] = None,
) -> tuple[str, Dict[str, Any], str]:
    created = await create_kling3_turbo_task(
        prompt=prompt,
        duration=duration,
        resolution=resolution,
        aspect_ratio=aspect_ratio,
        mode=mode,
        image_url=image_url,
        image_bytes=image_bytes,
        image_filename=image_filename,
        request_id=request_id,
    )
    task_id = _extract_task_id(created)
    if not task_id:
        raise Kling3TurboKieError(f"Kling 3.0 Turbo create did not return taskId: {created}")

    started = time.time()
    last: Dict[str, Any] = created
    while True:
        normalized = normalize_kling3_turbo_task(last)
        if normalized.get("status") == "succeeded" and normalized.get("video_url"):
            return task_id, last, str(normalized["video_url"])
        if normalized.get("status") == "failed":
            raise Kling3TurboKieError(normalized.get("error_message") or f"Kling 3.0 Turbo failed: {normalized.get('provider_status')}")
        if time.time() - started > timeout_sec:
            raise Kling3TurboKieError(f"Kling 3.0 Turbo timeout after {timeout_sec}s (taskId={task_id})")
        await asyncio_sleep(float(poll_interval_sec))
        last = await get_kling3_turbo_task(task_id)


async def asyncio_sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)
