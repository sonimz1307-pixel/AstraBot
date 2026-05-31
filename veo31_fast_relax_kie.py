from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict, Optional, Sequence

import aiohttp

from kling_flow import KlingFlowError, upload_bytes_to_supabase

KIE_API_BASE = (os.getenv("KIE_API_BASE") or "https://api.kie.ai").rstrip("/")
KIE_API_TOKEN = (os.getenv("KIE_API_TOKEN") or os.getenv("KIE_API_KEY") or "").strip()
KIE_VEO31_RELAX_MODEL = (os.getenv("KIE_VEO31_RELAX_MODEL") or "veo3_lite").strip() or "veo3_lite"
KIE_VEO31_RELAX_CALLBACK_URL = (os.getenv("KIE_VEO31_RELAX_CALLBACK_URL") or "").strip()
KIE_VEO31_RELAX_WATERMARK = (os.getenv("KIE_VEO31_RELAX_WATERMARK") or "").strip()
KIE_VEO31_RELAX_MAX_WAIT_SECONDS = float(os.getenv("KIE_VEO31_RELAX_MAX_WAIT_SECONDS", "1800") or "1800")
KIE_VEO31_RELAX_POLL_SECONDS = float(os.getenv("KIE_VEO31_RELAX_POLL_SECONDS", "8") or "8")
KIE_VEO31_RELAX_1080P_MAX_WAIT_SECONDS = float(os.getenv("KIE_VEO31_RELAX_1080P_MAX_WAIT_SECONDS", "300") or "300")
KIE_VEO31_RELAX_1080P_POLL_SECONDS = float(os.getenv("KIE_VEO31_RELAX_1080P_POLL_SECONDS", "25") or "25")
KIE_VEO31_RELAX_TOKENS = int(os.getenv("VEO31_FAST_RELAX_TOKENS", "4") or "4")
VEO31_FAST_RELAX_MODEL_SLUG = "veo-3.1-fast-relax"
VEO31_FAST_RELAX_DISPLAY_NAME = "Veo 3.1 Fast Relax"
VEO31_FAST_RELAX_DURATION = 8

_ALLOWED_ASPECT_RATIOS = ("16:9", "9:16")
_ALLOWED_RESOLUTIONS = ("720p", "1080p")


class Veo31FastRelaxError(RuntimeError):
    pass


def normalize_veo31_fast_relax_duration(value: Any = None) -> int:
    return VEO31_FAST_RELAX_DURATION


def normalize_veo31_fast_relax_aspect_ratio(value: Any, default: str = "16:9") -> str:
    raw = str(value or default).strip()
    return raw if raw in _ALLOWED_ASPECT_RATIOS else default


def normalize_veo31_fast_relax_resolution(value: Any, default: str = "1080p") -> str:
    raw = str(value or default).strip().lower()
    if raw in {"720", "720p", "hd"}:
        return "720p"
    if raw in {"1080", "1080p", "fhd", "fullhd", "full_hd"}:
        return "1080p"
    return default if default in _ALLOWED_RESOLUTIONS else "1080p"


def veo31_fast_relax_tokens_for_run(*_args: Any, **_kwargs: Any) -> int:
    return int(KIE_VEO31_RELAX_TOKENS)


def upload_veo31_fast_relax_input_image(*, user_id: int, image_bytes: bytes, filename_hint: Optional[str] = None, slot: str = "frame") -> str:
    if not image_bytes:
        raise Veo31FastRelaxError("Empty Veo 3.1 Fast Relax image")
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
    safe_slot = "".join(ch for ch in str(slot or "frame").lower() if ch.isalnum() or ch in {"_", "-"}) or "frame"
    path = f"veo31_fast_relax_inputs/{int(user_id)}/{safe_slot}_{int(time.time())}_{os.urandom(4).hex()}.{ext}"
    try:
        return upload_bytes_to_supabase(path, image_bytes, mime)
    except KlingFlowError as e:
        raise Veo31FastRelaxError(str(e)) from e
    except Exception as e:
        raise Veo31FastRelaxError(f"Failed to upload Veo 3.1 Fast Relax image: {e}") from e


