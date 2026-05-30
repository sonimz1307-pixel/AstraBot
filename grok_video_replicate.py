from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict, Optional
from uuid import uuid4

import aiohttp

from kling_flow import upload_bytes_to_supabase, KlingFlowError

KIE_API_BASE = (os.getenv("KIE_API_BASE") or "https://api.kie.ai").rstrip("/")
KIE_API_TOKEN = (os.getenv("KIE_API_TOKEN") or os.getenv("KIE_API_KEY") or "").strip()
KIE_GROK_TEXT_MODEL = (os.getenv("KIE_GROK_TEXT_MODEL") or "grok-imagine/text-to-video").strip()
KIE_GROK_IMAGE_MODEL = (os.getenv("KIE_GROK_IMAGE_MODEL") or "grok-imagine/image-to-video").strip()
KIE_GROK_CALLBACK_URL = (os.getenv("KIE_GROK_CALLBACK_URL") or "").strip()
KIE_GROK_CREATE_TIMEOUT_SECONDS = float(os.getenv("KIE_GROK_CREATE_TIMEOUT_SECONDS", "60") or "60")
KIE_GROK_MAX_WAIT_SECONDS = float(os.getenv("KIE_GROK_MAX_WAIT_SECONDS", "900") or "900")
# Price grid in internal tokens.
# KIE cost basis: 480p = 1.6 credits/sec, 720p = 3 credits/sec.
# Target grid after margin review:
#   480p: 6s=1, 12s=2, 18s=2, 24s=3, 30s=3
#   720p: 6s=2, 12s=3, 18s=4, 24s=5, 30s=6
GROK_TOKEN_PRICE_MAP = {
    "480p": {6: 1, 12: 2, 18: 2, 24: 3, 30: 3},
    "720p": {6: 2, 12: 3, 18: 4, 24: 5, 30: 6},
}
GROK_ALLOWED_DURATIONS = (6, 12, 18, 24, 30)

GROK_ALLOWED_ASPECT_RATIOS = {
    "2:3", "3:2", "1:1", "16:9", "9:16",
}
GROK_ALLOWED_RESOLUTIONS = {"480p", "720p"}
GROK_ALLOWED_PROVIDER_MODES = {"fun", "normal", "spicy"}
GROK_POLLING_STATES = {"waiting", "queuing", "generating"}


class GrokVideoError(RuntimeError):
    pass


def normalize_grok_mode(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"image", "i2v", "image_to_video", "image2video", "image->video"}:
        return "image_to_video"
    return "text_to_video"


def normalize_grok_provider_mode(value: Any, default: str = "normal") -> str:
    raw = str(value or default).strip().lower()
    return raw if raw in GROK_ALLOWED_PROVIDER_MODES else default


def normalize_grok_duration(value: Any, default: int = 6) -> int:
    try:
        out = int(value)
    except Exception:
        out = int(default)
    if out <= GROK_ALLOWED_DURATIONS[0]:
        return GROK_ALLOWED_DURATIONS[0]
    if out >= GROK_ALLOWED_DURATIONS[-1]:
        return GROK_ALLOWED_DURATIONS[-1]
    return min(GROK_ALLOWED_DURATIONS, key=lambda item: (abs(item - out), item))


def normalize_grok_aspect_ratio(value: Any, default: str = "16:9") -> str:
    raw = str(value or default).strip()
    return raw if raw in GROK_ALLOWED_ASPECT_RATIOS else default


def normalize_grok_resolution(value: Any, default: str = "480p") -> str:
    raw = str(value or default).strip().lower()
    if raw in {"720", "720p"}:
        return "720p"
    if raw in {"480", "480p", ""}:
        return "480p"
    return default


def grok_tokens_for_duration(duration: Any, resolution: Any = "480p") -> int:
    seconds = normalize_grok_duration(duration)
    normalized = normalize_grok_resolution(resolution)
    price_map = GROK_TOKEN_PRICE_MAP.get(normalized) or GROK_TOKEN_PRICE_MAP["480p"]
    return int(price_map.get(seconds) or price_map[GROK_ALLOWED_DURATIONS[0]])


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


