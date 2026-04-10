from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict, List, Optional, Sequence
from uuid import uuid4

import httpx

PIXVERSE_API_BASE = (os.getenv("PIXVERSE_API_BASE", "https://app-api.pixverse.ai") or "https://app-api.pixverse.ai").strip().rstrip("/")
PIXVERSE_API_KEY = (os.getenv("PIXVERSE_API_KEY") or "").strip()
PIXVERSE_TIMEOUT_SECONDS = float(os.getenv("PIXVERSE_TIMEOUT_SECONDS", "1800") or "1800")
PIXVERSE_POLL_SECONDS = float(os.getenv("PIXVERSE_POLL_SECONDS", "5") or "5")

PIXVERSE_C1_ALLOWED_MODES = ("text_to_video", "image_to_video", "transition", "fusion")
PIXVERSE_C1_ALLOWED_DURATIONS = (5, 10, 15)
PIXVERSE_C1_ALLOWED_QUALITIES = ("360p", "540p", "720p", "1080p")
PIXVERSE_C1_ALLOWED_ASPECT_RATIOS = ("16:9", "4:3", "1:1", "3:4", "9:16", "2:3", "3:2", "21:9")
PIXVERSE_C1_TOKEN_MAP = {
    "360p": {5: 2, 10: 4, 15: 6},
    "540p": {5: 2, 10: 5, 15: 7},
    "720p": {5: 3, 10: 6, 15: 9},
    "1080p": {5: 5, 10: 11, 15: 16},
}
PIXVERSE_C1_STATUS_PROCESSING = {5}
PIXVERSE_C1_STATUS_SUCCESS = {1}
PIXVERSE_C1_STATUS_MODERATION_FAILED = {7}
PIXVERSE_C1_STATUS_FAILED = {8}


class PixVerseC1Error(RuntimeError):
    pass


def normalize_pixverse_c1_mode(value: Any, default: str = "text_to_video") -> str:
    raw = str(value or default).strip().lower()
    if raw in {"text", "text_to_video", "t2v", "text2video"}:
        return "text_to_video"
    if raw in {"image", "image_to_video", "i2v", "image2video"}:
        return "image_to_video"
    if raw in {"transition", "first_last_frame", "first-last-frame", "first_last", "firstlast", "first_last_to_video"}:
        return "transition"
    if raw in {"fusion", "reference_to_video", "reference-video", "reference_to_video_fusion", "reference"}:
        return "fusion"
    return default


def normalize_pixverse_c1_duration(value: Any, default: int = 5) -> int:
    try:
        out = int(value)
    except Exception:
        out = int(default)
    if out <= PIXVERSE_C1_ALLOWED_DURATIONS[0]:
        return PIXVERSE_C1_ALLOWED_DURATIONS[0]
    if out >= PIXVERSE_C1_ALLOWED_DURATIONS[-1]:
        return PIXVERSE_C1_ALLOWED_DURATIONS[-1]
    return min(PIXVERSE_C1_ALLOWED_DURATIONS, key=lambda item: (abs(item - out), item))


def normalize_pixverse_c1_quality(value: Any, default: str = "720p") -> str:
    raw = str(value or default).strip().lower()
    if raw in {"360", "360p"}:
        return "360p"
    if raw in {"540", "540p"}:
        return "540p"
    if raw in {"720", "720p"}:
        return "720p"
    if raw in {"1080", "1080p"}:
        return "1080p"
    return default


def normalize_pixverse_c1_aspect_ratio(value: Any, default: str = "16:9") -> str:
    raw = str(value or default).strip()
    if raw in PIXVERSE_C1_ALLOWED_ASPECT_RATIOS:
        return raw
    return default


def pixverse_c1_tokens_for_duration(quality: Any, duration: Any) -> int:
    normalized_quality = normalize_pixverse_c1_quality(quality)
    normalized_duration = normalize_pixverse_c1_duration(duration)
    return int(PIXVERSE_C1_TOKEN_MAP[normalized_quality][normalized_duration])


