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

SEEDANCE_MODEL_MAP = {
    "seedance-2": "bytedance/seedance-2",
    "seedance-2-fast": "bytedance/seedance-2-fast",
}
SEEDANCE_ALLOWED_DURATIONS = (5, 10, 15)
SEEDANCE_ALLOWED_ASPECT_RATIOS = {"16:9", "9:16", "4:3", "3:4"}
SEEDANCE_ALLOWED_RESOLUTIONS = {"480p", "720p"}
SEEDANCE_POLLING_STATES = {"waiting", "queuing", "generating", "pending", "running"}


class SeedanceKieError(RuntimeError):
    pass


def normalize_seedance_kie_model(value: Any) -> str:
    raw = str(value or "seedance-2").strip().lower()
    return raw if raw in SEEDANCE_MODEL_MAP else "seedance-2"


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


def normalize_seedance_kie_resolution(value: Any, *, model: Any = "seedance-2") -> str:
    normalized_model = normalize_seedance_kie_model(model)
    if normalized_model == "seedance-2-fast":
        return "480p"
    raw = str(value or "720p").strip().lower()
    return raw if raw in SEEDANCE_ALLOWED_RESOLUTIONS else "720p"


def seedance_kie_tokens_for_duration(duration: Any, *, model: Any = "seedance-2") -> int:
    seconds = normalize_seedance_kie_duration(duration)
    normalized_model = normalize_seedance_kie_model(model)
    price_map = {
        "seedance-2": {5: 10, 10: 20, 15: 30},
        "seedance-2-fast": {5: 5, 10: 10, 15: 15},
    }
    return int(price_map.get(normalized_model, price_map["seedance-2"]).get(seconds, 10))


def _auth_headers() -> Dict[str, str]:
    if not KIE_API_TOKEN:
        raise SeedanceKieError("KIE API token is not configured. Set KIE_API_TOKEN or KIE_API_KEY.")
    return {
        "Authorization": f"Bearer {KIE_API_TOKEN}",
        "Content-Type": "application/json",
    }


async def _kie_request_json(
    session: aiohttp.ClientSession,
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    url = f"{KIE_API_BASE}{path}"
    async with session.request(method.upper(), url, headers=_auth_headers(), params=params, json=payload) as resp:
        text = await resp.text()
        try:
            data = json.loads(text) if text else {}
        except Exception:
            data = {"raw": text}
        if resp.status >= 400:
            detail = data.get("msg") or data.get("message") or data.get("error") or text or f"HTTP {resp.status}"
            raise SeedanceKieError(f"KIE request failed ({resp.status}): {detail}")
        if isinstance(data, dict) and str(data.get("code") or "200") not in {"0", "200"}:
            detail = data.get("msg") or data.get("message") or data.get("error") or data
            raise SeedanceKieError(f"KIE API error: {detail}")
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
                raise SeedanceKieError(f"KIE task succeeded but no video url was returned: {data}")
            return video_url
        if state == "fail":
            fail_msg = str(data.get("failMsg") or data.get("message") or payload.get("msg") or "KIE task failed").strip()
            fail_code = str(data.get("failCode") or "").strip()
            detail = f"{fail_msg} ({fail_code})" if fail_code else fail_msg
            raise SeedanceKieError(detail or "KIE task failed")
        if state not in SEEDANCE_POLLING_STATES:
            last_detail = str(payload.get("msg") or data or payload).strip()
        if (time.monotonic() - start_ts) >= KIE_SEEDANCE_MAX_WAIT_SECONDS:
            raise SeedanceKieError(f"KIE Seedance timeout. Last state: {last_state or 'unknown'} {last_detail}".strip())
        sleep_for = wait_schedule[min(attempt, len(wait_schedule) - 1)]
        attempt += 1
        await asyncio.sleep(sleep_for)


async def run_seedance_kie_video(
    *,
    model: Any,
    prompt: str,
    duration: Any = 5,
    aspect_ratio: Any = "16:9",
    resolution: Any = None,
    generate_audio: bool = False,
    first_frame_url: Optional[str] = None,
    last_frame_url: Optional[str] = None,
    reference_image_urls: Optional[List[str]] = None,
    reference_audio_urls: Optional[List[str]] = None,
) -> str:
    clean_prompt = str(prompt or "").strip()
    if not clean_prompt:
        raise SeedanceKieError("Prompt is required for Seedance Kie Video")

    normalized_model = normalize_seedance_kie_model(model)
    input_payload: Dict[str, Any] = {
        "prompt": clean_prompt,
        "generate_audio": bool(generate_audio),
        "resolution": normalize_seedance_kie_resolution(resolution, model=normalized_model),
        "aspect_ratio": normalize_seedance_kie_aspect_ratio(aspect_ratio),
        "duration": int(normalize_seedance_kie_duration(duration)),
        "web_search": False,
    }

    if str(first_frame_url or "").strip():
        input_payload["first_frame_url"] = str(first_frame_url).strip()
    if str(last_frame_url or "").strip():
        input_payload["last_frame_url"] = str(last_frame_url).strip()

    image_urls = [str(item).strip() for item in (reference_image_urls or []) if str(item).strip()]
    audio_urls = [str(item).strip() for item in (reference_audio_urls or []) if str(item).strip()]
    if image_urls:
        input_payload["reference_image_urls"] = image_urls
    if audio_urls:
        input_payload["reference_audio_urls"] = audio_urls

    payload: Dict[str, Any] = {
        "model": SEEDANCE_MODEL_MAP[normalized_model],
        "input": input_payload,
    }
    if KIE_SEEDANCE_CALLBACK_URL:
        payload["callBackUrl"] = KIE_SEEDANCE_CALLBACK_URL

    timeout = aiohttp.ClientTimeout(total=KIE_SEEDANCE_CREATE_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        created = await _kie_request_json(session, "POST", "/api/v1/jobs/createTask", payload=payload)
        task_id = _extract_kie_task_id(created)
        if not task_id:
            raise SeedanceKieError(f"KIE did not return taskId: {created}")
        return await _poll_kie_task(session, task_id)
