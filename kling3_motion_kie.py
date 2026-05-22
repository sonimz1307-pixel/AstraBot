from __future__ import annotations

import asyncio
import json
import math
import mimetypes
import os
import subprocess
import tempfile
import time
from typing import Any, Dict, Optional, Tuple
from uuid import uuid4

import httpx

from billing_db import ensure_user_row, get_balance, hold_tokens_for_kling, confirm_kling_job, rollback_kling_job
from kling3_kie_flow import (
    Kling3KieError,
    extract_kling3_kie_task_id,
    get_kling3_kie_task,
    normalize_kling3_kie_task,
    upload_kling3_kie_input_bytes,
)
from video_duration import get_duration_seconds


KIE_API_TOKEN = (
    os.getenv("KIE_API_TOKEN")
    or os.getenv("KIE_API_KEY")
    or os.getenv("KIE_TOKEN")
    or ""
).strip()
KIE_API_BASE = (os.getenv("KIE_API_BASE") or "https://api.kie.ai").strip().rstrip("/")
KIE_FILE_UPLOAD_BASE = (os.getenv("KIE_FILE_UPLOAD_BASE") or "https://kieai.redpandaai.co").strip().rstrip("/")
KIE_FILE_STREAM_UPLOAD_PATH = (os.getenv("KIE_FILE_STREAM_UPLOAD_PATH") or "/api/file-stream-upload").strip() or "/api/file-stream-upload"
KIE_KLING3_MOTION_MODEL = (os.getenv("KIE_KLING3_MOTION_MODEL") or "kling-3.0/motion-control").strip() or "kling-3.0/motion-control"

CREATE_PATH = "/api/v1/jobs/createTask"

# Розничная цена проекта. Можно переопределить в Render ENV без правки кода.
KLING3_MOTION_720_TOKENS_PER_SEC = float(os.getenv("KLING3_MOTION_720_TOKENS_PER_SEC", "2") or "2")
KLING3_MOTION_1080_TOKENS_PER_SEC = float(os.getenv("KLING3_MOTION_1080_TOKENS_PER_SEC", "3") or "3")


def _kie_upload_headers() -> Dict[str, str]:
    if not KIE_API_TOKEN:
        raise Kling3MotionKieError("KIE_API_TOKEN is not set")
    return {"Authorization": f"Bearer {KIE_API_TOKEN}"}


def _detect_upload_content_type_and_ext(data: bytes, *, filename: Optional[str] = None, content_type: Optional[str] = None) -> Tuple[str, str]:
    """Detect only formats accepted by Kling 3.0 Motion Control."""
    name = str(filename or "").strip()
    ext = os.path.splitext(name)[1].lower().lstrip(".")
    ct = str(content_type or "").split(";", 1)[0].strip().lower()
    if not ct and name:
        ct = (mimetypes.guess_type(name)[0] or "").lower()

    if data.startswith(b"\x89PNG"):
        return "image/png", "png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg", "jpg"
    if data[4:8] == b"ftyp":
        # MP4/MOV containers both use ftyp. Prefer the original extension/content-type when it is known.
        if ext == "mov" or ct == "video/quicktime":
            return "video/quicktime", "mov"
        return "video/mp4", "mp4"

    if ct in {"image/jpeg", "image/png", "video/mp4", "video/quicktime"}:
        guessed = mimetypes.guess_extension(ct) or ""
        return ct, ext or guessed.lstrip(".") or ("jpg" if ct == "image/jpeg" else "bin")

    raise Kling3MotionKieError("Kling 3.0 Motion Control supports only JPG/PNG image and MP4/MOV video files")


def _billable_motion_seconds(value: Any) -> int:
    """
    Browser players often display 6s for files with metadata like 6.03s.
    Use a small tolerance so 6.01–6.25 is billed as 6, while 6.8 is billed as 7.
    """
    try:
        seconds = float(value or 0)
    except Exception:
        seconds = 0.0
    if seconds <= 0:
        return 0
    return max(1, int(math.ceil(seconds - 0.25)))


