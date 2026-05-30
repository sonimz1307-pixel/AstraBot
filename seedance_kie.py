from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
from typing import Any, Dict, List, Optional, Sequence

import httpx

from kling_flow import KlingFlowError, upload_bytes_to_supabase

KIE_API_BASE = (os.getenv("KIE_API_BASE") or "https://api.kie.ai").strip().rstrip("/")
KIE_API_TOKEN = (
    os.getenv("KIE_API_TOKEN")
    or os.getenv("KIE_API_KEY")
    or os.getenv("KIE_AI_API_KEY")
    or ""
).strip()
KIE_SEEDANCE_CALLBACK_URL = (os.getenv("KIE_SEEDANCE_CALLBACK_URL") or "").strip()
KIE_SEEDANCE_TIMEOUT_SECONDS = float(os.getenv("KIE_SEEDANCE_TIMEOUT_SECONDS", "7200") or "7200")
KIE_SEEDANCE_POLL_SECONDS = float(os.getenv("KIE_SEEDANCE_POLL_SECONDS", "6") or "6")

# User-facing ordinary Seedance 2.0 presets.
# Preview/Fast Preview are still handled by the separate PiAPI provider and must not be routed here.
SEEDANCE_KIE_ALLOWED_MODELS = {
    "seedance-kie-480p",
    "seedance-kie-720p",
    "seedance-kie-1080p",
}
SEEDANCE_KIE_ALLOWED_DURATIONS = (5, 10, 15)
SEEDANCE_KIE_ALLOWED_ASPECT_RATIOS = ("16:9", "9:16", "1:1")
SEEDANCE_KIE_PROMPT_MAX_CHARS = int(os.getenv("KIE_SEEDANCE_PROMPT_MAX_CHARS", "20000") or "20000")
SEEDANCE_KIE_MAX_IMAGE_REFS = int(os.getenv("KIE_SEEDANCE_MAX_IMAGE_REFS", "7") or "7")
SEEDANCE_KIE_MAX_AUDIO_REFS = int(os.getenv("KIE_SEEDANCE_MAX_AUDIO_REFS", "3") or "3")
SEEDANCE_KIE_MAX_VIDEO_REFS = int(os.getenv("KIE_SEEDANCE_MAX_VIDEO_REFS", "3") or "3")
SEEDANCE_KIE_MAX_TOTAL_OMNI_REFS = int(os.getenv("KIE_SEEDANCE_MAX_TOTAL_OMNI_REFS", "12") or "12")

# Final retail prices approved for 5 / 10 / 15 seconds.
# Do not derive these base prices from provider rates: product pricing is fixed by business rules.
SEEDANCE_KIE_TOKEN_MAP = {
    "seedance-kie-480p": {5: 6, 10: 12, 15: 18},
    "seedance-kie-720p": {5: 12, 10: 24, 15: 33},
    "seedance-kie-1080p": {5: 28, 10: 55, 15: 80},
}

# KIE provider pricing for Seedance 2.0 video-input billing only.
# Important: without video input the platform keeps the approved retail grid above.
# With video input the provider bills Price × (Input seconds + Output seconds), so we only add
# a dynamic surcharge when that real provider cost is higher than the approved base price.
SEEDANCE_KIE_USD_RUB = float(os.getenv("SEEDANCE_KIE_USD_RUB", "100") or "100")
SEEDANCE_KIE_TOKEN_RUB = float(os.getenv("SEEDANCE_KIE_TOKEN_RUB", "8") or "8")
SEEDANCE_KIE_VIDEO_REF_METADATA_TOLERANCE_SEC = float(os.getenv("SEEDANCE_KIE_VIDEO_REF_METADATA_TOLERANCE_SEC", "0.25") or "0.25")

SEEDANCE_KIE_PROVIDER_USD_PER_SEC = {
    "seedance-kie-480p": {"with_video": 0.0575, "no_video": 0.095},
    "seedance-kie-720p": {"with_video": 0.125, "no_video": 0.205},
    "seedance-kie-1080p": {"with_video": 0.31, "no_video": 0.51},
}


