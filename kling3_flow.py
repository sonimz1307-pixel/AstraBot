import httpx
import os
from typing import Dict, Any, Optional, List

from db_supabase import supabase as sb  # service key client

PIAPI_KEY = os.getenv("PIAPI_API_KEY")

BASE_URL = "https://api.piapi.ai/api/v1/task"


class Kling3Error(Exception):
    pass


def _build_headers() -> Dict[str, str]:
    if not PIAPI_KEY:
        raise Kling3Error("PIAPI_API_KEY is not set")

    return {
        "x-api-key": PIAPI_KEY,
        "Content-Type": "application/json",
    }


def _validate_inputs(duration: int, resolution: str):
    if duration < 3 or duration > 15:
        raise Kling3Error("duration must be between 3 and 15 seconds")
    if str(resolution) not in ("720", "1080"):
        raise Kling3Error("resolution must be '720' or '1080'")


def _validate_multi_shots(multi_shots: List[Dict[str, Any]]) -> int:
    if not isinstance(multi_shots, list) or not multi_shots:
        raise Kling3Error("multi_shots must be a non-empty list")
    if len(multi_shots) > 6:
        raise Kling3Error("Maximum 6 multi_shots allowed")

    total = 0
    for i, shot in enumerate(multi_shots, start=1):
        if not isinstance(shot, dict):
            raise Kling3Error(f"multi_shots[{i}] must be an object")
        p = (shot.get("prompt") or "").strip()
        if not p:
            raise Kling3Error(f"multi_shots[{i}].prompt is required")
        d = int(shot.get("duration") or 3)
        if d < 1 or d > 14:
            raise Kling3Error(f"multi_shots[{i}].duration must be 1..14")
        total += d

    if total > 15:
        raise Kling3Error("Total duration of multi_shots should not exceed 15 seconds")
    return total

def _sb_upload_bytes_public(data: bytes, *, ext: str, content_type: str) -> str:
    """Upload bytes to Supabase Storage and return a public URL.

    Requirements:
    - Supabase Storage bucket must exist and be public (or signed URLs logic added).
    - Uses storage3; must pass raw bytes (not BytesIO).
    """
    if sb is None:
        raise Kling3Error("Supabase client is not configured (db_supabase.supabase is None)")

    ext = (ext or "jpg").lstrip(".").lower()
    if ext not in ("jpg", "jpeg", "png", "webp"):
        ext = "jpg"

    # Bucket name: try env, else fallback
    bucket = (os.getenv("SB_MEDIA_BUCKET") or os.getenv("SUPABASE_MEDIA_BUCKET") or "media").strip() or "media"

    # deterministic-ish path (avoid collisions, but keep short)
    import time
    from uuid import uuid4
    fn = f"kling3/{int(time.time())}_{uuid4().hex[:10]}.{ext}"

    try:
        sb.storage.from_(bucket).upload(
            path=fn,
            file=data,
            file_options={"content-type": content_type, "upsert": "true"},
        )
        public = sb.storage.from_(bucket).get_public_url(fn)
        if isinstance(public, str):
            return public
        # storage3 sometimes returns dict-like
        url = (public.get("publicUrl") if isinstance(public, dict) else None) or ""
        if not url:
            raise Kling3Error("Supabase storage: could not get public url")
        return str(url)
    except Exception as e:
        raise Kling3Error(f"Supabase upload failed: {e}")


async def create_kling3_task(
    *,
    prompt: str = "",
    duration: int = 5,
    resolution: str,
    enable_audio: bool,
    aspect_ratio: str = "16:9",
    prefer_multi_shots: bool = False,
    # Image -> Video
    start_image_bytes: Optional[bytes] = None,
    end_image_bytes: Optional[bytes] = None,
    # Multi-shot
    multi_shots: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Create a Kling 3.0 video task in PiAPI.

    - If multi_shots provided (non-empty), PiAPI ignores prompt+duration.
    - start/end frames are uploaded to Supabase Storage to obtain public URLs.
    """

    _validate_inputs(int(duration), str(resolution))

    mode = "std" if str(resolution) == "720" else "pro"

    input_obj: Dict[str, Any] = {
        "version": "3.0",
        "mode": mode,
        "enable_audio": bool(enable_audio),
        "prefer_multi_shots": bool(prefer_multi_shots),
    }

    # aspect_ratio is ignored if start image provided (per docs), but safe to include
    if aspect_ratio:
        input_obj["aspect_ratio"] = str(aspect_ratio)

    # Multi-shots
    ms = [x for x in (multi_shots or []) if isinstance(x, dict)]
    if ms:
        _validate_multi_shots(ms)
        input_obj["multi_shots"] = ms
    else:
        # Text-to-video
        input_obj["prompt"] = str(prompt or "")
        input_obj["duration"] = int(duration)

    # Image frames (upload to public URLs)
    if start_image_bytes:
        # naive content type detection
        ct = "image/jpeg"
        ext = "jpg"
        if start_image_bytes[:8].startswith(b"\x89PNG"):
            ct, ext = "image/png", "png"
        elif start_image_bytes[:12].startswith(b"RIFF") and start_image_bytes[8:12] == b"WEBP":
            ct, ext = "image/webp", "webp"
        url = _sb_upload_bytes_public(bytes(start_image_bytes), ext=ext, content_type=ct)
        input_obj["start_image_url"] = url

    if end_image_bytes:
        ct = "image/jpeg"
        ext = "jpg"
        if end_image_bytes[:8].startswith(b"\x89PNG"):
            ct, ext = "image/png", "png"
        elif end_image_bytes[:12].startswith(b"RIFF") and end_image_bytes[8:12] == b"WEBP":
            ct, ext = "image/webp", "webp"
        url = _sb_upload_bytes_public(bytes(end_image_bytes), ext=ext, content_type=ct)
        input_obj["end_image_url"] = url

    payload = {
        "model": "kling",
        "task_type": "video_generation",
        "input": input_obj,
        "config": {"service_mode": "public"},
    }

    headers = _build_headers()

    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(BASE_URL, json=payload, headers=headers)

    if response.status_code != 200:
        raise Kling3Error(response.text)

    return response.json()


async def get_kling3_task(task_id: str) -> Dict[str, Any]:
    headers = _build_headers()

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.get(f"{BASE_URL}/{task_id}", headers=headers)

    if response.status_code != 200:
        raise Kling3Error(response.text)

    return response.json()