def _probe_video_duration_float(video_bytes: bytes, *, suffix: str = ".mp4") -> float:
    if not video_bytes:
        return 0.0
    ffprobe = os.getenv("FFPROBE_BIN", "ffprobe")
    tmp_path = ""
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as fh:
        tmp_path = fh.name
        fh.write(video_bytes)
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", tmp_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:
            return 0.0
        data = json.loads(proc.stdout or "{}")
        return float((data.get("format") or {}).get("duration") or 0.0)
    except Exception:
        return 0.0
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass


async def upload_kie_file_stream_bytes(
    data: bytes,
    *,
    filename: str,
    content_type: Optional[str] = None,
    upload_path: str = "kling3-motion/inputs",
) -> str:
    """Upload to KIE temporary file storage and return a KIE-hosted URL accepted by createTask."""
    if not data:
        raise Kling3MotionKieError("Empty upload data")
    ct, ext = _detect_upload_content_type_and_ext(data, filename=filename, content_type=content_type)
    base_name = os.path.splitext(os.path.basename(str(filename or "file")))[0] or "file"
    safe_name = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in base_name)[:48] or "file"
    final_name = f"{safe_name}_{int(time.time())}_{uuid4().hex[:8]}.{ext}"

    url = f"{KIE_FILE_UPLOAD_BASE}{KIE_FILE_STREAM_UPLOAD_PATH if KIE_FILE_STREAM_UPLOAD_PATH.startswith('/') else '/' + KIE_FILE_STREAM_UPLOAD_PATH}"
    form_data = {"uploadPath": str(upload_path or "kling3-motion/inputs").strip("/"), "fileName": final_name}
    files = {"file": (final_name, data, ct)}
    async with httpx.AsyncClient(timeout=240.0) as client:
        try:
            resp = await client.post(url, headers=_kie_upload_headers(), data=form_data, files=files)
        except Exception as exc:
            raise Kling3MotionKieError(f"KIE file upload request failed: {exc}")
    if not (200 <= resp.status_code < 300):
        raise Kling3MotionKieError(f"KIE file upload error {resp.status_code}: {resp.text[:1000]}")
    try:
        payload = resp.json()
    except Exception:
        raise Kling3MotionKieError(f"KIE file upload returned non-JSON response: {resp.text[:300]}")
    if isinstance(payload, dict):
        code = payload.get("code")
        success = payload.get("success")
        if code not in (None, 0, 200, "0", "200") and success is not True:
            raise Kling3MotionKieError(f"KIE file upload failed: {payload}")
        info = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        file_url = str(info.get("downloadUrl") or info.get("fileUrl") or info.get("url") or "").strip()
        if file_url.startswith(("http://", "https://")):
            return file_url
    raise Kling3MotionKieError(f"KIE file upload did not return file URL: {payload}")

class Kling3MotionKieError(RuntimeError):
    pass


def _headers() -> Dict[str, str]:
    if not KIE_API_TOKEN:
        raise Kling3MotionKieError("KIE_API_TOKEN is not set")
    return {
        "Authorization": f"Bearer {KIE_API_TOKEN}",
        "Content-Type": "application/json",
    }


def normalize_kling3_motion_resolution(value: Any) -> str:
    text = str(value or "720p").strip().lower()
    if text in {"1080", "1080p", "pro", "professional", "hd"}:
        return "1080p"
    return "720p"


def kling3_motion_provider_mode(value: Any) -> str:
    # KIE API для kling-3.0/motion-control принимает качество именно как 720p/1080p.
    return normalize_kling3_motion_resolution(value)


def kling3_motion_billing_mode(value: Any) -> str:
    # Внутренний биллинг проекта исторически использует std/pro.
    return "pro" if normalize_kling3_motion_resolution(value) == "1080p" else "std"


def normalize_kling3_motion_character_orientation(value: Any) -> str:
    text = str(value or "video").strip().lower()
    return "image" if text == "image" else "video"


def normalize_kling3_motion_seconds(value: Any, *, character_orientation: str = "video") -> int:
    try:
        seconds = int(math.ceil(float(value or 0)))
    except Exception:
        seconds = 0
    max_seconds = 10 if normalize_kling3_motion_character_orientation(character_orientation) == "image" else 30
    if seconds < 3:
        raise Kling3MotionKieError("Kling 3.0 Motion Control requires reference video duration 3–30 seconds")
    if seconds > max_seconds:
        raise Kling3MotionKieError(f"Kling 3.0 Motion Control with character_orientation={character_orientation} supports up to {max_seconds} seconds")
    return seconds


