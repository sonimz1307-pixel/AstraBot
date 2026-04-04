from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import aiohttp

KIE_API_BASE = (os.getenv("KIE_API_BASE") or "https://api.kie.ai").rstrip("/")
KIE_FILE_API_BASE = (os.getenv("KIE_FILE_API_BASE") or "https://kieai.redpandaai.co").rstrip("/")
KIE_API_TOKEN = (os.getenv("KIE_API_TOKEN") or os.getenv("KIE_API_KEY") or "").strip()
KIE_SEEDANCE_CALLBACK_URL = (os.getenv("KIE_SEEDANCE_CALLBACK_URL") or "").strip()
KIE_SEEDANCE_CREATE_TIMEOUT_SECONDS = float(os.getenv("KIE_SEEDANCE_CREATE_TIMEOUT_SECONDS", "90") or "90")
KIE_SEEDANCE_MAX_WAIT_SECONDS = float(os.getenv("KIE_SEEDANCE_MAX_WAIT_SECONDS", "1800") or "1800")

SEEDANCE_KIE_ALLOWED_MODELS = {"seedance-kie", "seedance-kie-fast"}
SEEDANCE_KIE_ALLOWED_DURATIONS = (5, 10, 15)
SEEDANCE_KIE_POLLING_STATES = {"waiting", "queuing", "generating", "processing"}
SEEDANCE_KIE_TOKEN_MAP = {
    "seedance-kie": {5: 10, 10: 20, 15: 30},
    "seedance-kie-fast": {5: 5, 10: 10, 15: 15},
}
SEEDANCE_KIE_MODEL_IDS = {
    "seedance-kie": "bytedance/seedance-2",
    "seedance-kie-fast": "bytedance/seedance-2-fast",
}
SEEDANCE_KIE_RESOLUTIONS = {
    "seedance-kie": "720p",
    "seedance-kie-fast": "480p",
}


class SeedanceKieError(RuntimeError):
    pass


def normalize_seedance_kie_model(value: Any, default: str = "seedance-kie") -> str:
    raw = str(value or default).strip().lower()
    if raw in {"seedance-2-fast", "seedance-fast", "fast", "seedance-kie-fast"}:
        return "seedance-kie-fast"
    if raw in {"seedance-2", "seedance", "seedance-kie"}:
        return "seedance-kie"
    return default


def normalize_seedance_kie_mode(value: Any, default: str = "text_to_video") -> str:
    raw = str(value or default).strip().lower()
    if raw in {"image", "image_to_video", "i2v", "image2video"}:
        return "image_to_video"
    return "text_to_video"


def normalize_seedance_kie_duration(value: Any, default: int = 5) -> int:
    try:
        out = int(value)
    except Exception:
        out = int(default)
    if out <= SEEDANCE_KIE_ALLOWED_DURATIONS[0]:
        return SEEDANCE_KIE_ALLOWED_DURATIONS[0]
    if out >= SEEDANCE_KIE_ALLOWED_DURATIONS[-1]:
        return SEEDANCE_KIE_ALLOWED_DURATIONS[-1]
    return min(SEEDANCE_KIE_ALLOWED_DURATIONS, key=lambda item: (abs(item - out), item))


def seedance_kie_tokens_for_duration(model: Any, duration: Any) -> int:
    normalized_model = normalize_seedance_kie_model(model)
    normalized_duration = normalize_seedance_kie_duration(duration)
    return int(SEEDANCE_KIE_TOKEN_MAP[normalized_model][normalized_duration])


def seedance_kie_resolution(model: Any) -> str:
    return SEEDANCE_KIE_RESOLUTIONS[normalize_seedance_kie_model(model)]


def seedance_kie_model_id(model: Any) -> str:
    return SEEDANCE_KIE_MODEL_IDS[normalize_seedance_kie_model(model)]


def _auth_headers(json_mode: bool = True) -> Dict[str, str]:
    if not KIE_API_TOKEN:
        raise SeedanceKieError("KIE API token is not configured. Set KIE_API_TOKEN or KIE_API_KEY.")
    headers = {"Authorization": f"Bearer {KIE_API_TOKEN}"}
    if json_mode:
        headers["Content-Type"] = "application/json"
    return headers


