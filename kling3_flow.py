import httpx
import os
import logging
from typing import Dict, Any, Optional, List

from db_supabase import supabase as sb  # service key client

# Use uvicorn logger so it shows in Render logs
ulog = logging.getLogger("uvicorn.error")

# Support multiple env var names to avoid "silent" misconfig
PIAPI_KEY = (
    os.getenv("PIAPI_API_KEY")
    or os.getenv("PIAPI_KEY")
    or os.getenv("PIAPI_TOKEN")
    or os.getenv("PIAPI_API_TOKEN")
)

BASE_URL = "https://api.piapi.ai/api/v1/task"


class Kling3Error(Exception):
    pass


def _build_headers() -> Dict[str, str]:
    if not PIAPI_KEY:
        raise Kling3Error(
            "PiAPI key is not set. Set env var PIAPI_API_KEY (preferred) or PIAPI_KEY."
        )

    # PiAPI uses x-api-key in many examples; keep it.
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
    """Upload bytes to Supabase Storage and return a public URL."""
    if sb is None:
        raise Kling3Error("Supabase client is not configured (db_supabase.supabase is None)")

    ext = (ext or "jpg").lstrip(".").lower()
    if ext not in ("jpg", "jpeg", "png", "webp"):
        ext = "jpg"

    bucket = (os.getenv("SB_MEDIA_BUCKET") or os.getenv("SUPABASE_MEDIA_BUCKET") or "media").strip() or "media"

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
    request_id: Optional[str] = None,
    # Image -> Video
    start_image_bytes: Optional[bytes] = None,
    end_image_bytes: Optional[bytes] = None,
    # Multi-shot
    multi_shots: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Create a Kling 3.0 video task in PiAPI.

    Debug changes:
    - Logs outgoing request meta and any non-2xx responses (status + body).
    - Accepts any 2xx as success (PiAPI may return 201).
    - Supports multiple env var names for PiAPI key.
    """

    _validate_inputs(int(duration), str(resolution))

    mode = "std" if str(resolution) == "720" else "pro"

    input_obj: Dict[str, Any] = {
        "version": "3.0",
        "mode": mode,
        "enable_audio": bool(enable_audio),
        "prefer_multi_shots": bool(prefer_multi_shots),
    }

    if aspect_ratio:
        input_obj["aspect_ratio"] = str(aspect_ratio)

    ms = [x for x in (multi_shots or []) if isinstance(x, dict)]
    if ms:
        _validate_multi_shots(ms)
        input_obj["multi_shots"] = ms
    else:
        input_obj["prompt"] = str(prompt or "")
        input_obj["duration"] = int(duration)

    # Frames -> upload to public URLs (this can fail BEFORE PiAPI call)
    if start_image_bytes:
        ct, ext = "image/jpeg", "jpg"
        if start_image_bytes[:8].startswith(b"\x89PNG"):
            ct, ext = "image/png", "png"
        elif start_image_bytes[:12].startswith(b"RIFF") and start_image_bytes[8:12] == b"WEBP":
            ct, ext = "image/webp", "webp"
        url = _sb_upload_bytes_public(bytes(start_image_bytes), ext=ext, content_type=ct)
        input_obj["start_image_url"] = url

    if end_image_bytes:
        ct, ext = "image/jpeg", "jpg"
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
    if request_id:
        headers["x-request-id"] = str(request_id)

    # Log the fact we are about to call PiAPI (without leaking the key)
    try:
        safe = dict(payload)
        safe_in = dict(input_obj)
        # do not log URLs in full
        if "start_image_url" in safe_in:
            safe_in["start_image_url"] = "<set>"
        if "end_image_url" in safe_in:
            safe_in["end_image_url"] = "<set>"
        safe["input"] = safe_in
        ulog.warning("PIAPI_CREATE -> %s payload=%s", BASE_URL, safe)
    except Exception:
        pass

    async with httpx.AsyncClient(timeout=120) as client:
        try:
            response = await client.post(BASE_URL, json=payload, headers=headers)
        except Exception as e:
            raise Kling3Error(f"PiAPI request failed: {e}")

    # Accept any 2xx
    if not (200 <= response.status_code < 300):
        # Log status + body so you can see EXACT reason in Render logs
        try:
            ulog.warning("PIAPI_CREATE_FAIL status=%s body=%s", response.status_code, response.text[:2000])
        except Exception:
            pass
        raise Kling3Error(f"PiAPI error {response.status_code}: {response.text}")

    try:
        data = response.json()
    except Exception:
        data = {"raw": response.text}

    try:
        ulog.warning("PIAPI_CREATE_OK status=%s data_keys=%s", response.status_code, list(data.keys()) if isinstance(data, dict) else type(data))
    except Exception:
        pass

    return data


async def get_kling3_task(task_id: str) -> Dict[str, Any]:
    headers = _build_headers()

    async with httpx.AsyncClient(timeout=60) as client:
        try:
            response = await client.get(f"{BASE_URL}/{task_id}", headers=headers)
        except Exception as e:
            raise Kling3Error(f"PiAPI request failed: {e}")

    if not (200 <= response.status_code < 300):
        try:
            ulog.warning("PIAPI_GET_FAIL status=%s body=%s", response.status_code, response.text[:2000])
        except Exception:
            pass
        raise Kling3Error(f"PiAPI error {response.status_code}: {response.text}")

    try:
        return response.json()
    except Exception:
        return {"raw": response.text}