def calculate_kling3_motion_tokens(resolution: Any, seconds: int) -> int:
    normalized = normalize_kling3_motion_resolution(resolution)
    rate = KLING3_MOTION_1080_TOKENS_PER_SEC if normalized == "1080p" else KLING3_MOTION_720_TOKENS_PER_SEC
    return max(1, int(math.ceil(float(seconds) * float(rate))))


def _extract_video_url(payload: Dict[str, Any]) -> Optional[str]:
    normalized = normalize_kling3_kie_task(payload if isinstance(payload, dict) else {})
    return str(normalized.get("video_url") or normalized.get("output_url") or "").strip() or None


async def create_kling3_motion_kie_task(
    *,
    prompt: str,
    image_url: str,
    video_url: str,
    resolution: Any = "720p",
    character_orientation: str = "video",
    background_source: str = "input_video",
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    image_url = str(image_url or "").strip()
    video_url = str(video_url or "").strip()
    if not image_url:
        raise Kling3MotionKieError("Kling 3.0 Motion Control requires one character image")
    if not video_url:
        raise Kling3MotionKieError("Kling 3.0 Motion Control requires one reference motion video")

    input_obj: Dict[str, Any] = {
        "prompt": str(prompt or "").strip()[:2500],
        "input_urls": [image_url],
        "video_urls": [video_url],
        "character_orientation": normalize_kling3_motion_character_orientation(character_orientation),
        "mode": kling3_motion_provider_mode(resolution),
    }
    if background_source:
        input_obj["background_source"] = str(background_source).strip()

    payload = {"model": KIE_KLING3_MOTION_MODEL, "input": input_obj}
    headers = _headers()
    if request_id:
        headers["x-request-id"] = str(request_id)

    try:
        safe = json.loads(json.dumps(payload, ensure_ascii=False))
        safe["input"]["input_urls"] = ["<url>"]
        safe["input"]["video_urls"] = ["<url>"]
        print(f"[kling3_motion_kie] create payload={safe}", flush=True)
    except Exception:
        pass

    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            response = await client.post(f"{KIE_API_BASE}{CREATE_PATH}", headers=headers, json=payload)
        except Exception as exc:
            raise Kling3MotionKieError(f"KIE request failed: {exc}")
    if not (200 <= response.status_code < 300):
        raise Kling3MotionKieError(f"KIE error {response.status_code}: {response.text[:2000]}")
    try:
        data = response.json()
    except Exception:
        data = {"raw": response.text}
    if isinstance(data, dict):
        code = data.get("code")
        if code not in (None, 0, 200, "0", "200"):
            raise Kling3MotionKieError(f"KIE create failed: {data}")
    task_id = extract_kling3_kie_task_id(data if isinstance(data, dict) else {})
    if not task_id:
        raise Kling3MotionKieError(f"KIE did not return taskId: {data}")
    return data


async def run_kling3_motion_kie_task_and_wait(
    *,
    prompt: str,
    image_url: str,
    video_url: str,
    resolution: Any = "720p",
    character_orientation: str = "video",
    background_source: str = "input_video",
    poll_interval_sec: float = 5.0,
    timeout_sec: int = 2400,
) -> Tuple[str, Dict[str, Any], str]:
    created = await create_kling3_motion_kie_task(
        prompt=prompt,
        image_url=image_url,
        video_url=video_url,
        resolution=resolution,
        character_orientation=character_orientation,
        background_source=background_source,
    )
    task_id = extract_kling3_kie_task_id(created)
    if not task_id:
        raise Kling3MotionKieError(f"KIE create did not return taskId: {created}")

    loop = asyncio.get_event_loop()
    started = loop.time()
    last: Dict[str, Any] = {}
    while True:
        try:
            last = await get_kling3_kie_task(task_id)
        except Kling3KieError as exc:
            raise Kling3MotionKieError(f"KIE status failed: {exc}")
        normalized = normalize_kling3_kie_task(last)
        if normalized.get("status") == "failed":
            raise Kling3MotionKieError(normalized.get("error_message") or f"KIE task failed: {normalized.get('provider_status')}")
        video_out = _extract_video_url(last)
        if video_out and normalized.get("finished"):
            return str(task_id), last, video_out
        if (loop.time() - started) >= float(timeout_sec):
            raise Kling3MotionKieError(f"Kling 3.0 Motion Control timeout after {timeout_sec}s (taskId={task_id})")
        await asyncio.sleep(float(poll_interval_sec))


async def run_kling3_motion_kie_from_bytes(
    *,
    user_id: int,
    avatar_bytes: bytes,
    motion_video_bytes: bytes,
    prompt: str,
    resolution: Any = "720p",
    character_orientation: str = "video",
    duration_seconds: Optional[int] = None,
    bill_user: bool = True,
    billing_meta: Optional[Dict[str, Any]] = None,
) -> str:
    if not avatar_bytes:
        raise Kling3MotionKieError("Kling 3.0 Motion Control requires character image bytes")
    if not motion_video_bytes:
        raise Kling3MotionKieError("Kling 3.0 Motion Control requires reference video bytes")

    seconds = int(duration_seconds or 0)
    if seconds <= 0:
        seconds = _billable_motion_seconds(_probe_video_duration_float(motion_video_bytes, suffix=".mp4"))
        if seconds <= 0:
            seconds = int(get_duration_seconds(video_bytes=motion_video_bytes, suffix=".mp4") or 0)
    seconds = normalize_kling3_motion_seconds(seconds, character_orientation=character_orientation)
    normalized_resolution = normalize_kling3_motion_resolution(resolution)
    tokens_cost = calculate_kling3_motion_tokens(normalized_resolution, seconds)
    provider_mode = kling3_motion_provider_mode(normalized_resolution)
    billing_mode = kling3_motion_billing_mode(normalized_resolution)

    job_id: Optional[str] = None
    if bill_user:
        ensure_user_row(user_id)
        balance = int(get_balance(user_id) or 0)
        if balance < tokens_cost:
            raise Kling3MotionKieError(
                f"Недостаточно токенов. Нужно: {tokens_cost}, баланс: {balance}. "
                f"(Видео: {seconds} сек, качество: {normalized_resolution})"
            )
        job_id = hold_tokens_for_kling(
            telegram_user_id=user_id,
            seconds=seconds,
            mode=billing_mode,
            tokens_cost=tokens_cost,
            meta={
                "provider": "kie",
                "model": "kling-3.0/motion-control",
                "resolution": normalized_resolution,
                "tokens_per_second": KLING3_MOTION_1080_TOKENS_PER_SEC if normalized_resolution == "1080p" else KLING3_MOTION_720_TOKENS_PER_SEC,
                "provider_mode": provider_mode,
                "billing_mode": billing_mode,
                **(billing_meta or {}),
            },
        )

    try:
        # Motion Control 3.0 is stricter than the regular Kling 3.0 flow: KIE expects
        # temporary file URLs produced by its File Upload API. Supabase public URLs can
        # be rejected on createTask with: {code: 422, msg: "file format not support"}.
        image_url = await upload_kie_file_stream_bytes(
            avatar_bytes,
            filename=f"kling3_motion_avatar_{int(time.time())}.jpg",
            content_type="image/jpeg",
            upload_path="kling3-motion/images",
        )
        video_url = await upload_kie_file_stream_bytes(
            motion_video_bytes,
            filename=f"kling3_motion_ref_{int(time.time())}.mp4",
            content_type="video/mp4",
            upload_path="kling3-motion/videos",
        )
        task_id, raw_task, out_url = await run_kling3_motion_kie_task_and_wait(
            prompt=prompt,
            image_url=image_url,
            video_url=video_url,
            resolution=normalized_resolution,
            character_orientation=character_orientation,
        )
        if bill_user and job_id:
            confirm_kling_job(
                job_id,
                out_url=out_url,
                meta={
                    "seconds": seconds,
                    "mode": billing_mode,
                    "provider_mode": provider_mode,
                    "resolution": normalized_resolution,
                    "tokens_cost": tokens_cost,
                    "task_id": task_id,
                    "model": "kling-3.0/motion-control",
                },
            )
        return out_url
    except Exception as exc:
        if bill_user and job_id:
            try:
                rollback_kling_job(job_id, error=str(exc))
            except Exception:
                pass
        raise
