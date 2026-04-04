from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict, List, Optional

import aiohttp

KIE_API_BASE = (os.getenv("KIE_API_BASE") or "https://api.kie.ai").rstrip("/")
KIE_API_TOKEN = (os.getenv("KIE_API_TOKEN") or os.getenv("KIE_API_KEY") or "").strip()
KIE_SEEDANCE_CALLBACK_URL = (os.getenv("KIE_SEEDANCE_CALLBACK_URL") or "").strip()
KIE_SEEDANCE_CREATE_TIMEOUT_SECONDS = float(os.getenv("KIE_SEEDANCE_CREATE_TIMEOUT_SECONDS", "60") or "60")
KIE_SEEDANCE_MAX_WAIT_SECONDS = float(os.getenv("KIE_SEEDANCE_MAX_WAIT_SECONDS", "1800") or "1800")

SEEDANCE_MODEL = "seedance-2"
SEEDANCE_FAST_MODEL = "seedance-2-fast"
SEEDANCE_KIE_MODEL = "bytedance/seedance-2"
SEEDANCE_KIE_FAST_MODEL = "bytedance/seedance-2-fast"
SEEDANCE_ALLOWED_DURATIONS = (5, 10, 15)
SEEDANCE_ALLOWED_ASPECT_RATIOS = {"16:9", "9:16", "1:1", "4:3", "3:4"}
SEEDANCE_POLLING_STATES = {"waiting", "queuing", "generating"}
SEEDANCE_TOKENS = {
    SEEDANCE_MODEL: {5: 10, 10: 20, 15: 30},
    SEEDANCE_FAST_MODEL: {5: 5, 10: 10, 15: 15},
}


class SeedanceKieError(RuntimeError):
    pass