def _seedance_kie_tokens_from_usd(cost_usd: float) -> int:
    if SEEDANCE_KIE_TOKEN_RUB <= 0:
        return 1
    return max(1, int(math.ceil(float(cost_usd or 0.0) * SEEDANCE_KIE_USD_RUB / SEEDANCE_KIE_TOKEN_RUB)))


def seedance_kie_billable_input_video_seconds(value: Any) -> int:
    try:
        seconds = float(value or 0.0)
    except Exception:
        seconds = 0.0
    if seconds <= 0:
        return 0
    # Avoid charging 16s for common metadata such as 15.03s, while never rounding down a real 15.6s clip.
    return max(1, int(math.ceil(seconds - SEEDANCE_KIE_VIDEO_REF_METADATA_TOLERANCE_SEC)))


def _seedance_kie_cost_usd(model: Any, duration: Any, *, input_video_duration_sec: Any = 0) -> float:
    normalized_model = normalize_seedance_kie_model(model)
    normalized_duration = normalize_seedance_kie_duration(duration)
    input_seconds = seedance_kie_billable_input_video_seconds(input_video_duration_sec)
    rates = SEEDANCE_KIE_PROVIDER_USD_PER_SEC[normalized_model]
    if input_seconds > 0:
        return float(rates["with_video"]) * float(normalized_duration + input_seconds)
    return float(rates["no_video"]) * float(normalized_duration)


def _seedance_kie_base_tokens(model: Any, duration: Any) -> int:
    normalized_model = normalize_seedance_kie_model(model)
    normalized_duration = normalize_seedance_kie_duration(duration)
    return int(SEEDANCE_KIE_TOKEN_MAP[normalized_model][normalized_duration])


# Kept for old callers/imports; dynamic video-reference billing is handled by seedance_kie_tokens_for_duration(..., input_video_duration_sec=...).
SEEDANCE_KIE_VIDEO_REFERENCE_SURCHARGE = {
    "seedance-kie-480p": 0,
    "seedance-kie-720p": 0,
    "seedance-kie-1080p": 0,
}

SEEDANCE_KIE_MODEL_IDS = {
    "seedance-kie-480p": "bytedance/seedance-2-fast",
    "seedance-kie-720p": "bytedance/seedance-2",
    "seedance-kie-1080p": "bytedance/seedance-2",
}
SEEDANCE_KIE_RESOLUTIONS = {
    "seedance-kie-480p": "480p",
    "seedance-kie-720p": "720p",
    "seedance-kie-1080p": "1080p",
}
SEEDANCE_KIE_DISPLAY_NAMES = {
    "seedance-kie-480p": "Seedance 2.0 480p",
    "seedance-kie-720p": "Seedance 2.0 720p",
    "seedance-kie-1080p": "Seedance 2.0 1080p",
}


class SeedanceKieError(RuntimeError):
    pass


def normalize_seedance_kie_model(value: Any, default: str = "seedance-kie-720p") -> str:
    raw = str(value or default).strip().lower().replace("_", "-")
    aliases = {
        "480": "seedance-kie-480p",
        "480p": "seedance-kie-480p",
        "seedance-480p": "seedance-kie-480p",
        "seedance-2-480p": "seedance-kie-480p",
        "seedance-kie-480": "seedance-kie-480p",
        "seedance-kie-480p": "seedance-kie-480p",
        "seedance-kie-fast": "seedance-kie-480p",
        "seedance-2-fast": "seedance-kie-480p",
        "seedance-fast": "seedance-kie-480p",
        "fast": "seedance-kie-480p",
        "720": "seedance-kie-720p",
        "720p": "seedance-kie-720p",
        "seedance-720p": "seedance-kie-720p",
        "seedance-2-720p": "seedance-kie-720p",
        "seedance-kie-720": "seedance-kie-720p",
        "seedance-kie-720p": "seedance-kie-720p",
        "seedance-kie": "seedance-kie-720p",
        "seedance-2": "seedance-kie-720p",
        "seedance": "seedance-kie-720p",
        "standard": "seedance-kie-720p",
        "1080": "seedance-kie-1080p",
        "1080p": "seedance-kie-1080p",
        "seedance-1080p": "seedance-kie-1080p",
        "seedance-2-1080p": "seedance-kie-1080p",
        "seedance-kie-1080": "seedance-kie-1080p",
        "seedance-kie-1080p": "seedance-kie-1080p",
    }
    return aliases.get(raw, default if default in SEEDANCE_KIE_ALLOWED_MODELS else "seedance-kie-720p")


