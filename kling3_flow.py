import httpx
import os
import logging
import re
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


def _detect_image_ct_ext(img_bytes: bytes) -> tuple[str, str]:
    """Best-effort content-type/ext detection for common formats."""
    ct, ext = "image/jpeg", "jpg"
    if not img_bytes:
        return ct, ext
    if img_bytes[:8].startswith(b"\x89PNG"):
        return "image/png", "png"
    if img_bytes[:12].startswith(b"RIFF") and img_bytes[8:12] == b"WEBP":
        return "image/webp", "webp"
    return ct, ext


def _resolution_to_omni(resolution: str) -> str:
    """PiAPI Kling 3.0 Omni expects '720p' / '1080p'."""
    return "720p" if str(resolution) == "720" else "1080p"


def _append_frame_constraints_to_prompt(prompt: str, has_start: bool, has_end: bool) -> str:
    """
    Kling 3.0 Omni uses ref images via @image_i placeholders (i starts at 1).
    We drive first/last frames by referencing @image_1 / @image_2 in the prompt.
    """
    p = (prompt or "").strip()
    lines: List[str] = []
    if p:
        lines.append(p)

    if has_start and has_end:
        # Use both frames: start=@image_1, end=@image_2
        lines.append("Start frame MUST match @image_1.")
        lines.append("End frame MUST match @image_2.")
        lines.append("Maintain the same subject identity and composition between frames.")
    elif has_start:
        lines.append("Use @image_1 as the start frame reference. Preserve subject identity and composition.")
    elif has_end:
        # Rare case, but supported
        lines.append("Use @image_1 as the end frame reference. Move naturally toward that final frame.")

    return " ".join(lines).strip()

def _parse_multishot_prompt(prompt: str) -> Optional[List[Dict[str, Any]]]:
    """
    Parse user text into structured multi_shots list.

    Supported line formats (examples):
      SHOT 1 (3s): text...
      Shot 2 (4): text...
      SHOT 3: text...              (duration default=3)
      1) (3s) text...
      2) text...                    (duration default=3)

    Rules:
      - Detects only if >=2 shots.
      - Max 6 shots.
      - Each duration 1..14.
      - Total duration <= 15 (validated by _validate_multi_shots).
    Returns None if pattern not detected.
    """
    p = (prompt or "").strip()
    if not p:
        return None

    lines = [ln.strip() for ln in p.splitlines() if ln.strip()]
    if not lines:
        return None

    shots: List[Dict[str, Any]] = []
    pat = re.compile(
        r'^(?:shot\s*(\d+)|(\d+)[\)\.\-])\s*'     # "SHOT 1" or "1)" or "1."
        r'(?:\(\s*(\d+)\s*s?\s*\)\s*)?'           # optional "(3s)" or "(3)"
        r'[:\-]?\s*(.+)$',
        re.IGNORECASE
    )

    for ln in lines:
        m = pat.match(ln)
        if not m:
            # treat as continuation if we already started
            if shots:
                shots[-1]["prompt"] = (shots[-1]["prompt"] + " " + ln).strip()
                continue
            return None

        dur_raw = m.group(3)
        text = (m.group(4) or "").strip()
        if not text:
            continue
        dur = int(dur_raw) if dur_raw else 3
        shots.append({"prompt": text, "duration": dur})

    if len(shots) < 2:
        return None
    if len(shots) > 6:
        shots = shots[:6]

    _validate_multi_shots(shots)
    return shots


def _apply_ref_images_to_shots(
    shots: List[Dict[str, Any]],
    *,
    has_start: bool,
    has_end: bool,
) -> List[Dict[str, Any]]:
    """
    If ref images exist but user did not mention @image_i, softly anchor:
      - first shot -> @image_1 if start image exists
      - last shot  -> @image_2 (if start+end) or @image_1 (if only end)
    """
    if not shots:
        return shots

    def contains(token: str) -> bool:
        t = token.lower()
        return any(t in (s.get("prompt") or "").lower() for s in shots)

    out = [dict(s) for s in shots]

    if has_start and not contains("@image_1"):
        out[0]["prompt"] = (out[0]["prompt"] + " Use @image_1 as visual reference.").strip()

    if has_end:
        end_token = "@image_2" if has_start else "@image_1"
        if not contains(end_token):
            out[-1]["prompt"] = (out[-1]["prompt"] + f" End frame should match {end_token}.").strip()

    return out