def normalize_seedance_kie_model(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {SEEDANCE_FAST_MODEL, SEEDANCE_KIE_FAST_MODEL, "seedance_fast", "fast"}:
        return SEEDANCE_FAST_MODEL
    return SEEDANCE_MODEL


def seedance_kie_api_model(value: Any) -> str:
    return SEEDANCE_KIE_FAST_MODEL if normalize_seedance_kie_model(value) == SEEDANCE_FAST_MODEL else SEEDANCE_KIE_MODEL


def seedance_kie_resolution_for_model(value: Any) -> str:
    return "480p" if normalize_seedance_kie_model(value) == SEEDANCE_FAST_MODEL else "720p"


def normalize_seedance_kie_duration(value: Any, default: int = 5) -> int:
    try:
        out = int(value)
    except Exception:
        out = int(default)
    if out <= SEEDANCE_ALLOWED_DURATIONS[0]:
        return SEEDANCE_ALLOWED_DURATIONS[0]
    if out >= SEEDANCE_ALLOWED_DURATIONS[-1]:
        return SEEDANCE_ALLOWED_DURATIONS[-1]
    return min(SEEDANCE_ALLOWED_DURATIONS, key=lambda item: (abs(item - out), item))


def normalize_seedance_kie_aspect_ratio(value: Any, default: str = "16:9") -> str:
    raw = str(value or default).strip()
    return raw if raw in SEEDANCE_ALLOWED_ASPECT_RATIOS else default


def seedance_kie_tokens_for_duration(model: Any, duration: Any) -> int:
    normalized_model = normalize_seedance_kie_model(model)
    normalized_duration = normalize_seedance_kie_duration(duration)
    return int(SEEDANCE_TOKENS.get(normalized_model, {}).get(normalized_duration, 0))


def _headers() -> Dict[str, str]:
    if not KIE_API_TOKEN:
        raise SeedanceKieError("KIE API token is not configured. Set KIE_API_TOKEN or KIE_API_KEY.")
    return {"Authorization": f"Bearer {KIE_API_TOKEN}", "Content-Type": "application/json"}


async def _request_json(session: aiohttp.ClientSession, method: str, path: str, *, params: Optional[Dict[str, Any]] = None, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"{KIE_API_BASE}{path}"
    async with session.request(method.upper(), url, headers=_headers(), params=params, json=payload) as resp:
        text = await resp.text()
    try:
        data = json.loads(text) if text else {}
    except Exception:
        data = {"raw": text}
    if resp.status >= 400:
        detail = data.get("msg") if isinstance(data, dict) else None
        detail = detail or (data.get("message") if isinstance(data, dict) else None) or text or f"HTTP {resp.status}"
        raise SeedanceKieError(f"KIE request failed ({resp.status}): {detail}")
    if isinstance(data, dict) and str(data.get("code") or "200") not in {"0", "200"}:
        detail = data.get("msg") or data.get("message") or data.get("error") or data
        raise SeedanceKieError(f"KIE API error: {detail}")
    return data if isinstance(data, dict) else {"data": data}


def _extract_task_id(payload: Dict[str, Any]) -> str:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        for key in ("taskId", "task_id", "id"):
            value = str(data.get(key) or "").strip()
            if value:
                return value
    for key in ("taskId", "task_id", "id"):
        value = str(payload.get(key) or "").strip() if isinstance(payload, dict) else ""
        if value:
            return value
    return ""


def _extract_video_url(value: Any) -> Optional[str]:
    if isinstance(value, str):
        raw = value.strip()
        if raw.startswith("{") or raw.startswith("["):
            try:
                return _extract_video_url(json.loads(raw))
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
        for key in ("videoUrl", "video_url", "resultUrl", "result_url", "downloadUrl", "download_url", "url"):
            found = _extract_video_url(value.get(key))
            if found:
                return found
        for key in ("resultUrls", "result_urls", "videos", "urls", "output"):
            found = _extract_video_url(value.get(key))
            if found:
                return found
        for item in value.values():
            found = _extract_video_url(item)
            if found:
                return found
    return None


async def create_seedance_kie_task(*, model: Any, prompt: str, duration: Any = 5, aspect_ratio: Any = "16:9", generate_audio: bool = False, first_frame_url: Optional[str] = None, last_frame_url: Optional[str] = None, reference_image_urls: Optional[List[str]] = None, reference_audio_urls: Optional[List[str]] = None, return_last_frame: bool = False, web_search: bool = False) -> str:
    normalized_model = normalize_seedance_kie_model(model)
    input_payload: Dict[str, Any] = {
        "prompt": str(prompt or "").strip(),
        "generate_audio": bool(generate_audio),
        "resolution": seedance_kie_resolution_for_model(normalized_model),
        "aspect_ratio": normalize_seedance_kie_aspect_ratio(aspect_ratio),
        "duration": normalize_seedance_kie_duration(duration),
        "return_last_frame": bool(return_last_frame),
        "web_search": bool(web_search),
    }
    first_frame_url = str(first_frame_url or "").strip() or None
    last_frame_url = str(last_frame_url or "").strip() or None
    ref_images = [str(item or "").strip() for item in (reference_image_urls or []) if str(item or "").strip()]
    ref_audios = [str(item or "").strip() for item in (reference_audio_urls or []) if str(item or "").strip()]
    if first_frame_url:
        input_payload["first_frame_url"] = first_frame_url
        if last_frame_url:
            input_payload["last_frame_url"] = last_frame_url
    else:
        if ref_images:
            input_payload["reference_image_urls"] = ref_images
        if ref_audios:
            input_payload["reference_audio_urls"] = ref_audios

    timeout = aiohttp.ClientTimeout(total=KIE_SEEDANCE_CREATE_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        payload: Dict[str, Any] = {"model": seedance_kie_api_model(normalized_model), "input": input_payload}
        if KIE_SEEDANCE_CALLBACK_URL:
            payload["callBackUrl"] = KIE_SEEDANCE_CALLBACK_URL
        created = await _request_json(session, "POST", "/api/v1/jobs/createTask", payload=payload)
        task_id = _extract_task_id(created)
        if not task_id:
            raise SeedanceKieError(f"KIE did not return taskId: {created}")
        return task_id


async def wait_seedance_kie_task(task_id: str) -> str:
    task_id_text = str(task_id or "").strip()
    if not task_id_text:
        raise SeedanceKieError("Seedance task_id is empty")
    timeout = aiohttp.ClientTimeout(total=KIE_SEEDANCE_CREATE_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        start_ts = time.monotonic()
        wait_schedule = [2, 3, 5, 8, 13, 21, 34, 55]
        attempt = 0
        last_state = ""
        last_detail = ""
        while True:
            payload = await _request_json(session, "GET", "/api/v1/jobs/recordInfo", params={"taskId": task_id_text})
            data = payload.get("data") if isinstance(payload, dict) else None
            data = data if isinstance(data, dict) else {}
            state = str(data.get("state") or data.get("status") or "").strip().lower()
            if state:
                last_state = state
            if state == "success":
                video_url = _extract_video_url(data.get("resultJson")) or _extract_video_url(data)
                if not video_url:
                    raise SeedanceKieError(f"KIE task succeeded but no video URL was returned: {data}")
                return video_url
            if state == "fail":
                fail_msg = str(data.get("failMsg") or data.get("message") or payload.get("msg") or "Seedance task failed").strip()
                fail_code = str(data.get("failCode") or "").strip()
                detail = f"{fail_msg} ({fail_code})" if fail_code else fail_msg
                raise SeedanceKieError(detail or "Seedance task failed")
            if state not in SEEDANCE_POLLING_STATES:
                last_detail = str(payload.get("msg") or data or payload).strip()
            if (time.monotonic() - start_ts) >= KIE_SEEDANCE_MAX_WAIT_SECONDS:
                raise SeedanceKieError(f"Seedance timeout. Last state: {last_state or 'unknown'} {last_detail}".strip())
            await asyncio.sleep(wait_schedule[min(attempt, len(wait_schedule) - 1)])
            attempt += 1