async def _kie_request_json(
    session: aiohttp.ClientSession,
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    url = f"{KIE_API_BASE}{path}"
    async with session.request(method.upper(), url, headers=_auth_headers(json_mode=True), params=params, json=payload) as resp:
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


async def _upload_one_file(session: aiohttp.ClientSession, *, data: bytes, file_name: str, upload_path: str) -> str:
    if not data:
        raise SeedanceKieError("Empty upload payload")
    form = aiohttp.FormData()
    form.add_field("file", data, filename=file_name, content_type=_guess_mime(file_name, data))
    form.add_field("uploadPath", upload_path)
    form.add_field("fileName", file_name)
    url = f"{KIE_FILE_API_BASE}/api/file-stream-upload"
    async with session.post(url, headers=_auth_headers(json_mode=False), data=form) as resp:
        text = await resp.text()
    try:
        payload = json.loads(text) if text else {}
    except Exception:
        payload = {"raw": text}
    if resp.status >= 400:
        detail = payload.get("msg") if isinstance(payload, dict) else None
        detail = detail or (payload.get("message") if isinstance(payload, dict) else None) or text or f"HTTP {resp.status}"
        raise SeedanceKieError(f"KIE file upload failed ({resp.status}): {detail}")
    if isinstance(payload, dict) and str(payload.get("code") or "200") not in {"0", "200"}:
        detail = payload.get("msg") or payload.get("message") or payload.get("error") or payload
        raise SeedanceKieError(f"KIE file upload error: {detail}")
    data_obj = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data_obj, dict):
        for key in ("fileUrl", "downloadUrl"):
            value = str(data_obj.get(key) or "").strip()
            if value:
                return value
    raise SeedanceKieError(f"KIE file upload did not return file URL: {payload}")