def _auth_headers() -> Dict[str, str]:
    if not KIE_API_TOKEN:
        raise GrokVideoError("KIE API token is not configured. Set KIE_API_TOKEN or KIE_API_KEY.")
    return {
        "Authorization": f"Bearer {KIE_API_TOKEN}",
        "Content-Type": "application/json",
    }


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
            raise GrokVideoError(f"KIE request failed ({resp.status}): {detail}")
        if isinstance(data, dict) and str(data.get("code") or "200") not in {"0", "200"}:
            detail = data.get("msg") or data.get("message") or data.get("error") or data
            raise GrokVideoError(f"KIE API error: {detail}")
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
        for key in (
            "videoUrl", "video_url", "resultUrl", "result_url", "url", "downloadUrl", "download_url",
        ):
            value = result.get(key)
            found = _extract_video_url_from_result(value)
            if found:
                return found
        for key in ("resultUrls", "result_urls", "videos", "urls", "output"):
            value = result.get(key)
            found = _extract_video_url_from_result(value)
            if found:
                return found
        for value in result.values():
            found = _extract_video_url_from_result(value)
            if found:
                return found
    return None


async def _poll_kie_task(session: aiohttp.ClientSession, task_id: str) -> str:
    start_ts = time.monotonic()
    wait_schedule = [2, 3, 5, 8, 13, 21, 34, 55]
    attempt = 0
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
                video_url = _extract_video_url_from_result(data)
            if not video_url:
                raise GrokVideoError(f"KIE task succeeded but no video url was returned: {data}")
            return video_url
        if state == "fail":
            fail_msg = str(data.get("failMsg") or data.get("message") or payload.get("msg") or "KIE task failed").strip()
            fail_code = str(data.get("failCode") or "").strip()
            detail = f"{fail_msg} ({fail_code})" if fail_code else fail_msg
            raise GrokVideoError(detail or "KIE task failed")
        if state not in GROK_POLLING_STATES:
            last_detail = str(payload.get("msg") or data or payload).strip()
        if (time.monotonic() - start_ts) >= KIE_GROK_MAX_WAIT_SECONDS:
            raise GrokVideoError(f"KIE Grok timeout. Last state: {last_state or 'unknown'} {last_detail}".strip())
        sleep_for = wait_schedule[min(attempt, len(wait_schedule) - 1)]
        attempt += 1
        await asyncio.sleep(sleep_for)


async def _run_grok_kie(*, task_model: str, input_payload: Dict[str, Any]) -> str:
    timeout = aiohttp.ClientTimeout(total=KIE_GROK_CREATE_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        payload: Dict[str, Any] = {"model": task_model, "input": input_payload}
        if KIE_GROK_CALLBACK_URL:
            payload["callBackUrl"] = KIE_GROK_CALLBACK_URL
        created = await _kie_request_json(session, "POST", "/api/v1/jobs/createTask", payload=payload)
        task_id = _extract_kie_task_id(created)
        if not task_id:
            raise GrokVideoError(f"KIE did not return taskId: {created}")
        return await _poll_kie_task(session, task_id)


async def run_grok_text_to_video(
    *,
    prompt: str,
    duration: Any = 6,
    resolution: Any = "480p",
    aspect_ratio: Any = "16:9",
    provider_mode: Any = "normal",
) -> str:
    clean_prompt = str(prompt or "").strip()
    if not clean_prompt:
        raise GrokVideoError("Prompt is required for Grok Text → Video")
    input_payload = {
        "prompt": clean_prompt,
        "aspect_ratio": normalize_grok_aspect_ratio(aspect_ratio),
        "mode": normalize_grok_provider_mode(provider_mode),
        "duration": str(normalize_grok_duration(duration)),
        "resolution": normalize_grok_resolution(resolution),
    }
    return await _run_grok_kie(task_model=KIE_GROK_TEXT_MODEL, input_payload=input_payload)


async def run_grok_image_to_video(
    *,
    user_id: int,
    image_bytes: bytes,
    prompt: str,
    duration: Any = 6,
    resolution: Any = "480p",
    aspect_ratio: Any = "16:9",
    provider_mode: Any = "normal",
    image_url: Optional[str] = None,
    filename_hint: Optional[str] = None,
) -> str:
    clean_prompt = str(prompt or "").strip()
    if not clean_prompt:
        raise GrokVideoError("Prompt is required for Grok Image → Video")
    if not image_url:
        image_url = upload_grok_input_image(user_id=int(user_id), image_bytes=image_bytes, filename_hint=filename_hint)
    input_payload = {
        "task_id": f"task_grok_{uuid4().hex[:16]}",
        "image_urls": [str(image_url)],
        "prompt": clean_prompt,
        "mode": normalize_grok_provider_mode(provider_mode),
        "duration": str(normalize_grok_duration(duration)),
        "resolution": normalize_grok_resolution(resolution),
        "aspect_ratio": normalize_grok_aspect_ratio(aspect_ratio),
    }
    return await _run_grok_kie(task_model=KIE_GROK_IMAGE_MODEL, input_payload=input_payload)