def _auth_headers() -> Dict[str, str]:
    if not KIE_API_TOKEN:
        raise Veo31FastRelaxError("Veo 3.1 Fast Relax API token is not configured")
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
            raise Veo31FastRelaxError(f"Veo 3.1 Fast Relax request failed ({resp.status}): {detail}")
        if isinstance(data, dict):
            code = str(data.get("code") or "200")
            msg = str(data.get("msg") or data.get("message") or "").strip().lower()
            if code not in {"0", "200"} and msg != "success":
                detail = data.get("msg") or data.get("message") or data.get("error") or data
                raise Veo31FastRelaxError(f"Veo 3.1 Fast Relax API error: {detail}")
        return data if isinstance(data, dict) else {"data": data}


def _extract_task_id(create_response: Dict[str, Any]) -> str:
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


def _extract_video_url(value: Any) -> Optional[str]:
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.startswith("{") or raw.startswith("["):
            try:
                found = _extract_video_url(json.loads(raw))
                if found:
                    return found
            except Exception:
                pass
        if raw.startswith("http://") or raw.startswith("https://"):
            return raw
        return None
    if isinstance(value, list):
        for item in value:
            found = _extract_video_url(item)
            if found:
                return found
        return None
    if isinstance(value, dict):
        for key in (
            "resultUrl", "result_url", "videoUrl", "video_url", "url", "downloadUrl", "download_url",
            "resultUrls", "result_urls", "originUrls", "origin_urls", "fullResultUrls", "full_result_urls", "videos", "output",
        ):
            found = _extract_video_url(value.get(key))
            if found:
                return found
        for item in value.values():
            found = _extract_video_url(item)
            if found:
                return found
    return None


def _task_detail(data: Dict[str, Any]) -> str:
    return str(data.get("errorMessage") or data.get("error") or data.get("failMsg") or data.get("msg") or data.get("message") or data)[:1200]


async def _poll_task(session: aiohttp.ClientSession, task_id: str) -> str:
    start_ts = time.monotonic()
    last_state = ""
    while True:
        payload = await _kie_request_json(session, "GET", "/api/v1/veo/record-info", params={"taskId": task_id})
        data = payload.get("data") if isinstance(payload, dict) else None
        data = data if isinstance(data, dict) else {}
        success_flag = str(data.get("successFlag") or data.get("success_flag") or "").strip()
        state = str(data.get("state") or data.get("status") or "").strip().lower()
        last_state = success_flag or state or last_state

        if success_flag == "1" or state in {"success", "succeeded", "completed", "complete"}:
            video_url = _extract_video_url(data.get("response")) or _extract_video_url(data)
            if not video_url:
                raise Veo31FastRelaxError(f"Veo 3.1 Fast Relax task succeeded but result URL missing: {data}")
            return video_url
        if success_flag in {"2", "3"} or state in {"fail", "failed", "error", "canceled", "cancelled"}:
            raise Veo31FastRelaxError(f"Veo 3.1 Fast Relax task failed: {_task_detail(data)}")
        if time.monotonic() - start_ts > KIE_VEO31_RELAX_MAX_WAIT_SECONDS:
            raise Veo31FastRelaxError(f"Veo 3.1 Fast Relax timed out. Last state: {last_state or 'unknown'}")
        await asyncio.sleep(KIE_VEO31_RELAX_POLL_SECONDS)