async def _upload_files(user_id: int, files: Sequence[bytes], kind: str) -> List[str]:
    if not files:
        return []
    timeout = aiohttp.ClientTimeout(total=KIE_SEEDANCE_CREATE_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        out: List[str] = []
        for idx, raw in enumerate(files, start=1):
            ext = _guess_ext(raw, default="bin")
            file_name = f"seedance_{kind}_{int(user_id)}_{int(time.time())}_{idx}.{ext}"
            out.append(await _upload_one_file(session, data=raw, file_name=file_name, upload_path=f"seedance/{kind}/{int(user_id)}"))
        return out


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


def _extract_video_url(result: Any) -> Optional[str]:
    if isinstance(result, str):
        raw = result.strip()
        if raw.startswith("{") or raw.startswith("["):
            try:
                return _extract_video_url(json.loads(raw))
            except Exception:
                pass
        if raw.startswith("http://") or raw.startswith("https://"):
            return raw
        return None
    if isinstance(result, list):
        for item in result:
            found = _extract_video_url(item)
            if found:
                return found
        return None
    if isinstance(result, dict):
        for key in ("videoUrl", "video_url", "resultUrl", "result_url", "url", "downloadUrl", "download_url"):
            found = _extract_video_url(result.get(key))
            if found:
                return found
        for key in ("resultUrls", "result_urls", "videos", "urls", "output"):
            found = _extract_video_url(result.get(key))
            if found:
                return found
        for value in result.values():
            found = _extract_video_url(value)
            if found:
                return found
    return None


async def _poll_task(session: aiohttp.ClientSession, task_id: str) -> str:
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
            video_url = _extract_video_url(data.get("resultJson")) or _extract_video_url(data) or _extract_video_url(payload)
            if not video_url:
                raise SeedanceKieError(f"Seedance task succeeded but no video url was returned: {data}")
            return video_url
        if state == "fail":
            fail_msg = str(data.get("failMsg") or data.get("message") or payload.get("msg") or "Seedance task failed").strip()
            fail_code = str(data.get("failCode") or "").strip()
            detail = f"{fail_msg} ({fail_code})" if fail_code else fail_msg
            raise SeedanceKieError(detail or "Seedance task failed")
        if state not in SEEDANCE_KIE_POLLING_STATES:
            last_detail = str(payload.get("msg") or data or payload).strip()
        if (time.monotonic() - start_ts) >= KIE_SEEDANCE_MAX_WAIT_SECONDS:
            raise SeedanceKieError(f"Seedance timeout. Last state: {last_state or 'unknown'} {last_detail}".strip())
        sleep_for = wait_schedule[min(attempt, len(wait_schedule) - 1)]
        attempt += 1
        await asyncio.sleep(sleep_for)


async def _run_seedance_task(*, model: str, payload_input: Dict[str, Any]) -> str:
    timeout = aiohttp.ClientTimeout(total=KIE_SEEDANCE_CREATE_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        payload: Dict[str, Any] = {
            "model": seedance_kie_model_id(model),
            "input": payload_input,
        }
        if KIE_SEEDANCE_CALLBACK_URL:
            payload["callBackUrl"] = KIE_SEEDANCE_CALLBACK_URL
        created = await _kie_request_json(session, "POST", "/api/v1/jobs/createTask", payload=payload)
        task_id = _extract_task_id(created)
        if not task_id:
            raise SeedanceKieError(f"KIE did not return taskId: {created}")
        return await _poll_task(session, task_id)


async def run_seedance_kie_text_to_video(*, model: Any, prompt: str, duration: Any) -> str:
    normalized_model = normalize_seedance_kie_model(model)
    clean_prompt = str(prompt or "").strip()
    if not clean_prompt:
        raise SeedanceKieError("Seedance prompt is required")
    payload_input: Dict[str, Any] = {
        "prompt": clean_prompt,
        "duration": normalize_seedance_kie_duration(duration),
        "resolution": seedance_kie_resolution(normalized_model),
        "aspect_ratio": "16:9",
        "generate_audio": True,
    }
    return await _run_seedance_task(model=normalized_model, payload_input=payload_input)


async def run_seedance_kie_image_to_video(
    *,
    user_id: int,
    model: Any,
    prompt: str,
    duration: Any,
    reference_images: Sequence[bytes],
    reference_audios: Sequence[bytes] | None = None,
) -> str:
    normalized_model = normalize_seedance_kie_model(model)
    clean_prompt = str(prompt or "").strip()
    if not clean_prompt:
        raise SeedanceKieError("Seedance prompt is required")
    image_urls = await _upload_files(int(user_id), list(reference_images or [])[:7], "image")
    if not image_urls:
        raise SeedanceKieError("Seedance Image→Video requires at least one image reference")
    audio_urls = await _upload_files(int(user_id), list(reference_audios or [])[:3], "audio")
    payload_input: Dict[str, Any] = {
        "prompt": clean_prompt,
        "duration": normalize_seedance_kie_duration(duration),
        "resolution": seedance_kie_resolution(normalized_model),
        "aspect_ratio": "16:9",
        "generate_audio": True,
        "reference_image_urls": image_urls,
    }
    if audio_urls:
        payload_input["reference_audio_urls"] = audio_urls
    return await _run_seedance_task(model=normalized_model, payload_input=payload_input)


def _guess_ext(data: bytes, default: str = "bin") -> str:
    head = bytes((data or b"")[:32])
    if head.startswith(b"\x89PNG"):
        return "png"
    if head[:3] == b"\xff\xd8\xff":
        return "jpg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"GIF8"):
        return "gif"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in {b"M4A ", b"M4B ", b"isom", b"mp42", b"M4P ", b"qt  "}:
            return "m4a"
    if head.startswith(b"ID3"):
        return "mp3"
    return default


def _guess_mime(file_name: str, data: bytes) -> str:
    name = str(file_name or "").lower()
    if name.endswith(".png") or data.startswith(b"\x89PNG"):
        return "image/png"
    if name.endswith(".jpg") or name.endswith(".jpeg") or data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if name.endswith(".webp") or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"):
        return "image/webp"
    if name.endswith(".gif") or data.startswith(b"GIF8"):
        return "image/gif"
    if name.endswith(".wav") or (data[:4] == b"RIFF" and data[8:12] == b"WAVE"):
        return "audio/wav"
    if name.endswith(".ogg") or data[:4] == b"OggS":
        return "audio/ogg"
    if name.endswith(".m4a"):
        return "audio/mp4"
    if name.endswith(".mp3") or data.startswith(b"ID3"):
        return "audio/mpeg"
    return "application/octet-stream"