async def create_kling3_task(
    *,
    prompt: str = "",
    duration: int = 5,
    resolution: str,
    enable_audio: bool,
    aspect_ratio: str = "16:9",
    prefer_multi_shots: bool = False,
    request_id: Optional[str] = None,
    # Image -> Video (first/last frame)
    start_image_bytes: Optional[bytes] = None,
    end_image_bytes: Optional[bytes] = None,
    # Multi-shot
    multi_shots: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Create a Kling 3.0 task in PiAPI.

    ✅ Fix for FIRST+LAST frame:
    - Base 'video_generation' task_type does not reliably support end frame.
    - We switch to Kling 3.0 Omni endpoint via:
        task_type = 'omni_video_generation'
        input.resolution = '720p' | '1080p'
    - Omni supports reference images and prompt placeholders @image_i to reference them.
      Use @image_1 for start frame and @image_2 for end frame by putting both URLs in input.images.

    Notes (per PiAPI docs):
    - If multi_shots is provided, prompt/duration are ignored for omni. Total <= 15s, max 6 shots.
    - If reference images provided, use @image_i in prompt (i starts at 1).

    Docs:
    - Kling 3.0 Omni API: https://piapi.ai/docs/kling-api/kling-3-omni-api
    """

    _validate_inputs(int(duration), str(resolution))

    input_obj: Dict[str, Any] = {
        "version": "3.0",
        "resolution": _resolution_to_omni(str(resolution)),
        "enable_audio": bool(enable_audio),
        "prefer_multi_shots": bool(prefer_multi_shots),
    }

    if aspect_ratio:
        input_obj["aspect_ratio"] = str(aspect_ratio)
    # Multi-shot
    ms = [x for x in (multi_shots or []) if isinstance(x, dict)]

    # If UI toggled prefer_multi_shots but backend didn't provide structured multi_shots,
    # try to parse them from the free-form prompt (no main.py changes required).
    if (not ms) and bool(prefer_multi_shots):
        parsed = _parse_multishot_prompt(prompt)
        if parsed:
            ms = _apply_ref_images_to_shots(parsed, has_start=bool(start_image_bytes), has_end=bool(end_image_bytes))

    if ms:
        _validate_multi_shots(ms)
        input_obj["multi_shots"] = ms
        # In omni, prompt/duration are ignored when multi_shots used (per docs).
    else:
        input_obj["duration"] = int(duration)

    # Upload reference images (order matters: @image_1, @image_2, ...)
    images: List[str] = []

    if start_image_bytes:
        ct, ext = _detect_image_ct_ext(start_image_bytes)
        start_url = _sb_upload_bytes_public(bytes(start_image_bytes), ext=ext, content_type=ct)
        images.append(start_url)

    if end_image_bytes:
        ct, ext = _detect_image_ct_ext(end_image_bytes)
        end_url = _sb_upload_bytes_public(bytes(end_image_bytes), ext=ext, content_type=ct)
        images.append(end_url)

    if images:
        # PiAPI omni uses a list of ref images, referenced by @image_i in prompt.
        input_obj["images"] = images

    # Prompt building (single-shot)
    if not ms:
        # If we have ref images, steer first/last frames via @image_i.
        input_obj["prompt"] = _append_frame_constraints_to_prompt(
            prompt=prompt,
            has_start=bool(start_image_bytes),
            has_end=bool(end_image_bytes),
        )
    else:
        # In multi-shot mode: do NOT overwrite shot prompts.
        # But for debugging, we can warn if end frame exists but isn't referenced anywhere.
        pass

    payload = {
        "model": "kling",
        "task_type": "omni_video_generation",
        "input": input_obj,
        "config": {"service_mode": "public"},
    }

    headers = _build_headers()
    if request_id:
        headers["x-request-id"] = str(request_id)

    # Log outgoing meta (no secrets)
    try:
        safe = {"model": payload["model"], "task_type": payload["task_type"], "config": payload["config"]}
        safe_in = dict(input_obj)
        if "images" in safe_in:
            safe_in["images"] = [f"<set:{len(images)}>" for _ in safe_in.get("images", [])]
        safe["input"] = safe_in
        ulog.warning("PIAPI_CREATE -> %s payload=%s", BASE_URL, safe)
        ulog.warning("Kling3Omni refs: start=%s end=%s images_count=%s", bool(start_image_bytes), bool(end_image_bytes), len(images))
    except Exception:
        pass

    async with httpx.AsyncClient(timeout=120) as client:
        try:
            response = await client.post(BASE_URL, json=payload, headers=headers)
        except Exception as e:
            raise Kling3Error(f"PiAPI request failed: {e}")

    if not (200 <= response.status_code < 300):
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