async def _get_1080p_video(session: aiohttp.ClientSession, task_id: str, fallback_url: str) -> str:
    start_ts = time.monotonic()
    last_error = ""
    while True:
        try:
            payload = await _kie_request_json(session, "GET", "/api/v1/veo/get-1080p-video", params={"taskId": task_id, "index": 0})
            data = payload.get("data") if isinstance(payload, dict) else None
            result_url = ""
            if isinstance(data, dict):
                result_url = str(data.get("resultUrl") or data.get("result_url") or "").strip()
            if not result_url:
                result_url = _extract_video_url(data) or _extract_video_url(payload) or ""
            if result_url:
                return result_url
            last_error = str(payload)[:500]
        except Exception as e:
            last_error = str(e)[:500]
        if time.monotonic() - start_ts > KIE_VEO31_RELAX_1080P_MAX_WAIT_SECONDS:
            return fallback_url
        await asyncio.sleep(KIE_VEO31_RELAX_1080P_POLL_SECONDS)


async def run_veo31_fast_relax(
    *,
    user_id: int,
    prompt: str,
    mode: Any = "text_to_video",
    duration: Any = None,
    resolution: Any = "1080p",
    aspect_ratio: Any = "16:9",
    image_bytes: Optional[bytes] = None,
    last_frame_bytes: Optional[bytes] = None,
    image_urls: Optional[Sequence[str]] = None,
) -> str:
    prompt_text = str(prompt or "").strip()
    if not prompt_text:
        raise Veo31FastRelaxError("Prompt is required")
    normalized_duration = normalize_veo31_fast_relax_duration(duration)
    normalized_resolution = normalize_veo31_fast_relax_resolution(resolution)
    normalized_aspect_ratio = normalize_veo31_fast_relax_aspect_ratio(aspect_ratio)
    normalized_mode = str(mode or "text_to_video").strip().lower()
    if normalized_mode in {"image", "image_to_video", "i2v", "image2video", "image->video"}:
        normalized_mode = "image_to_video"
    else:
        normalized_mode = "text_to_video"

    direct_urls = [str(url or "").strip() for url in (image_urls or []) if str(url or "").strip()]
    if image_bytes:
        direct_urls.append(upload_veo31_fast_relax_input_image(user_id=user_id, image_bytes=image_bytes, slot="start"))
    if last_frame_bytes:
        direct_urls.append(upload_veo31_fast_relax_input_image(user_id=user_id, image_bytes=last_frame_bytes, slot="last"))

    if normalized_mode == "text_to_video":
        direct_urls = []
        generation_type = "TEXT_2_VIDEO"
    else:
        if not direct_urls:
            raise Veo31FastRelaxError("Для Veo 3.1 Fast Relax Image → Video нужен первый кадр")
        if len(direct_urls) > 2:
            raise Veo31FastRelaxError("Veo 3.1 Fast Relax поддерживает максимум 2 кадра: первый и последний")
        generation_type = "FIRST_AND_LAST_FRAMES_2_VIDEO"

    # KIE Veo 3.1 does not expose a stable request field for disabling audio.
    # Do not send an audio/off flag here: output audio is provider/model-defined.
    body: Dict[str, Any] = {
        "prompt": prompt_text,
        "imageUrls": direct_urls,
        "model": KIE_VEO31_RELAX_MODEL,
        "aspect_ratio": normalized_aspect_ratio,
        "enableFallback": False,
        "enableTranslation": True,
        "generationType": generation_type,
        "duration": normalized_duration,
    }
    if KIE_VEO31_RELAX_CALLBACK_URL:
        body["callBackUrl"] = KIE_VEO31_RELAX_CALLBACK_URL
    if KIE_VEO31_RELAX_WATERMARK:
        body["watermark"] = KIE_VEO31_RELAX_WATERMARK

    timeout = aiohttp.ClientTimeout(total=None, sock_connect=60, sock_read=180)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        created = await _kie_request_json(session, "POST", "/api/v1/veo/generate", payload=body)
        task_id = _extract_task_id(created)
        if not task_id:
            raise Veo31FastRelaxError(f"Veo 3.1 Fast Relax create response missing taskId: {created}")
        video_url = await _poll_task(session, task_id)
        if normalized_resolution == "1080p":
            video_url = await _get_1080p_video(session, task_id, video_url)
        return video_url