def normalize_seedance_kie_mode(value: Any, default: str = "text_to_video") -> str:
    raw = str(value or default).strip().lower()
    if raw in {"omni", "omni_reference", "omni-reference", "reference", "refs", "multimodal", "reference_to_video"}:
        return "omni_reference"
    if raw in {"image", "image_to_video", "i2v", "image2video", "image->video"}:
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


def normalize_seedance_kie_aspect_ratio(value: Any, default: str = "16:9") -> str:
    raw = str(value or default).strip()
    return raw if raw in SEEDANCE_KIE_ALLOWED_ASPECT_RATIOS else default


def seedance_kie_tokens_for_duration(model: Any, duration: Any, *, input_video_duration_sec: Any = 0) -> int:
    base_tokens = _seedance_kie_base_tokens(model, duration)
    input_seconds = seedance_kie_billable_input_video_seconds(input_video_duration_sec)
    if input_seconds <= 0:
        return int(base_tokens)
    provider_tokens = _seedance_kie_tokens_from_usd(
        _seedance_kie_cost_usd(model, duration, input_video_duration_sec=input_seconds)
    )
    return max(int(base_tokens), int(provider_tokens))


def seedance_kie_pricing_breakdown(model: Any, duration: Any, *, input_video_duration_sec: Any = 0) -> Dict[str, Any]:
    normalized_model = normalize_seedance_kie_model(model)
    normalized_duration = normalize_seedance_kie_duration(duration)
    input_seconds = seedance_kie_billable_input_video_seconds(input_video_duration_sec)
    has_video_input = input_seconds > 0
    rates = SEEDANCE_KIE_PROVIDER_USD_PER_SEC[normalized_model]
    rate_key = "with_video" if has_video_input else "no_video"
    billable_seconds = normalized_duration + input_seconds if has_video_input else normalized_duration
    cost_usd = float(rates[rate_key]) * float(billable_seconds)
    base_tokens = _seedance_kie_base_tokens(normalized_model, normalized_duration)
    provider_cost_tokens = _seedance_kie_tokens_from_usd(cost_usd)
    tokens = max(base_tokens, provider_cost_tokens) if has_video_input else base_tokens
    return {
        "model": normalized_model,
        "duration": normalized_duration,
        "input_video_seconds": input_seconds,
        "billable_seconds": billable_seconds,
        "has_video_input": has_video_input,
        "provider_rate_usd_per_sec": float(rates[rate_key]),
        "provider_cost_usd": cost_usd,
        "provider_cost_tokens": provider_cost_tokens,
        "base_tokens": base_tokens,
        "video_reference_surcharge_tokens": max(0, int(tokens) - int(base_tokens)),
        "usd_rub": SEEDANCE_KIE_USD_RUB,
        "token_rub": SEEDANCE_KIE_TOKEN_RUB,
        "tokens": int(tokens),
    }


def seedance_kie_video_reference_surcharge(model: Any, duration: Any = 5, input_video_duration_sec: Any = 0) -> int:
    input_seconds = seedance_kie_billable_input_video_seconds(input_video_duration_sec)
    if input_seconds <= 0:
        return 0
    base = seedance_kie_tokens_for_duration(model, duration, input_video_duration_sec=0)
    with_video = seedance_kie_tokens_for_duration(model, duration, input_video_duration_sec=input_seconds)
    return max(0, int(with_video) - int(base))


def seedance_kie_resolution(model: Any) -> str:
    return SEEDANCE_KIE_RESOLUTIONS[normalize_seedance_kie_model(model)]


def seedance_kie_model_id(model: Any) -> str:
    return SEEDANCE_KIE_MODEL_IDS[normalize_seedance_kie_model(model)]


def seedance_kie_display_name(model: Any) -> str:
    return SEEDANCE_KIE_DISPLAY_NAMES[normalize_seedance_kie_model(model)]


