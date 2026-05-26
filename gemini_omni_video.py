from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict, List, Optional, Sequence

import aiohttp

from kling_flow import upload_bytes_to_supabase, KlingFlowError

KIE_API_BASE = (os.getenv("KIE_API_BASE") or "https://api.kie.ai").rstrip("/")
KIE_API_TOKEN = (os.getenv("KIE_API_TOKEN") or os.getenv("KIE_API_KEY") or "").strip()
KIE_OMNI_CALLBACK_URL = (os.getenv("KIE_OMNI_CALLBACK_URL") or "").strip()
KIE_OMNI_MAX_WAIT_SECONDS = float(os.getenv("KIE_OMNI_MAX_WAIT_SECONDS", "1800") or "1800")
KIE_OMNI_POLL_SECONDS = float(os.getenv("KIE_OMNI_POLL_SECONDS", "6") or "6")
KIE_OMNI_ALLOWED_DURATIONS = (6, 8, 10)
KIE_OMNI_ALLOWED_ASPECT_RATIOS = ("16:9", "9:16")
KIE_OMNI_ALLOWED_RESOLUTIONS = ("720p", "1080p", "4k")
KIE_OMNI_VIDEO_EDIT_MAX_DURATION_SEC = int(os.getenv("KIE_OMNI_VIDEO_EDIT_MAX_DURATION_SEC", "60") or "60")
# Business pricing map. Can be overridden via env later.
KIE_OMNI_TOKEN_MAP = {
    "720p": {6: 8, 8: 10, 10: 12},
    "1080p": {6: 8, 8: 10, 10: 12},
    "4k": {6: 16, 8: 18, 10: 20},
}
# Fixed retail price for Gemini Omni video input / Video Edit. KIE bills video input per task.
KIE_OMNI_VIDEO_EDIT_TOKEN_MAP = {
    "720p": int(os.getenv("KIE_OMNI_VIDEO_EDIT_720P_TOKENS", "20") or "20"),
    "1080p": int(os.getenv("KIE_OMNI_VIDEO_EDIT_1080P_TOKENS", "20") or "20"),
    "4k": int(os.getenv("KIE_OMNI_VIDEO_EDIT_4K_TOKENS", "30") or "30"),
}


class GeminiOmniVideoError(RuntimeError):
    pass


def normalize_gemini_omni_mode(value: Any, default: str = "text_to_video") -> str:
    raw = str(value or default).strip().lower()
    if raw in {"image", "image_to_video", "i2v", "image2video", "image->video", "reference"}:
        return "image_to_video"
    if raw in {"video", "video_edit", "video_to_video", "v2v", "video2video", "video->video", "edit", "edit_video"}:
        return "video_edit"
    return "text_to_video"


def normalize_gemini_omni_duration(value: Any, default: int = 8) -> int:
    try:
        out = int(value)
    except Exception:
        out = int(default)
    if out <= KIE_OMNI_ALLOWED_DURATIONS[0]:
        return KIE_OMNI_ALLOWED_DURATIONS[0]
    if out >= KIE_OMNI_ALLOWED_DURATIONS[-1]:
        return KIE_OMNI_ALLOWED_DURATIONS[-1]
    return min(KIE_OMNI_ALLOWED_DURATIONS, key=lambda item: (abs(item - out), item))


def normalize_gemini_omni_aspect_ratio(value: Any, default: str = "16:9") -> str:
    raw = str(value or default).strip()
    return raw if raw in KIE_OMNI_ALLOWED_ASPECT_RATIOS else default


def normalize_gemini_omni_resolution(value: Any, default: str = "1080p") -> str:
    raw = str(value or default).strip().lower()
    if raw in {"720", "720p"}:
        return "720p"
    if raw in {"1080", "1080p", "fhd", "fullhd"}:
        return "1080p"
    if raw in {"4k", "4K".lower(), "uhd", "2160", "2160p"}:
        return "4k"
    return default


def gemini_omni_tokens_for_duration(duration: Any, resolution: Any = "1080p") -> int:
    d = normalize_gemini_omni_duration(duration)
    r = normalize_gemini_omni_resolution(resolution)
    return int(KIE_OMNI_TOKEN_MAP[r][d])


def gemini_omni_video_edit_tokens(resolution: Any = "1080p") -> int:
    r = normalize_gemini_omni_resolution(resolution)
    return int(KIE_OMNI_VIDEO_EDIT_TOKEN_MAP[r])


