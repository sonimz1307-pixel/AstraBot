from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict, List, Optional, Sequence

import httpx

from kling_flow import upload_bytes_to_supabase

PIAPI_BASE_URL = (os.getenv("PIAPI_BASE_URL", "https://api.piapi.ai") or "https://api.piapi.ai").strip().rstrip("/")
PIAPI_API_KEY = (os.getenv("PIAPI_API_KEY") or os.getenv("PIAPI_KEY") or "").strip()
PIAPI_SEEDANCE_SERVICE_MODE = (os.getenv("PIAPI_SEEDANCE_SERVICE_MODE") or "public").strip() or "public"
PIAPI_SEEDANCE_TIMEOUT_SECONDS = float(os.getenv("PIAPI_SEEDANCE_TIMEOUT_SECONDS", "1800") or "1800")
PIAPI_SEEDANCE_POLL_SECONDS = float(os.getenv("PIAPI_SEEDANCE_POLL_SECONDS", "6") or "6")

SEEDANCE_KIE_ALLOWED_MODELS = {"seedance-kie", "seedance-kie-fast"}
SEEDANCE_KIE_ALLOWED_DURATIONS = (5, 10, 15)
SEEDANCE_KIE_ALLOWED_ASPECT_RATIOS = ("16:9", "9:16", "1:1")
SEEDANCE_KIE_TOKEN_MAP = {
    "seedance-kie": {5: 10, 10: 20, 15: 30},
    "seedance-kie-fast": {5: 5, 10: 10, 15: 15},
}
SEEDANCE_KIE_TASK_TYPES = {
    "seedance-kie": "seedance-2",
    "seedance-kie-fast": "seedance-2-fast",
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
    return SEEDANCE_KIE_TASK_TYPES[normalize_seedance_kie_model(model)]


def normalize_seedance_kie_aspect_ratio(value: Any, default: str = "16:9") -> str:
    raw = str(value or default).strip()
    if raw in SEEDANCE_KIE_ALLOWED_ASPECT_RATIOS:
        return raw
    return default




def _looks_like_mp3(data: bytes) -> bool:
    head = bytes((data or b"")[:64])
    if head.startswith(b"ID3"):
        return True
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return True
    return False

def _auth_headers() -> Dict[str, str]:
    if not PIAPI_API_KEY:
        raise SeedanceKieError("PIAPI API key is not configured. Set PIAPI_API_KEY or PIAPI_KEY.")
    return {"X-API-Key": PIAPI_API_KEY, "Content-Type": "application/json"}


def _upload_public_file(user_id: int, kind: str, idx: int, raw: bytes) -> str:
    if not raw:
        raise SeedanceKieError("Empty upload payload")
    ext = _guess_ext(raw, default="bin")
    mime = _guess_mime(ext, raw)
    path = f"workspace_refs/{int(user_id)}/seedance/{kind}/{int(time.time())}_{idx}.{ext}"
    return upload_bytes_to_supabase(path, raw, mime)


async def _upload_files(user_id: int, files: Sequence[bytes], kind: str) -> List[str]:
    urls: List[str] = []
    for idx, raw in enumerate(files or [], start=1):
        if not raw:
            continue
        urls.append(_upload_public_file(int(user_id), kind, idx, raw))
    return urls


def _extract_task_id(payload: Dict[str, Any]) -> str:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        for key in ("task_id", "taskId", "id"):
            value = str(data.get(key) or "").strip()
            if value:
                return value
    for key in ("task_id", "taskId", "id"):
        value = str(payload.get(key) or "").strip() if isinstance(payload, dict) else ""
        if value:
            return value
    return ""


def _extract_status(payload: Dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return ""
    if payload.get("status"):
        return str(payload.get("status") or "").strip().lower()
    data = payload.get("data")
    if isinstance(data, dict):
        return str(data.get("status") or "").strip().lower()
    return ""


def _extract_error(payload: Dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return ""
    message = str(payload.get("message") or "").strip()
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        detail = str(error.get("message") or error.get("raw_message") or error.get("detail") or "").strip()
        if detail:
            return detail
    return message


def _extract_video_url(payload: Dict[str, Any]) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    output = data.get("output") if isinstance(data, dict) else None
    if isinstance(output, dict):
        for key in ("video", "video_url", "videoUrl", "url"):
            value = output.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value
    return None


async def _request_json(method: str, path: str, *, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"{PIAPI_BASE_URL}{path}"
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.request(method.upper(), url, headers=_auth_headers(), json=payload)
    parsed: Optional[Dict[str, Any]] = None
    try:
        candidate = resp.json()
        if isinstance(candidate, dict):
            parsed = candidate
    except Exception:
        parsed = None
    if resp.status_code >= 300:
        if parsed:
            task_id = _extract_task_id(parsed)
            status = _extract_status(parsed) or "unknown"
            detail = _extract_error(parsed) or str(resp.text[:800] or "PiAPI request failed").strip()
            suffix = f" [task_id={task_id}; status={status}]" if task_id else ""
            raise SeedanceKieError(f"PiAPI request failed ({resp.status_code}){suffix}: {detail}")
        raise SeedanceKieError(f"PiAPI request failed ({resp.status_code}): {resp.text[:800]}")
    if parsed is None:
        try:
            data = resp.json()
        except Exception as exc:
            raise SeedanceKieError("PiAPI returned invalid JSON") from exc
    else:
        data = parsed
    code = data.get("code") if isinstance(data, dict) else None
    if code not in (None, 0, 200, "0", "200"):
        raise SeedanceKieError(_extract_error(data) or f"PiAPI returned error code {code}")
    return data if isinstance(data, dict) else {"data": data}


async def _create_task(*, model: str, input_payload: Dict[str, Any]) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "model": "seedance",
        "task_type": seedance_kie_model_id(model),
        "input": input_payload,
        "config": {"service_mode": PIAPI_SEEDANCE_SERVICE_MODE},
    }
    return await _request_json("POST", "/api/v1/task", payload=body)


async def _wait_task(task_id: str) -> Dict[str, Any]:
    started = time.monotonic()
    last: Dict[str, Any] = {}
    while True:
        last = await _request_json("GET", f"/api/v1/task/{task_id}")
        status = _extract_status(last)
        if status in {"completed", "failed"}:
            return last
        if (time.monotonic() - started) >= PIAPI_SEEDANCE_TIMEOUT_SECONDS:
            raise SeedanceKieError(f"Seedance timeout. Last status: {status or 'unknown'}")
        await asyncio.sleep(PIAPI_SEEDANCE_POLL_SECONDS)


async def _run_seedance_task(*, model: str, input_payload: Dict[str, Any]) -> str:
    created = await _create_task(model=model, input_payload=input_payload)
    task_id = _extract_task_id(created)
    if not task_id:
        raise SeedanceKieError(f"PiAPI did not return task_id: {created}")
    done = await _wait_task(task_id)
    status = _extract_status(done)
    if status != "completed":
        raise SeedanceKieError(_extract_error(done) or f"Seedance task finished with status: {status or 'unknown'}")
    video_url = _extract_video_url(done)
    if not video_url:
        raise SeedanceKieError(f"Seedance task completed but no video url was returned: {done}")
    return video_url


async def run_seedance_kie_text_to_video(*, model: Any, prompt: str, duration: Any, aspect_ratio: Any = "16:9") -> str:
    normalized_model = normalize_seedance_kie_model(model)
    clean_prompt = str(prompt or "").strip()
    if not clean_prompt:
        raise SeedanceKieError("Seedance prompt is required")
    input_payload: Dict[str, Any] = {
        "prompt": clean_prompt,
        "mode": "text_to_video",
        "duration": normalize_seedance_kie_duration(duration),
        "aspect_ratio": normalize_seedance_kie_aspect_ratio(aspect_ratio),
    }
    return await _run_seedance_task(model=normalized_model, input_payload=input_payload)


async def run_seedance_kie_image_to_video(
    *,
    user_id: int,
    model: Any,
    prompt: str,
    duration: Any,
    aspect_ratio: Any = "16:9",
    start_frame: bytes | None = None,
    last_frame: bytes | None = None,
    reference_images: Sequence[bytes] | None = None,
    reference_audios: Sequence[bytes] | None = None,
) -> str:
    normalized_model = normalize_seedance_kie_model(model)
    clean_prompt = str(prompt or "").strip()
    if not clean_prompt:
        raise SeedanceKieError("Seedance prompt is required")

    extra_images = list(reference_images or [])[:7]
    audio_refs = list(reference_audios or [])[:3]
    start_raw = start_frame if start_frame else None
    last_raw = last_frame if last_frame else None

    only_frame_refs = bool(start_raw or last_raw) and not extra_images and not audio_refs
    if only_frame_refs:
        image_payload = [raw for raw in [start_raw, last_raw] if raw]
        image_urls = await _upload_files(int(user_id), image_payload[:2], "image")
        if not image_urls:
            raise SeedanceKieError("Seedance Image→Video requires at least one image reference")
        input_payload: Dict[str, Any] = {
            "prompt": clean_prompt,
            "mode": "first_last_frames",
            "duration": normalize_seedance_kie_duration(duration),
            "image_urls": image_urls,
        }
        return await _run_seedance_task(model=normalized_model, input_payload=input_payload)

    image_payload: List[bytes] = []
    if start_raw:
        image_payload.append(start_raw)
    image_payload.extend(extra_images)
    if last_raw:
        image_payload.append(last_raw)

    image_urls = await _upload_files(int(user_id), image_payload[:7], "image")
    if not image_urls:
        raise SeedanceKieError("Seedance Image→Video requires at least one image reference")
    audio_urls = await _upload_files(int(user_id), audio_refs, "audio")
    input_payload = {
        "prompt": clean_prompt,
        "mode": "omni_reference",
        "duration": normalize_seedance_kie_duration(duration),
        "aspect_ratio": normalize_seedance_kie_aspect_ratio(aspect_ratio),
        "image_urls": image_urls,
    }
    if audio_urls:
        input_payload["audio_urls"] = audio_urls
    return await _run_seedance_task(model=normalized_model, input_payload=input_payload)


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
    if _looks_like_mp3(head):
        return "mp3"
    return default


def _guess_mime(ext_or_name: str, data: bytes) -> str:
    name = str(ext_or_name or "").lower()
    if name.endswith(".png") or name == "png" or data.startswith(b"\x89PNG"):
        return "image/png"
    if name.endswith(".jpg") or name.endswith(".jpeg") or name == "jpg" or data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if name.endswith(".webp") or name == "webp" or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"):
        return "image/webp"
    if name.endswith(".gif") or name == "gif" or data.startswith(b"GIF8"):
        return "image/gif"
    if name.endswith(".wav") or name == "wav" or (data[:4] == b"RIFF" and data[8:12] == b"WAVE"):
        return "audio/wav"
    if name.endswith(".ogg") or name == "ogg" or data[:4] == b"OggS":
        return "audio/ogg"
    if name.endswith(".m4a") or name == "m4a":
        return "audio/mp4"
    if name.endswith(".mp3") or name == "mp3" or _looks_like_mp3(data):
        return "audio/mpeg"
    return "application/octet-stream"