def _looks_like_mp3(data: bytes) -> bool:
    head = bytes((data or b"")[:64])
    if head.startswith(b"ID3"):
        return True
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return True
    return False


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
        if brand == b"qt  ":
            return "mov"
        if brand in {b"M4A ", b"M4B ", b"M4P "}:
            return "m4a"
        if brand in {b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"MSNV", b"dash"}:
            return "mp4"
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
    if name.endswith(".mp4") or name == "mp4":
        return "video/mp4"
    if name.endswith(".mov") or name == "mov":
        return "video/quicktime"
    if name.endswith(".mp3") or name == "mp3" or _looks_like_mp3(data):
        return "audio/mpeg"
    return "application/octet-stream"


def _auth_headers() -> Dict[str, str]:
    if not KIE_API_TOKEN:
        raise SeedanceKieError("KIE API token is not configured. Set KIE_API_TOKEN or KIE_API_KEY.")
    return {"Authorization": f"Bearer {KIE_API_TOKEN}", "Content-Type": "application/json"}


def _clean_prompt(prompt: Any) -> str:
    text = str(prompt or "").strip()
    if not text:
        raise SeedanceKieError("Seedance prompt is required")
    if len(text) > SEEDANCE_KIE_PROMPT_MAX_CHARS:
        raise SeedanceKieError(f"Seedance prompt is too long. Maximum: {SEEDANCE_KIE_PROMPT_MAX_CHARS} characters")
    return text


def _max_prompt_audio_ref_index(prompt: Any) -> int:
    max_idx = 0
    for match in re.finditer(r"@audio(\d+)", str(prompt or ""), flags=re.IGNORECASE):
        try:
            max_idx = max(max_idx, int(match.group(1) or 0))
        except Exception:
            continue
    return max_idx


def _upload_public_file(user_id: int, kind: str, idx: int, raw: bytes) -> str:
    if not raw:
        raise SeedanceKieError("Empty upload payload")
    ext = _guess_ext(raw, default="bin")
    mime = _guess_mime(ext, raw)
    path = f"workspace_refs/{int(user_id)}/seedance_kie/{kind}/{int(time.time())}_{os.urandom(4).hex()}_{idx}.{ext}"
    try:
        return upload_bytes_to_supabase(path, raw, mime)
    except KlingFlowError as exc:
        raise SeedanceKieError(str(exc)) from exc
    except Exception as exc:
        raise SeedanceKieError(f"Failed to upload Seedance {kind} reference: {exc}") from exc


async def _upload_files(user_id: int, files: Sequence[bytes] | None, kind: str, *, limit: int) -> List[str]:
    urls: List[str] = []
    for idx, raw in enumerate(list(files or [])[:limit], start=1):
        if raw:
            urls.append(_upload_public_file(int(user_id), kind, idx, raw))
    return urls


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
            "videoUrl",
            "video_url",
            "resultUrl",
            "result_url",
            "url",
            "downloadUrl",
            "download_url",
        ):
            found = _extract_video_url_from_result(result.get(key))
            if found:
                return found
        for key in ("resultUrls", "result_urls", "videos", "urls", "output", "data"):
            found = _extract_video_url_from_result(result.get(key))
            if found:
                return found
        for value in result.values():
            found = _extract_video_url_from_result(value)
            if found:
                return found
    return None


async def _request_json(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    payload: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    url = f"{KIE_API_BASE}{path}"
    resp = await client.request(method.upper(), url, headers=_auth_headers(), json=payload, params=params)
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}
    if resp.status_code >= 400:
        detail = data.get("msg") or data.get("message") or data.get("error") or resp.text[:800] or f"HTTP {resp.status_code}"
        raise SeedanceKieError(f"KIE Seedance request failed ({resp.status_code}): {detail}")
    if isinstance(data, dict):
        code = str(data.get("code") or "200")
        msg = str(data.get("msg") or data.get("message") or "").strip().lower()
        if code not in {"0", "200"} and msg != "success":
            detail = data.get("msg") or data.get("message") or data.get("error") or data
            raise SeedanceKieError(f"KIE Seedance API error: {detail}")
    return data if isinstance(data, dict) else {"data": data}