def gemini_omni_tokens_for_run(mode: Any = "text_to_video", duration: Any = 8, resolution: Any = "1080p") -> int:
    normalized_mode = normalize_gemini_omni_mode(mode)
    if normalized_mode == "video_edit":
        return gemini_omni_video_edit_tokens(resolution)
    return gemini_omni_tokens_for_duration(duration, resolution)


def upload_gemini_omni_input_image(*, user_id: int, image_bytes: bytes, filename_hint: Optional[str] = None, slot: str = "ref") -> str:
    if not image_bytes:
        raise GeminiOmniVideoError("Empty Gemini Omni image")
    ext = "jpg"
    mime = "image/jpeg"
    head = bytes(image_bytes[:16])
    if head.startswith(b"\x89PNG"):
        ext = "png"
        mime = "image/png"
    elif head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        ext = "webp"
        mime = "image/webp"
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
    path = f"gemini_omni_inputs/{int(user_id)}/{slot}_{int(time.time())}_{os.urandom(4).hex()}.{ext}"
    try:
        return upload_bytes_to_supabase(path, image_bytes, mime)
    except KlingFlowError as e:
        raise GeminiOmniVideoError(str(e)) from e
    except Exception as e:
        raise GeminiOmniVideoError(f"Failed to upload Gemini Omni image: {e}") from e


def _auth_headers() -> Dict[str, str]:
    if not KIE_API_TOKEN:
        raise GeminiOmniVideoError("KIE API token is not configured. Set KIE_API_TOKEN or KIE_API_KEY.")
    return {"Authorization": f"Bearer {KIE_API_TOKEN}", "Content-Type": "application/json"}


