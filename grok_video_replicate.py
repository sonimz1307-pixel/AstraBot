from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

import aiohttp

from kling_flow import upload_bytes_to_supabase, KlingFlowError
from replicate_http import (
    REPLICATE_HTTP_TIMEOUT_SECONDS,
    REPLICATE_MAX_WAIT_SECONDS,
    get_prediction_get_url,
    post_prediction,
    wait_for_result_url,
)

REPLICATE_GROK_VIDEO_MODEL = (os.getenv("REPLICATE_GROK_VIDEO_MODEL") or "xai/grok-imagine-video").strip()
GROK_VIDEO_TOKENS_PER_SEC = max(1, int(os.getenv("GROK_VIDEO_TOKENS_PER_SEC", "1") or "1"))

GROK_ALLOWED_ASPECT_RATIOS = {
    "16:9", "9:16", "1:1", "4:3", "3:4", "3:2", "2:3",
}
GROK_ALLOWED_RESOLUTIONS = {"480p", "720p"}


class GrokVideoError(RuntimeError):
    pass


def normalize_grok_mode(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"image", "i2v", "image_to_video", "image2video", "image->video"}:
        return "image_to_video"
    return "text_to_video"


def normalize_grok_duration(value: Any, default: int = 5) -> int:
    try:
        out = int(value)
    except Exception:
        out = int(default)
    return max(1, min(15, out))


def normalize_grok_aspect_ratio(value: Any, default: str = "16:9") -> str:
    raw = str(value or default).strip()
    return raw if raw in GROK_ALLOWED_ASPECT_RATIOS else default


def normalize_grok_resolution(value: Any, default: str = "720p") -> str:
    raw = str(value or default).strip().lower()
    if raw in {"480", "480p"}:
        return "480p"
    if raw in {"720", "720p", ""}:
        return "720p"
    return default


def grok_tokens_for_duration(duration: Any) -> int:
    return normalize_grok_duration(duration) * int(GROK_VIDEO_TOKENS_PER_SEC)


def upload_grok_input_image(*, user_id: int, image_bytes: bytes, filename_hint: Optional[str] = None) -> str:
    if not image_bytes:
        raise GrokVideoError("Empty Grok input image")
    ext = "jpg"
    mime = "image/jpeg"
    head = bytes(image_bytes[:16])
    if head.startswith(b"\x89PNG"):
        ext = "png"
        mime = "image/png"
    elif head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        ext = "webp"
        mime = "image/webp"
    elif head.startswith(b"GIF8"):
        ext = "gif"
        mime = "image/gif"
    elif isinstance(filename_hint, str) and "." in filename_hint:
        suffix = filename_hint.rsplit(".", 1)[-1].lower().strip()
        if suffix in {"jpg", "jpeg"}:
            ext = "jpg"
            mime = "image/jpeg"
        elif suffix == "png":
            ext = "png"
            mime = "image/png"
        elif suffix == "webp":
            ext = "webp"
            mime = "image/webp"
        elif suffix == "gif":
            ext = "gif"
            mime = "image/gif"
    path = f"grok_inputs/{int(user_id)}/{int(time.time())}_{os.urandom(4).hex()}.{ext}"
    try:
        return upload_bytes_to_supabase(path, image_bytes, mime)
    except KlingFlowError as e:
        raise GrokVideoError(str(e)) from e
    except Exception as e:
        raise GrokVideoError(f"Failed to upload Grok input image: {e}") from e


async def _run_grok_prediction(input_payload: Dict[str, Any]) -> str:
    timeout = aiohttp.ClientTimeout(total=REPLICATE_HTTP_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        pred = await post_prediction(session, REPLICATE_GROK_VIDEO_MODEL, {"input": input_payload})
        get_url = get_prediction_get_url(pred)
        if not get_url:
            raise GrokVideoError(f"Replicate did not return prediction polling url: {pred}")
        try:
            return await wait_for_result_url(session, get_url, max_wait_seconds=REPLICATE_MAX_WAIT_SECONDS)
        except Exception as e:
            raise GrokVideoError(str(e)) from e


async def run_grok_text_to_video(
    *,
    prompt: str,
    duration: Any = 5,
    resolution: Any = "720p",
    aspect_ratio: Any = "16:9",
) -> str:
    clean_prompt = str(prompt or "").strip()
    if not clean_prompt:
        raise GrokVideoError("Prompt is required for Grok Text → Video")
    input_payload = {
        "prompt": clean_prompt,
        "duration": normalize_grok_duration(duration),
        "resolution": normalize_grok_resolution(resolution),
        "aspect_ratio": normalize_grok_aspect_ratio(aspect_ratio),
    }
    return await _run_grok_prediction(input_payload)


async def run_grok_image_to_video(
    *,
    user_id: int,
    image_bytes: bytes,
    prompt: str,
    duration: Any = 5,
    resolution: Any = "720p",
    aspect_ratio: Any = "16:9",
    image_url: Optional[str] = None,
    filename_hint: Optional[str] = None,
) -> str:
    clean_prompt = str(prompt or "").strip()
    if not clean_prompt:
        raise GrokVideoError("Prompt is required for Grok Image → Video")
    if not image_url:
        image_url = upload_grok_input_image(user_id=int(user_id), image_bytes=image_bytes, filename_hint=filename_hint)
    input_payload = {
        "prompt": clean_prompt,
        "image": image_url,
        "duration": normalize_grok_duration(duration),
        "resolution": normalize_grok_resolution(resolution),
        "aspect_ratio": normalize_grok_aspect_ratio(aspect_ratio),
    }
    return await _run_grok_prediction(input_payload)