async def _create_task(client: httpx.AsyncClient, *, model: str, input_payload: Dict[str, Any]) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "model": seedance_kie_model_id(model),
        "input": input_payload,
    }
    if KIE_SEEDANCE_CALLBACK_URL:
        body["callBackUrl"] = KIE_SEEDANCE_CALLBACK_URL
    return await _request_json(client, "POST", "/api/v1/jobs/createTask", payload=body)


async def _wait_task(client: httpx.AsyncClient, task_id: str) -> Dict[str, Any]:
    started = time.monotonic()
    last_state = ""
    while True:
        payload = await _request_json(client, "GET", "/api/v1/jobs/recordInfo", params={"taskId": task_id})
        data = payload.get("data") if isinstance(payload, dict) else None
        data = data if isinstance(data, dict) else {}
        state = str(data.get("state") or data.get("status") or "").strip().lower()
        if state:
            last_state = state
        if state == "success":
            return payload
        if state in {"fail", "failed", "error"}:
            detail = data.get("failMsg") or data.get("errorMessage") or data.get("msg") or data.get("message") or data
            raise SeedanceKieError(f"KIE Seedance task failed: {detail}")
        if (time.monotonic() - started) >= KIE_SEEDANCE_TIMEOUT_SECONDS:
            raise SeedanceKieError(f"Seedance timeout. Last state: {last_state or 'unknown'}")
        await asyncio.sleep(max(1.0, KIE_SEEDANCE_POLL_SECONDS))