async def _kie_request_json(session: aiohttp.ClientSession, method: str, path: str, *, params: Optional[Dict[str, Any]] = None, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"{KIE_API_BASE}{path}"
    async with session.request(method.upper(), url, headers=_auth_headers(), params=params, json=payload) as resp:
        text = await resp.text()
        try:
            data = json.loads(text) if text else {}
        except Exception:
            data = {"raw": text}
        if resp.status >= 400:
            detail = data.get("msg") or data.get("message") or data.get("error") or text or f"HTTP {resp.status}"
            raise GeminiOmniVideoError(f"KIE request failed ({resp.status}): {detail}")
        if isinstance(data, dict):
            code = str(data.get("code") or "200")
            msg = str(data.get("msg") or data.get("message") or "").strip().lower()
            if code not in {"0", "200"} and msg != "success":
                detail = data.get("msg") or data.get("message") or data.get("error") or data
                raise GeminiOmniVideoError(f"KIE API error: {detail}")
        return data if isinstance(data, dict) else {"data": data}


def _extract_kie_task_id(create_response: Dict[str, Any]) -> str:
    data = create_response.get("data") if isinstance(create_response, dict) else None
    if isinstance(data, dict):
        for key in ("taskId", "task_id", "id"):
            value = str(data.get(key) or "").strip()
            if value:
                return value
    for key in ("taskId", "task_id", "id"):
        value = str(create_response.get(key) or "").strip() if isinstance(create_response, dict) else ""
        if value:
            return value
    return ""


def _extract_video_url_from_result(result: Any) -> Optional[str]:
    if isinstance(result, str):
        raw = result.strip()
        if raw.startswith("{") or raw.startswith("["):
            try:
                return _extract_video_url_from_result(json.loads(raw))
            except Exception:
                pass
        if raw.startswith("http://") or raw.startswith("https://"):
            return raw
        return None
    if isinstance(result, list):
        for item in result:
            found = _extract_video_url_from_result(item)
            if found:
                return found
        return None
    if isinstance(result, dict):
        for key in ("videoUrl", "video_url", "resultUrl", "result_url", "url", "downloadUrl", "download_url"):
            found = _extract_video_url_from_result(result.get(key))
            if found:
                return found
        for key in ("resultUrls", "result_urls", "videos", "urls", "output"):
            found = _extract_video_url_from_result(result.get(key))
            if found:
                return found
        for value in result.values():
            found = _extract_video_url_from_result(value)
            if found:
                return found
    return None


async def _poll_kie_task(session: aiohttp.ClientSession, task_id: str) -> str:
    start_ts = time.monotonic()
    last_state = ""
    last_detail = ""
    while True:
        payload = await _kie_request_json(session, "GET", "/api/v1/jobs/recordInfo", params={"taskId": task_id})
        data = payload.get("data") if isinstance(payload, dict) else None
        data = data if isinstance(data, dict) else {}
        state = str(data.get("state") or data.get("status") or "").strip().lower()
        if state:
            last_state = state
        if state == "success":
            result = data.get("resultJson")
            video_url = _extract_video_url_from_result(result)
            if not video_url:
                raise GeminiOmniVideoError(f"Gemini Omni task succeeded but result URL missing: {data}")
            return video_url
        if state in {"fail", "failed", "error"}:
            detail = data.get("failMsg") or data.get("errorMessage") or data.get("msg") or data.get("message") or data
            raise GeminiOmniVideoError(f"Gemini Omni task failed: {detail}")
        if time.monotonic() - start_ts > KIE_OMNI_MAX_WAIT_SECONDS:
            raise GeminiOmniVideoError(f"Gemini Omni task timed out. Last state: {last_state or 'unknown'} {last_detail}")
        await asyncio.sleep(KIE_OMNI_POLL_SECONDS)


async def run_gemini_omni_video(
    *,
    user_id: int,
    prompt: str,
    duration: Any,
    aspect_ratio: Any,
    resolution: Any,
    reference_images: Optional[Sequence[bytes]] = None,
    reference_image_urls: Optional[Sequence[str]] = None,
    source_video_url: Optional[str] = None,
    source_video_start: Any = 0,
    source_video_end: Optional[Any] = None,
) -> str:
    prompt_text = str(prompt or "").strip()
    if not prompt_text:
        raise GeminiOmniVideoError("Prompt is required")
    normalized_duration = normalize_gemini_omni_duration(duration)
    normalized_aspect_ratio = normalize_gemini_omni_aspect_ratio(aspect_ratio)
    normalized_resolution = normalize_gemini_omni_resolution(resolution)
    direct_urls = [str(item or "").strip() for item in (reference_image_urls or []) if str(item or "").strip()]
    refs = [bytes(item) for item in (reference_images or []) if item]
    video_url = str(source_video_url or "").strip()
    if direct_urls and refs:
        raise GeminiOmniVideoError("Use either reference_image_urls or reference_images, not both")
    max_image_refs = 5 if video_url else 7
    if len(direct_urls) > max_image_refs or len(refs) > max_image_refs:
        raise GeminiOmniVideoError(f"Gemini Omni supports maximum {max_image_refs} image references for this mode")

    image_urls = direct_urls or [
        upload_gemini_omni_input_image(user_id=user_id, image_bytes=raw, slot=f"ref_{idx}")
        for idx, raw in enumerate(refs, start=1)
    ]

    body: Dict[str, Any] = {
        "model": "gemini-omni-video",
        "input": {
            "prompt": prompt_text,
            "duration": str(normalized_duration),
            "aspect_ratio": normalized_aspect_ratio,
            "resolution": normalized_resolution,
        },
    }
    if image_urls:
        body["input"]["image_urls"] = image_urls
    if video_url:
        try:
            start_sec = max(0, int(float(source_video_start or 0)))
        except Exception:
            start_sec = 0
        try:
            end_sec = int(float(source_video_end)) if source_video_end is not None else KIE_OMNI_VIDEO_EDIT_MAX_DURATION_SEC
        except Exception:
            end_sec = KIE_OMNI_VIDEO_EDIT_MAX_DURATION_SEC
        end_sec = max(start_sec + 1, min(int(KIE_OMNI_VIDEO_EDIT_MAX_DURATION_SEC), end_sec))
        body["input"]["video_list"] = [{"url": video_url, "start": start_sec, "ends": end_sec}]
    if KIE_OMNI_CALLBACK_URL:
        body["callBackUrl"] = KIE_OMNI_CALLBACK_URL

    timeout = aiohttp.ClientTimeout(total=max(60, int(KIE_OMNI_MAX_WAIT_SECONDS) + 120))
    async with aiohttp.ClientSession(timeout=timeout) as session:
        created = await _kie_request_json(session, "POST", "/api/v1/jobs/createTask", payload=body)
        task_id = _extract_kie_task_id(created)
        if not task_id:
            raise GeminiOmniVideoError(f"Gemini Omni createTask did not return task id: {created}")
        return await _poll_kie_task(session, task_id)