def _auth_headers(*, trace_id: Optional[str] = None, content_type: Optional[str] = "application/json") -> Dict[str, str]:
    if not PIXVERSE_API_KEY:
        raise PixVerseC1Error("PIXVERSE_API_KEY is not configured")
    headers = {
        "API-KEY": PIXVERSE_API_KEY,
        "Ai-trace-id": str(trace_id or uuid4()),
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def _extract_resp(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, dict):
        resp = payload.get("Resp")
        if isinstance(resp, dict):
            return resp
        return payload
    return {}


def _extract_error(payload: Any) -> str:
    if isinstance(payload, dict):
        err = str(payload.get("ErrMsg") or payload.get("message") or payload.get("error") or "").strip()
        resp = payload.get("Resp")
        if isinstance(resp, dict):
            detail = str(resp.get("message") or resp.get("error") or resp.get("detail") or "").strip()
            if detail:
                return detail
        return err
    return ""


def _extract_video_id(payload: Any) -> str:
    resp = _extract_resp(payload)
    for key in ("video_id", "id"):
        value = str(resp.get(key) or "").strip()
        if value:
            return value
    return ""


def extract_pixverse_video_url(payload: Any) -> Optional[str]:
    resp = _extract_resp(payload)
    for key in ("url", "video_url", "download_url"):
        value = resp.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    return None


def extract_pixverse_status(payload: Any) -> int:
    resp = _extract_resp(payload)
    try:
        return int(resp.get("status") or 0)
    except Exception:
        return 0


async def _request_json(method: str, path: str, *, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"{PIXVERSE_API_BASE}{path}"
    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.request(method.upper(), url, headers=_auth_headers(), json=payload)
    if resp.status_code >= 300:
        raise PixVerseC1Error(f"PixVerse request failed ({resp.status_code}): {resp.text[:800]}")
    try:
        data = resp.json()
    except Exception as exc:
        raise PixVerseC1Error("PixVerse returned invalid JSON") from exc
    if isinstance(data, dict) and int(data.get("ErrCode") or 0) != 0:
        raise PixVerseC1Error(_extract_error(data) or f"PixVerse error code {data.get('ErrCode')}")
    return data if isinstance(data, dict) else {"Resp": data}


async def upload_pixverse_image(*, image_bytes: bytes, filename_hint: Optional[str] = None) -> Dict[str, Any]:
    if not image_bytes:
        raise PixVerseC1Error("Empty PixVerse image upload")
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
    url = f"{PIXVERSE_API_BASE}/openapi/v2/image/upload"
    files = {"image": (f"upload.{ext}", image_bytes, mime)}
    async with httpx.AsyncClient(timeout=180.0) as client:
        resp = await client.post(url, headers=_auth_headers(content_type=None), files=files)
    if resp.status_code >= 300:
        raise PixVerseC1Error(f"PixVerse image upload failed ({resp.status_code}): {resp.text[:800]}")
    try:
        data = resp.json()
    except Exception as exc:
        raise PixVerseC1Error("PixVerse image upload returned invalid JSON") from exc
    if isinstance(data, dict) and int(data.get("ErrCode") or 0) != 0:
        raise PixVerseC1Error(_extract_error(data) or f"PixVerse error code {data.get('ErrCode')}")
    resp_data = _extract_resp(data)
    img_id = resp_data.get("img_id")
    try:
        img_id_int = int(img_id)
    except Exception as exc:
        raise PixVerseC1Error(f"PixVerse image upload did not return img_id: {data}") from exc
    return {"img_id": img_id_int, "img_url": str(resp_data.get("img_url") or "").strip()}


async def create_pixverse_c1_text_to_video(*, prompt: str, duration: Any, quality: Any, aspect_ratio: Any, generate_audio: bool = True) -> str:
    payload = {
        "model": "c1",
        "prompt": str(prompt or "").strip(),
        "duration": normalize_pixverse_c1_duration(duration),
        "quality": normalize_pixverse_c1_quality(quality),
        "aspect_ratio": normalize_pixverse_c1_aspect_ratio(aspect_ratio),
        "generate_audio_switch": bool(generate_audio),
    }
    if not payload["prompt"]:
        raise PixVerseC1Error("PixVerse C1 prompt is required")
    data = await _request_json("POST", "/openapi/v2/video/text/generate", payload=payload)
    video_id = _extract_video_id(data)
    if not video_id:
        raise PixVerseC1Error(f"PixVerse C1 did not return video_id: {data}")
    return video_id


async def create_pixverse_c1_image_to_video(*, prompt: str, duration: Any, quality: Any, start_frame_img_id: int, generate_audio: bool = True) -> str:
    payload = {
        "model": "c1",
        "prompt": str(prompt or "").strip(),
        "duration": normalize_pixverse_c1_duration(duration),
        "quality": normalize_pixverse_c1_quality(quality),
        "img_id": int(start_frame_img_id),
        "generate_audio_switch": bool(generate_audio),
    }
    if not payload["prompt"]:
        raise PixVerseC1Error("PixVerse C1 prompt is required")
    data = await _request_json("POST", "/openapi/v2/video/img/generate", payload=payload)
    video_id = _extract_video_id(data)
    if not video_id:
        raise PixVerseC1Error(f"PixVerse C1 did not return video_id: {data}")
    return video_id


async def create_pixverse_c1_transition(*, prompt: str, duration: Any, quality: Any, first_frame_img_id: int, last_frame_img_id: int, generate_audio: bool = True) -> str:
    payload = {
        "model": "c1",
        "prompt": str(prompt or "").strip(),
        "duration": normalize_pixverse_c1_duration(duration),
        "quality": normalize_pixverse_c1_quality(quality),
        "first_frame_img": int(first_frame_img_id),
        "last_frame_img": int(last_frame_img_id),
        "generate_audio_switch": bool(generate_audio),
    }
    if not payload["prompt"]:
        raise PixVerseC1Error("PixVerse C1 prompt is required")
    data = await _request_json("POST", "/openapi/v2/video/transition/generate", payload=payload)
    video_id = _extract_video_id(data)
    if not video_id:
        raise PixVerseC1Error(f"PixVerse C1 did not return video_id: {data}")
    return video_id


async def create_pixverse_c1_fusion(
    *,
    prompt: str,
    duration: Any,
    quality: Any,
    aspect_ratio: Any,
    image_references: Sequence[Dict[str, Any]],
    generate_audio: bool = True,
) -> str:
    refs: List[Dict[str, Any]] = []
    for item in list(image_references or [])[:7]:
        try:
            img_id = int(item.get("img_id"))
        except Exception:
            continue
        ref_name = str(item.get("ref_name") or "").strip()
        if not ref_name:
            continue
        ref_type = str(item.get("type") or "subject").strip().lower() or "subject"
        if ref_type not in {"subject", "background"}:
            ref_type = "subject"
        refs.append({"img_id": img_id, "ref_name": ref_name, "type": ref_type})
    if not refs:
        raise PixVerseC1Error("PixVerse C1 Fusion requires at least one reference image")
    payload = {
        "model": "c1",
        "prompt": str(prompt or "").strip(),
        "duration": normalize_pixverse_c1_duration(duration),
        "quality": normalize_pixverse_c1_quality(quality),
        "aspect_ratio": normalize_pixverse_c1_aspect_ratio(aspect_ratio),
        "image_references": refs,
        "generate_audio_switch": bool(generate_audio),
    }
    if not payload["prompt"]:
        raise PixVerseC1Error("PixVerse C1 prompt is required")
    data = await _request_json("POST", "/openapi/v2/video/fusion/generate", payload=payload)
    video_id = _extract_video_id(data)
    if not video_id:
        raise PixVerseC1Error(f"PixVerse C1 did not return video_id: {data}")
    return video_id


async def get_pixverse_video_result(video_id: Any) -> Dict[str, Any]:
    video_id_text = str(video_id or "").strip()
    if not video_id_text:
        raise PixVerseC1Error("PixVerse video_id is required")
    return await _request_json("GET", f"/openapi/v2/video/result/{video_id_text}")


async def wait_for_pixverse_video(video_id: Any) -> str:
    started = time.monotonic()
    video_id_text = str(video_id or "").strip()
    last_payload: Dict[str, Any] = {}
    while True:
        last_payload = await get_pixverse_video_result(video_id_text)
        status = extract_pixverse_status(last_payload)
        video_url = extract_pixverse_video_url(last_payload)
        if status in PIXVERSE_C1_STATUS_SUCCESS:
            if not video_url:
                raise PixVerseC1Error(f"PixVerse C1 completed but returned no video url: {last_payload}")
            return video_url
        if status in PIXVERSE_C1_STATUS_MODERATION_FAILED:
            raise PixVerseC1Error(_extract_error(last_payload) or "PixVerse C1 moderation failed")
        if status in PIXVERSE_C1_STATUS_FAILED:
            raise PixVerseC1Error(_extract_error(last_payload) or "PixVerse C1 generation failed")
        if status not in PIXVERSE_C1_STATUS_PROCESSING and video_url:
            return video_url
        if (time.monotonic() - started) >= PIXVERSE_TIMEOUT_SECONDS:
            raise PixVerseC1Error(f"PixVerse C1 timeout. Last payload: {json.dumps(last_payload, ensure_ascii=False)[:1200]}")
        await asyncio.sleep(PIXVERSE_POLL_SECONDS)