async def _run_seedance_task(*, model: str, input_payload: Dict[str, Any]) -> str:
    timeout = httpx.Timeout(max(60.0, KIE_SEEDANCE_TIMEOUT_SECONDS + 120.0), connect=60.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        created = await _create_task(client, model=model, input_payload=input_payload)
        task_id = _extract_task_id(created)
        if not task_id:
            raise SeedanceKieError(f"KIE Seedance createTask did not return taskId: {created}")
        done = await _wait_task(client, task_id)
    data = done.get("data") if isinstance(done, dict) else None
    data = data if isinstance(data, dict) else {}
    result = data.get("resultJson") or data.get("result") or data.get("output")
    video_url = _extract_video_url_from_result(result)
    if not video_url:
        raise SeedanceKieError(f"KIE Seedance task succeeded but no video URL was returned: {data}")
    return video_url


def _base_input_payload(*, prompt: str, model: str, duration: Any, aspect_ratio: Any, generate_audio: bool = True) -> Dict[str, Any]:
    return {
        "prompt": _clean_prompt(prompt),
        "return_last_frame": False,
        "generate_audio": bool(generate_audio),
        "resolution": seedance_kie_resolution(model),
        "aspect_ratio": normalize_seedance_kie_aspect_ratio(aspect_ratio),
        "duration": normalize_seedance_kie_duration(duration),
        "web_search": False,
    }


async def run_seedance_kie_text_to_video(*, model: Any, prompt: str, duration: Any, aspect_ratio: Any = "16:9") -> str:
    normalized_model = normalize_seedance_kie_model(model)
    input_payload = _base_input_payload(prompt=prompt, model=normalized_model, duration=duration, aspect_ratio=aspect_ratio)
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
    start_raw = bytes(start_frame) if start_frame else None
    last_raw = bytes(last_frame) if last_frame else None
    extra_images = [bytes(item) for item in list(reference_images or []) if item]
    audio_refs = [bytes(item) for item in list(reference_audios or []) if item]

    # KIE docs: strict Image→Video and Multimodal Reference-to-Video are mutually exclusive.
    # Audio refs belong to Omni Reference; do not mix them into first/last-frame I2V payloads.
    if audio_refs:
        refs: List[bytes] = []
        if start_raw:
            refs.append(start_raw)
        refs.extend(extra_images)
        if last_raw:
            refs.append(last_raw)
        return await run_seedance_kie_omni_reference(
            user_id=user_id,
            model=normalized_model,
            prompt=prompt,
            duration=duration,
            aspect_ratio=aspect_ratio,
            reference_images=refs,
            reference_audios=audio_refs,
        )

    frames: List[bytes] = []
    if start_raw:
        frames.append(start_raw)
    frames.extend(extra_images)
    if last_raw:
        frames.append(last_raw)
    frames = [raw for raw in frames if raw]

    if not frames:
        raise SeedanceKieError("Seedance Image→Video requires at least one first/last frame")
    if len(frames) > 2:
        raise SeedanceKieError("Seedance Image→Video supports maximum 2 images: first frame and optional last frame")

    image_urls = await _upload_files(int(user_id), frames, "image", limit=2)
    input_payload = _base_input_payload(prompt=prompt, model=normalized_model, duration=duration, aspect_ratio=aspect_ratio)
    input_payload["first_frame_url"] = image_urls[0]
    if len(image_urls) > 1:
        input_payload["last_frame_url"] = image_urls[1]
    return await _run_seedance_task(model=normalized_model, input_payload=input_payload)


async def run_seedance_kie_omni_reference(
    *,
    user_id: int,
    model: Any,
    prompt: str,
    duration: Any,
    aspect_ratio: Any = "16:9",
    reference_images: Sequence[bytes] | None = None,
    reference_videos: Sequence[bytes] | None = None,
    reference_audios: Sequence[bytes] | None = None,
) -> str:
    normalized_model = normalize_seedance_kie_model(model)
    image_refs = [bytes(item) for item in list(reference_images or []) if item]
    video_refs = [bytes(item) for item in list(reference_videos or []) if item]
    audio_refs = [bytes(item) for item in list(reference_audios or []) if item]

    if not image_refs and not video_refs and not audio_refs:
        raise SeedanceKieError("Seedance Omni Reference requires at least one reference")
    if audio_refs and not (image_refs or video_refs):
        raise SeedanceKieError("Seedance Omni Reference does not support audio-only input")
    if len(audio_refs) > SEEDANCE_KIE_MAX_AUDIO_REFS:
        raise SeedanceKieError(f"Seedance supports maximum {SEEDANCE_KIE_MAX_AUDIO_REFS} audio references")
    if len(video_refs) > SEEDANCE_KIE_MAX_VIDEO_REFS:
        raise SeedanceKieError(f"Seedance supports maximum {SEEDANCE_KIE_MAX_VIDEO_REFS} video references")
    if len(image_refs) > SEEDANCE_KIE_MAX_IMAGE_REFS:
        raise SeedanceKieError(f"Seedance supports maximum {SEEDANCE_KIE_MAX_IMAGE_REFS} image references")
    if len(image_refs) + len(video_refs) + len(audio_refs) > SEEDANCE_KIE_MAX_TOTAL_OMNI_REFS:
        raise SeedanceKieError(f"Seedance supports maximum {SEEDANCE_KIE_MAX_TOTAL_OMNI_REFS} total references")

    image_urls = await _upload_files(int(user_id), image_refs, "image", limit=SEEDANCE_KIE_MAX_IMAGE_REFS)
    video_urls = await _upload_files(int(user_id), video_refs, "video", limit=SEEDANCE_KIE_MAX_VIDEO_REFS)
    audio_urls = await _upload_files(int(user_id), audio_refs, "audio", limit=SEEDANCE_KIE_MAX_AUDIO_REFS)

    max_audio_ref = _max_prompt_audio_ref_index(prompt)
    if max_audio_ref > len(audio_urls):
        raise SeedanceKieError(
            f"Prompt references @audio{max_audio_ref}, but only {len(audio_urls)} audio reference(s) were uploaded"
        )

    # Audio references are conditioning inputs, not a final audio track replacement.
    # Keep generated audio enabled; otherwise KIE returns a silent video when audio refs exist.
    input_payload = _base_input_payload(
        prompt=prompt,
        model=normalized_model,
        duration=duration,
        aspect_ratio=aspect_ratio,
        generate_audio=True,
    )
    if image_urls:
        input_payload["reference_image_urls"] = image_urls
    if video_urls:
        input_payload["reference_video_urls"] = video_urls
    if audio_urls:
        input_payload["reference_audio_urls"] = audio_urls
    return await _run_seedance_task(model=normalized_model, input_payload=input_payload)
