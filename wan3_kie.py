from __future__ import annotations

import asyncio
import json
import math
import mimetypes
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

import httpx

from kling_flow import upload_bytes_to_supabase

KIE_API_BASE = (os.getenv("KIE_API_BASE") or "https://api.kie.ai").strip().rstrip("/")
KIE_API_TOKEN = (
    os.getenv("KIE_API_TOKEN")
    or os.getenv("KIE_API_KEY")
    or os.getenv("KIE_AI_API_KEY")
    or ""
).strip()
WAN3_CALLBACK_URL = (os.getenv("WAN3_CALLBACK_URL") or "").strip()
WAN3_TIMEOUT_SECONDS = float(os.getenv("WAN3_TIMEOUT_SECONDS", "7200") or "7200")
WAN3_POLL_SECONDS = float(os.getenv("WAN3_POLL_SECONDS", "6") or "6")
WAN3_MODEL_ID = (os.getenv("WAN3_KIE_MODEL_ID") or "wan3.0-video").strip() or "wan3.0-video"
WAN3_PROMPT_MAX_CHARS = max(1, int(os.getenv("WAN3_PROMPT_MAX_CHARS", "20000") or "20000"))
WAN3_MAX_IMAGE_REFS = 10
WAN3_MAX_VIDEO_REFS = 5
WAN3_MAX_AUDIO_REFS = 5
WAN3_MAX_LINK_REFS = 1
WAN3_MAX_FILE_REFS = 1
WAN3_MAX_FILE_BYTES = 100 * 1024 * 1024
WAN3_ALLOWED_FILE_EXTS = {".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".pdf", ".txt", ".key", ".pages", ".numbers", ".md"}
WAN3_MAX_TOTAL_VIDEO_SECONDS = 15.0
WAN3_MAX_TOTAL_AUDIO_SECONDS = 15.0
WAN3_ALLOWED_ASPECT_RATIOS = ("adaptive", "16:9", "4:3", "1:1", "3:4", "9:16")
WAN3_TOKENS_PER_SEC = {
    "480p": float(os.getenv("WAN3_TOKENS_PER_SEC_480", "1") or "1"),
    "720p": float(os.getenv("WAN3_TOKENS_PER_SEC_720", "2") or "2"),
    "1080p": float(os.getenv("WAN3_TOKENS_PER_SEC_1080", "4") or "4"),
}


class Wan3KieError(RuntimeError):
    pass


class Wan3TaskFailedError(Wan3KieError):
    """KIE explicitly reported terminal `fail`; refund is safe."""


class Wan3TaskPendingError(Wan3KieError):
    """A paid KIE task exists but its final outcome is unknown; never refund/recreate."""


def normalize_wan3_resolution(value: Any, default: str = "720p") -> str:
    raw = str(value or default).strip().lower()
    aliases = {"480": "480p", "480p": "480p", "720": "720p", "720p": "720p", "1080": "1080p", "1080p": "1080p"}
    return aliases.get(raw, default if default in WAN3_TOKENS_PER_SEC else "720p")


def normalize_wan3_duration(value: Any, default: int = 5) -> int:
    try:
        out = int(value)
    except Exception:
        out = int(default)
    return max(2, min(30, out))


def normalize_wan3_aspect_ratio(value: Any, default: str = "adaptive") -> str:
    raw = str(value or default).strip()
    return raw if raw in WAN3_ALLOWED_ASPECT_RATIOS else default


def normalize_wan3_mode(value: Any, default: str = "text_to_video") -> str:
    raw = str(value or default).strip().lower().replace("-", "_")
    if raw in {"omni", "reference", "refs", "omni_reference", "reference_to_video", "multimodal"}:
        return "omni_reference"
    if raw in {"image", "i2v", "image_to_video", "first_last_frame", "first_last"}:
        return "image_to_video"
    return "text_to_video"


def wan3_tokens_for_run(resolution: Any, duration: Any) -> int:
    res = normalize_wan3_resolution(resolution)
    sec = normalize_wan3_duration(duration)
    return max(1, int(math.ceil(float(sec) * float(WAN3_TOKENS_PER_SEC[res]))))


def wan3_pricing_breakdown(resolution: Any, duration: Any) -> Dict[str, Any]:
    res = normalize_wan3_resolution(resolution)
    sec = normalize_wan3_duration(duration)
    rate = float(WAN3_TOKENS_PER_SEC[res])
    return {"resolution": res, "duration": sec, "tokens_per_sec": rate, "tokens": wan3_tokens_for_run(res, sec)}


def _auth_headers() -> Dict[str, str]:
    if not KIE_API_TOKEN:
        raise Wan3KieError("KIE API token is not configured. Set KIE_API_TOKEN or KIE_API_KEY.")
    return {"Authorization": f"Bearer {KIE_API_TOKEN}", "Content-Type": "application/json"}


def _clean_prompt(prompt: Any) -> str:
    text = str(prompt or "").strip()
    if not text:
        raise Wan3KieError("Wan 3.0 prompt is required")
    if len(text) > WAN3_PROMPT_MAX_CHARS:
        raise Wan3KieError(f"Wan 3.0 prompt is too long. Maximum: {WAN3_PROMPT_MAX_CHARS} characters")
    return text


def _extract_task_id(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("taskId", "task_id", "id"):
            value = payload.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                return str(value).strip()
        for key in ("data", "result"):
            found = _extract_task_id(payload.get(key))
            if found:
                return found
    return ""


def _extract_video_url(value: Any) -> str:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        if text.startswith("http://") or text.startswith("https://"):
            return text
        try:
            decoded = json.loads(text)
        except Exception:
            return ""
        return _extract_video_url(decoded)
    if isinstance(value, list):
        for item in value:
            found = _extract_video_url(item)
            if found:
                return found
        return ""
    if isinstance(value, dict):
        preferred = ("resultUrls", "result_urls", "videoUrl", "video_url", "url", "output", "result", "data")
        for key in preferred:
            if key in value:
                found = _extract_video_url(value.get(key))
                if found:
                    return found
        for item in value.values():
            found = _extract_video_url(item)
            if found:
                return found
    return ""


async def _request_json(client: httpx.AsyncClient, method: str, path: str, *, payload: Optional[Dict[str, Any]] = None, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    resp = await client.request(method, f"{KIE_API_BASE}{path}", headers=_auth_headers(), json=payload, params=params)
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}
    if resp.status_code >= 400:
        detail = (data.get("msg") or data.get("message") or data.get("error") or resp.text[:800]) if isinstance(data, dict) else resp.text[:800]
        raise Wan3KieError(f"KIE Wan 3.0 request failed ({resp.status_code}): {detail}")
    if isinstance(data, dict):
        code = str(data.get("code") or "200")
        msg = str(data.get("msg") or data.get("message") or "").strip().lower()
        if code not in {"0", "200"} and msg != "success":
            raise Wan3KieError(f"KIE Wan 3.0 API error: {data.get('msg') or data.get('message') or data.get('error') or data}")
    return data if isinstance(data, dict) else {"data": data}


async def _notify_task_id(on_task_id: Optional[Callable[[str], Any]], task_id: str) -> None:
    if not on_task_id or not task_id:
        return
    attempt = 0
    while True:
        attempt += 1
        try:
            result = on_task_id(task_id)
            if hasattr(result, "__await__"):
                await result
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if attempt == 1 or attempt % 12 == 0:
                print(f"Wan 3.0 KIE: taskId persistence pending task_id={task_id} attempt={attempt}: {exc}", flush=True)
            await asyncio.sleep(min(5.0, 1.0 + attempt * 0.25))


async def wait_wan3_task(task_id: str) -> str:
    task_id = str(task_id or "").strip()
    if not task_id:
        raise Wan3KieError("Wan 3.0 taskId is empty")
    timeout = httpx.Timeout(max(60.0, WAN3_TIMEOUT_SECONDS + 120.0), connect=60.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        started = time.monotonic()
        last_state = ""
        last_poll_error = ""
        while True:
            try:
                done = await _request_json(client, "GET", "/api/v1/jobs/recordInfo", params={"taskId": task_id})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_poll_error = str(exc)[:800]
                if time.monotonic() - started >= WAN3_TIMEOUT_SECONDS:
                    raise Wan3TaskPendingError(f"Wan 3.0 task status is still unknown for taskId={task_id}; state={last_state or 'unknown'}; error={last_poll_error}") from exc
                await asyncio.sleep(max(1.0, WAN3_POLL_SECONDS))
                continue
            data = done.get("data") if isinstance(done, dict) else None
            data = data if isinstance(data, dict) else {}
            state = str(data.get("state") or data.get("status") or "").strip().lower()
            if state:
                last_state = state
            if state == "success":
                result = data.get("resultJson") or data.get("result") or data.get("output")
                url = _extract_video_url(result)
                if url:
                    return url
                last_poll_error = "provider state=success but result URL is not available yet"
            elif state == "fail":
                detail = data.get("failMsg") or data.get("errorMessage") or data.get("msg") or data.get("message") or data
                raise Wan3TaskFailedError(f"KIE Wan 3.0 task failed: {detail}")
            if time.monotonic() - started >= WAN3_TIMEOUT_SECONDS:
                raise Wan3TaskPendingError(f"Wan 3.0 task is not terminal yet for taskId={task_id}; state={last_state or 'unknown'}; detail={last_poll_error or 'polling timeout'}")
            await asyncio.sleep(max(1.0, WAN3_POLL_SECONDS))


def _validate_urls(values: Sequence[str] | None, *, limit: int, label: str) -> List[str]:
    out = [str(x or "").strip() for x in list(values or []) if str(x or "").strip()]
    if len(out) > limit:
        raise Wan3KieError(f"Wan 3.0 supports maximum {limit} {label} references")
    for url in out:
        if not re.match(r"^https?://", url, flags=re.I):
            raise Wan3KieError(f"Wan 3.0 {label} reference must be an http(s) URL")
    return out


def _guess_media(raw: bytes, kind: str, filename: str = "") -> tuple[str, str]:
    name = str(filename or "").lower()
    head = bytes(raw[:32])
    if kind == "image":
        if head.startswith(b"\x89PNG") or name.endswith(".png"):
            return "png", "image/png"
        if head[:3] == b"\xff\xd8\xff" or name.endswith((".jpg", ".jpeg")):
            return "jpg", "image/jpeg"
        if (head.startswith(b"RIFF") and head[8:12] == b"WEBP") or name.endswith(".webp"):
            return "webp", "image/webp"
        if head.startswith(b"BM") or name.endswith(".bmp"):
            return "bmp", "image/bmp"
        raise Wan3KieError("Wan 3.0 image reference must be JPEG, PNG, WEBP or BMP")
    if kind == "audio":
        if name.endswith(".wav") or (head.startswith(b"RIFF") and head[8:12] == b"WAVE"): return "wav", "audio/wav"
        return "mp3", "audio/mpeg"
    if name.endswith(".mov"): return "mov", "video/quicktime"
    return "mp4", "video/mp4"


def upload_wan3_reference_bytes(user_id: int, kind: str, idx: int, raw: bytes, *, filename: str = "") -> str:
    if not raw:
        raise Wan3KieError("Wan 3.0 reference file is empty")
    kind = str(kind or "").strip().lower()
    if kind not in {"image", "video", "audio", "frame"}:
        raise Wan3KieError(f"Unsupported Wan 3.0 reference kind: {kind}")
    media_kind = "image" if kind == "frame" else kind
    size_limits = {"image": 20 * 1024 * 1024, "video": 100 * 1024 * 1024, "audio": 15 * 1024 * 1024}
    if len(raw) > size_limits[media_kind]:
        raise Wan3KieError(f"Wan 3.0 {media_kind} reference exceeds provider file-size limit")
    ext, content_type = _guess_media(raw, media_kind, filename)
    path = f"wan3_inputs/{int(user_id)}/{int(time.time() * 1000)}_{int(idx)}_{kind}.{ext}"
    return upload_bytes_to_supabase(path, bytes(raw), content_type)


def upload_wan3_file_reference_bytes(user_id: int, raw: bytes, *, filename: str) -> str:
    if not raw:
        raise Wan3KieError("Wan 3.0 file reference is empty")
    if len(raw) > WAN3_MAX_FILE_BYTES:
        raise Wan3KieError("Wan 3.0 file reference maximum size is 100 MB")
    safe_name = Path(str(filename or "").strip()).name
    suffix = Path(safe_name).suffix.lower()
    if suffix not in WAN3_ALLOWED_FILE_EXTS:
        allowed = ", ".join(sorted(x.lstrip(".") for x in WAN3_ALLOWED_FILE_EXTS))
        raise Wan3KieError(f"Wan 3.0 file reference format is not supported. Allowed: {allowed}")
    content_type = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
    stem = re.sub(r"[^a-zA-Z0-9_-]+", "_", Path(safe_name).stem).strip("_")[:48] or "reference"
    path = f"wan3_inputs/{int(user_id)}/{int(time.time() * 1000)}_file_{stem}{suffix}"
    return upload_bytes_to_supabase(path, bytes(raw), content_type)


async def _run_task(*, input_payload: Dict[str, Any], on_task_id: Optional[Callable[[str], Any]] = None, resume_task_id: str = "") -> str:
    task_id = str(resume_task_id or "").strip()
    if not task_id:
        body: Dict[str, Any] = {"model": WAN3_MODEL_ID, "input": input_payload}
        if WAN3_CALLBACK_URL:
            body["callBackUrl"] = WAN3_CALLBACK_URL
        timeout = httpx.Timeout(max(60.0, WAN3_TIMEOUT_SECONDS + 120.0), connect=60.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            created = await _request_json(client, "POST", "/api/v1/jobs/createTask", payload=body)
        task_id = _extract_task_id(created)
        if not task_id:
            raise Wan3KieError(f"KIE Wan 3.0 createTask did not return taskId: {created}")
        await _notify_task_id(on_task_id, task_id)
    return await wait_wan3_task(task_id)


async def run_wan3_video(
    *,
    user_id: int,
    prompt: str,
    resolution: Any,
    duration: Any,
    aspect_ratio: Any = "adaptive",
    audio: bool = True,
    seed: Any = None,
    first_frame: Optional[bytes] = None,
    last_frame: Optional[bytes] = None,
    first_frame_url: str = "",
    last_frame_url: str = "",
    reference_images: Sequence[bytes] | None = None,
    reference_videos: Sequence[bytes] | None = None,
    reference_audios: Sequence[bytes] | None = None,
    reference_image_urls: Sequence[str] | None = None,
    reference_video_urls: Sequence[str] | None = None,
    reference_audio_urls: Sequence[str] | None = None,
    reference_link_urls: Sequence[str] | None = None,
    reference_file_urls: Sequence[str] | None = None,
    on_task_id: Optional[Callable[[str], Any]] = None,
    resume_task_id: str = "",
) -> str:
    if str(resume_task_id or "").strip():
        return await _run_task(input_payload={}, on_task_id=on_task_id, resume_task_id=resume_task_id)

    res = normalize_wan3_resolution(resolution)
    dur = normalize_wan3_duration(duration)
    payload: Dict[str, Any] = {
        "prompt": _clean_prompt(prompt),
        "resolution": res.upper(),
        "duration": dur,
        "aspect_ratio": normalize_wan3_aspect_ratio(aspect_ratio),
        "audio": bool(audio),
    }
    if seed not in (None, ""):
        try:
            seed_int = int(seed)
        except Exception as exc:
            raise Wan3KieError("Wan 3.0 seed must be an integer") from exc
        if seed_int < 0 or seed_int > 2147483647:
            raise Wan3KieError("Wan 3.0 seed must be between 0 and 2147483647")
        payload["seed"] = seed_int

    image_urls = _validate_urls(reference_image_urls, limit=WAN3_MAX_IMAGE_REFS, label="image")
    video_urls = _validate_urls(reference_video_urls, limit=WAN3_MAX_VIDEO_REFS, label="video")
    audio_urls = _validate_urls(reference_audio_urls, limit=WAN3_MAX_AUDIO_REFS, label="audio")
    link_urls = _validate_urls(reference_link_urls, limit=WAN3_MAX_LINK_REFS, label="webpage")
    file_urls = _validate_urls(reference_file_urls, limit=WAN3_MAX_FILE_REFS, label="file")
    if link_urls and file_urls:
        raise Wan3KieError("Wan 3.0 reference_link_urls and reference_file_urls are mutually exclusive")
    for idx, raw in enumerate([bytes(x) for x in list(reference_images or []) if x], start=1):
        image_urls.append(upload_wan3_reference_bytes(user_id, "image", idx, raw))
    for idx, raw in enumerate([bytes(x) for x in list(reference_videos or []) if x], start=1):
        video_urls.append(upload_wan3_reference_bytes(user_id, "video", idx, raw))
    for idx, raw in enumerate([bytes(x) for x in list(reference_audios or []) if x], start=1):
        audio_urls.append(upload_wan3_reference_bytes(user_id, "audio", idx, raw))
    if len(image_urls) > WAN3_MAX_IMAGE_REFS or len(video_urls) > WAN3_MAX_VIDEO_REFS or len(audio_urls) > WAN3_MAX_AUDIO_REFS:
        raise Wan3KieError("Wan 3.0 reference count exceeds provider limits")

    ff_url = str(first_frame_url or "").strip()
    lf_url = str(last_frame_url or "").strip()
    if first_frame:
        ff_url = upload_wan3_reference_bytes(user_id, "frame", 1, first_frame)
    if last_frame:
        lf_url = upload_wan3_reference_bytes(user_id, "frame", 2, last_frame)
    if (ff_url or lf_url) and (image_urls or video_urls or audio_urls or link_urls or file_urls):
        raise Wan3KieError("Wan 3.0 frame mode cannot be combined with reference media, webpage or file")
    if lf_url and not ff_url:
        raise Wan3KieError("Wan 3.0 last frame requires first frame")
    if ff_url:
        payload["first_frame_url"] = ff_url
    if lf_url:
        payload["last_frame_url"] = lf_url
    if image_urls:
        payload["reference_image_urls"] = image_urls
    if video_urls:
        payload["reference_video_urls"] = video_urls
    if audio_urls:
        payload["reference_audio_urls"] = audio_urls
    if link_urls:
        payload["reference_link_urls"] = link_urls
    if file_urls:
        payload["reference_file_urls"] = file_urls

    return await _run_task(input_payload=payload, on_task_id=on_task_id, resume_task_id="")
