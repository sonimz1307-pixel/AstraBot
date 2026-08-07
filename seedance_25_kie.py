from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

import httpx

from kling_flow import KlingFlowError, upload_bytes_to_supabase

KIE_API_BASE = (os.getenv("KIE_API_BASE") or "https://api.kie.ai").strip().rstrip("/")
KIE_API_TOKEN = (
    os.getenv("KIE_API_TOKEN")
    or os.getenv("KIE_API_KEY")
    or os.getenv("KIE_AI_API_KEY")
    or ""
).strip()
SEEDANCE25_CALLBACK_URL = (os.getenv("SEEDANCE25_CALLBACK_URL") or os.getenv("KIE_SEEDANCE_CALLBACK_URL") or "").strip()
SEEDANCE25_TIMEOUT_SECONDS = float(os.getenv("SEEDANCE25_TIMEOUT_SECONDS", "7200") or "7200")
SEEDANCE25_POLL_SECONDS = float(os.getenv("SEEDANCE25_POLL_SECONDS", "6") or "6")

SEEDANCE25_MODEL_ID = "bytedance/seedance-2-5"
SEEDANCE25_ALLOWED_MODELS = {"seedance25-480p", "seedance25-720p"}
SEEDANCE25_ALLOWED_DURATIONS = tuple(range(4, 31))
SEEDANCE25_ALLOWED_ASPECT_RATIOS = ("1:1", "4:3", "3:4", "16:9", "9:16", "21:9", "adaptive")
SEEDANCE25_PROMPT_MAX_CHARS = max(1, int(os.getenv("SEEDANCE25_PROMPT_MAX_CHARS", "30000") or "30000"))
SEEDANCE25_MAX_IMAGE_REFS = max(1, int(os.getenv("SEEDANCE25_MAX_IMAGE_REFS", "30") or "30"))
SEEDANCE25_MAX_VIDEO_REFS = max(1, int(os.getenv("SEEDANCE25_MAX_VIDEO_REFS", "10") or "10"))
SEEDANCE25_MAX_AUDIO_REFS = max(1, int(os.getenv("SEEDANCE25_MAX_AUDIO_REFS", "10") or "10"))
SEEDANCE25_MAX_TOTAL_REFS = max(1, int(os.getenv("SEEDANCE25_MAX_TOTAL_REFS", "50") or "50"))
SEEDANCE25_MAX_TOTAL_VIDEO_SECONDS = float(os.getenv("SEEDANCE25_MAX_TOTAL_VIDEO_SECONDS", "30") or "30")
SEEDANCE25_MAX_TOTAL_AUDIO_SECONDS = float(os.getenv("SEEDANCE25_MAX_TOTAL_AUDIO_SECONDS", "30") or "30")

# Business pricing approved for NABEX. User balances are integer tokens; all results are ceil()'d.
# No-video means Text→Video or Omni without reference_video_urls.
SEEDANCE25_RETAIL_TOKENS_PER_SEC_NO_VIDEO = {
    "seedance25-480p": 2.0,
    "seedance25-720p": 4.5,
}
# With video, KIE billing is treated as input-video seconds + output seconds.
SEEDANCE25_RETAIL_TOKENS_PER_BILLABLE_SEC_WITH_VIDEO = {
    "seedance25-480p": 1.25,
    "seedance25-720p": 2.75,
}

# Provider rates are diagnostics only. Retail pricing above is the source of truth.
SEEDANCE25_PROVIDER_USD_PER_SEC = {
    "seedance25-480p": {"with_video": 0.085, "no_video": 0.140},
    "seedance25-720p": {"with_video": 0.190, "no_video": 0.315},
}
SEEDANCE25_USD_RUB = float(os.getenv("SEEDANCE25_USD_RUB", "85") or "85")
SEEDANCE25_TOKEN_RUB = float(os.getenv("SEEDANCE25_TOKEN_RUB", "8.5") or "8.5")
SEEDANCE25_VIDEO_REF_METADATA_TOLERANCE_SEC = float(os.getenv("SEEDANCE25_VIDEO_REF_METADATA_TOLERANCE_SEC", "0.25") or "0.25")

SEEDANCE25_RESOLUTIONS = {
    "seedance25-480p": "480p",
    "seedance25-720p": "720p",
}


class Seedance25KieError(RuntimeError):
    pass


class Seedance25TaskFailedError(Seedance25KieError):
    """KIE explicitly reported a terminal failed state; refund is safe."""


class Seedance25TaskPendingError(Seedance25KieError):
    """A KIE task already exists, but its final result is not safely known yet.

    Callers must NOT refund or create a replacement task. The reliable worker
    should leave the job unacked so it can resume polling the same taskId.
    """


def normalize_seedance25_model(value: Any, default: str = "seedance25-720p") -> str:
    raw = str(value or default).strip().lower().replace("_", "-")
    aliases = {
        "480": "seedance25-480p",
        "480p": "seedance25-480p",
        "seedance25-480": "seedance25-480p",
        "seedance25-480p": "seedance25-480p",
        "seedance-2.5-480p": "seedance25-480p",
        "seedance-2-5-480p": "seedance25-480p",
        "720": "seedance25-720p",
        "720p": "seedance25-720p",
        "seedance25": "seedance25-720p",
        "seedance25-720": "seedance25-720p",
        "seedance25-720p": "seedance25-720p",
        "seedance-2.5": "seedance25-720p",
        "seedance-2-5": "seedance25-720p",
        "seedance-2.5-720p": "seedance25-720p",
        "seedance-2-5-720p": "seedance25-720p",
    }
    return aliases.get(raw, default if default in SEEDANCE25_ALLOWED_MODELS else "seedance25-720p")


def normalize_seedance25_mode(value: Any, default: str = "text_to_video") -> str:
    raw = str(value or default).strip().lower().replace("-", "_")
    if raw in {"omni", "omni_reference", "reference", "refs", "multimodal", "reference_to_video"}:
        return "omni_reference"
    return "text_to_video"


def normalize_seedance25_duration(value: Any, default: int = 5) -> int:
    try:
        out = int(value)
    except Exception:
        out = int(default)
    return max(4, min(30, out))


def normalize_seedance25_aspect_ratio(value: Any, default: str = "adaptive") -> str:
    raw = str(value or default).strip()
    return raw if raw in SEEDANCE25_ALLOWED_ASPECT_RATIOS else default


def normalize_seedance25_resolution(value: Any, default: str = "720p") -> str:
    raw = str(value or default).strip().lower()
    return "480p" if raw in {"480", "480p", "seedance25-480p"} else "720p"


def seedance25_resolution(model: Any) -> str:
    return SEEDANCE25_RESOLUTIONS[normalize_seedance25_model(model)]


def seedance25_billable_input_video_seconds(value: Any) -> int:
    try:
        seconds = float(value or 0.0)
    except Exception:
        seconds = 0.0
    if seconds <= 0:
        return 0
    return max(1, int(math.ceil(seconds - SEEDANCE25_VIDEO_REF_METADATA_TOLERANCE_SEC)))


def seedance25_tokens_for_duration(model: Any, duration: Any, *, input_video_duration_sec: Any = 0) -> int:
    normalized_model = normalize_seedance25_model(model)
    output_seconds = normalize_seedance25_duration(duration)
    input_seconds = seedance25_billable_input_video_seconds(input_video_duration_sec)
    if input_seconds > 0:
        rate = SEEDANCE25_RETAIL_TOKENS_PER_BILLABLE_SEC_WITH_VIDEO[normalized_model]
        return max(1, int(math.ceil(float(input_seconds + output_seconds) * float(rate))))
    rate = SEEDANCE25_RETAIL_TOKENS_PER_SEC_NO_VIDEO[normalized_model]
    return max(1, int(math.ceil(float(output_seconds) * float(rate))))


def seedance25_pricing_breakdown(model: Any, duration: Any, *, input_video_duration_sec: Any = 0) -> Dict[str, Any]:
    normalized_model = normalize_seedance25_model(model)
    output_seconds = normalize_seedance25_duration(duration)
    input_seconds = seedance25_billable_input_video_seconds(input_video_duration_sec)
    has_video_input = input_seconds > 0
    billable_seconds = input_seconds + output_seconds if has_video_input else output_seconds
    retail_rate = (
        SEEDANCE25_RETAIL_TOKENS_PER_BILLABLE_SEC_WITH_VIDEO[normalized_model]
        if has_video_input
        else SEEDANCE25_RETAIL_TOKENS_PER_SEC_NO_VIDEO[normalized_model]
    )
    provider_rate = SEEDANCE25_PROVIDER_USD_PER_SEC[normalized_model]["with_video" if has_video_input else "no_video"]
    provider_cost_usd = float(provider_rate) * float(billable_seconds)
    tokens = seedance25_tokens_for_duration(normalized_model, output_seconds, input_video_duration_sec=input_seconds)
    retail_rub = float(tokens) * SEEDANCE25_TOKEN_RUB
    provider_rub = provider_cost_usd * SEEDANCE25_USD_RUB
    margin_pct = ((retail_rub - provider_rub) / retail_rub * 100.0) if retail_rub > 0 else 0.0
    return {
        "model": normalized_model,
        "duration": output_seconds,
        "input_video_seconds": input_seconds,
        "billable_seconds": billable_seconds,
        "has_video_input": has_video_input,
        "retail_rate_tokens": float(retail_rate),
        "tokens": int(tokens),
        "provider_rate_usd_per_sec": float(provider_rate),
        "provider_cost_usd": provider_cost_usd,
        "usd_rub": SEEDANCE25_USD_RUB,
        "token_rub": SEEDANCE25_TOKEN_RUB,
        "provider_cost_rub": provider_rub,
        "retail_rub": retail_rub,
        "margin_pct": margin_pct,
        "generate_audio": True,
    }


def _auth_headers() -> Dict[str, str]:
    if not KIE_API_TOKEN:
        raise Seedance25KieError("KIE API token is not configured. Set KIE_API_TOKEN or KIE_API_KEY.")
    return {"Authorization": f"Bearer {KIE_API_TOKEN}", "Content-Type": "application/json"}


def _clean_prompt(prompt: Any) -> str:
    text = str(prompt or "").strip()
    if not text:
        raise Seedance25KieError("Seedance 2.5 prompt is required")
    if len(text) > SEEDANCE25_PROMPT_MAX_CHARS:
        raise Seedance25KieError(f"Seedance 2.5 prompt is too long. Maximum: {SEEDANCE25_PROMPT_MAX_CHARS} characters")
    return text


def _looks_like_mp3(data: bytes) -> bool:
    head = bytes((data or b"")[:64])
    return head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0)


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
    if head[:2] in {b"BM"}:
        return "bmp"
    if head[:4] in {b"II*\x00", b"MM\x00*"}:
        return "tiff"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "mov" if head[8:12] == b"qt  " else "mp4"
    if _looks_like_mp3(head):
        return "mp3"
    return default


def _guess_mime(ext: str) -> str:
    return {
        "png": "image/png", "jpg": "image/jpeg",
        "mp4": "video/mp4", "mp3": "audio/mpeg", "wav": "audio/wav",
    }.get(str(ext or "").lower(), "application/octet-stream")


def _validate_ref_aliases(prompt: str, image_count: int, video_count: int, audio_count: int) -> None:
    for kind, count in (("image", image_count), ("video", video_count), ("audio", audio_count)):
        for match in re.finditer(rf"@{kind}(\d+)", str(prompt or ""), flags=re.IGNORECASE):
            idx = int(match.group(1) or 0)
            if idx <= 0 or idx > count:
                raise Seedance25KieError(f"Prompt references @{kind}{idx}, but only {count} {kind} reference(s) were uploaded")


def _upload_public_file(user_id: int, kind: str, idx: int, raw: bytes) -> str:
    data = bytes(raw or b"")
    if not data:
        raise Seedance25KieError("Empty upload payload")
    ext = _guess_ext(data)
    if kind == "image" and ext not in {"jpg", "png"}:
        raise Seedance25KieError("Seedance 2.5 image reference must be JPEG or PNG")
    if kind == "video" and ext != "mp4":
        raise Seedance25KieError("Seedance 2.5 video reference must be MP4")
    if kind == "audio" and ext not in {"mp3", "wav"}:
        raise Seedance25KieError("Seedance 2.5 audio reference must be MP3 or WAV")
    path = f"workspace_refs/{int(user_id)}/seedance25/{kind}/{int(time.time())}_{os.urandom(4).hex()}_{idx}.{ext}"
    try:
        return upload_bytes_to_supabase(path, data, _guess_mime(ext))
    except KlingFlowError as exc:
        raise Seedance25KieError(str(exc)) from exc
    except Exception as exc:
        raise Seedance25KieError(f"Failed to upload Seedance 2.5 {kind} reference: {exc}") from exc


async def _upload_files(user_id: int, files: Sequence[bytes] | None, kind: str, *, limit: int) -> List[str]:
    # Upload one reference at a time and avoid copying the full collection.
    # This keeps peak RAM bounded for large Omni video references.
    urls: List[str] = []
    for idx, raw in enumerate((files or []), start=1):
        if idx > int(limit):
            break
        if not raw:
            continue
        urls.append(await asyncio.to_thread(_upload_public_file, int(user_id), kind, idx, raw))
    return urls


def upload_seedance25_reference_bytes(user_id: int, kind: str, idx: int, raw: bytes) -> str:
    """Upload one validated Seedance 2.5 reference and return its public URL.

    Exposed for workers so they can download/upload files sequentially instead
    of keeping all Omni references in memory while KIE is polling.
    """
    normalized_kind = str(kind or "").strip().lower()
    if normalized_kind not in {"image", "video", "audio"}:
        raise Seedance25KieError(f"Unsupported Seedance 2.5 reference kind: {kind}")
    return _upload_public_file(int(user_id), normalized_kind, int(idx), raw)


def _clean_reference_urls(values: Sequence[str] | None, *, limit: int) -> List[str]:
    out: List[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if not text:
            continue
        out.append(text)
        if len(out) >= int(limit):
            break
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
        return raw if raw.startswith(("http://", "https://")) else None
    if isinstance(result, list):
        for item in result:
            found = _extract_video_url(item)
            if found:
                return found
    if isinstance(result, dict):
        for key in ("videoUrl", "video_url", "resultUrl", "result_url", "url", "downloadUrl", "download_url"):
            found = _extract_video_url(result.get(key))
            if found:
                return found
        for key in ("resultUrls", "result_urls", "videos", "urls", "output", "data"):
            found = _extract_video_url(result.get(key))
            if found:
                return found
        for value in result.values():
            found = _extract_video_url(value)
            if found:
                return found
    return None


async def _request_json(client: httpx.AsyncClient, method: str, path: str, *, payload: Optional[Dict[str, Any]] = None, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    resp = await client.request(method.upper(), f"{KIE_API_BASE}{path}", headers=_auth_headers(), json=payload, params=params)
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}
    if resp.status_code >= 400:
        detail = (data.get("msg") or data.get("message") or data.get("error") or resp.text[:800]) if isinstance(data, dict) else resp.text[:800]
        raise Seedance25KieError(f"KIE Seedance 2.5 request failed ({resp.status_code}): {detail}")
    if isinstance(data, dict):
        code = str(data.get("code") or "200")
        msg = str(data.get("msg") or data.get("message") or "").strip().lower()
        if code not in {"0", "200"} and msg != "success":
            raise Seedance25KieError(f"KIE Seedance 2.5 API error: {data.get('msg') or data.get('message') or data.get('error') or data}")
    return data if isinstance(data, dict) else {"data": data}


async def _notify_task_id(on_task_id: Optional[Callable[[str], Any]], task_id: str) -> None:
    if not on_task_id or not task_id:
        return

    # createTask has already succeeded once task_id reaches this function.
    # Never continue as if persistence were optional: a Render crash without a
    # durable taskId could cause the reliable job to call createTask a second
    # time.  Callers persist to Redis and/or Supabase; keep retrying until at
    # least one durable path succeeds. asyncio cancellation still propagates.
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
                print(
                    f"Seedance 2.5 KIE: taskId persistence pending task_id={task_id} "
                    f"attempt={attempt}: {exc}",
                    flush=True,
                )
            await asyncio.sleep(min(5.0, 1.0 + attempt * 0.25))


async def wait_seedance25_task(task_id: str) -> str:
    task_id = str(task_id or "").strip()
    if not task_id:
        raise Seedance25KieError("Seedance 2.5 taskId is empty")
    timeout = httpx.Timeout(max(60.0, SEEDANCE25_TIMEOUT_SECONDS + 120.0), connect=60.0)
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
                # Once taskId exists, a timeout/5xx/429/auth hiccup does not prove
                # that provider generation failed. Keep polling the SAME task. If
                # the overall polling window expires, surface a non-refundable
                # pending error so the reliable queue can reclaim and resume it.
                last_poll_error = str(exc)[:800]
                if time.monotonic() - started >= SEEDANCE25_TIMEOUT_SECONDS:
                    raise Seedance25TaskPendingError(
                        f"Seedance 2.5 task status is still unknown for taskId={task_id}. "
                        f"Last state: {last_state or 'unknown'}; last poll error: {last_poll_error or 'unknown'}"
                    ) from exc
                await asyncio.sleep(max(1.0, SEEDANCE25_POLL_SECONDS))
                continue

            data = done.get("data") if isinstance(done, dict) else None
            data = data if isinstance(data, dict) else {}
            state = str(data.get("state") or data.get("status") or "").strip().lower()
            if state:
                last_state = state
            if state == "success":
                result = data.get("resultJson") or data.get("result") or data.get("output")
                video_url = _extract_video_url(result)
                if video_url:
                    return video_url
                # A success state without a downloadable URL is already a paid
                # provider success, so it must never be auto-refunded. Give KIE
                # time to finish publishing resultJson and then resume later.
                last_poll_error = "provider state=success but result URL is not available yet"
            elif state == "fail":
                # KIE Market documents only the exact state `fail` as terminal
                # failure. Unknown future/provider states (for example `error`)
                # are deliberately treated as pending so we never refund a task
                # whose final provider outcome is not proven.
                detail = data.get("failMsg") or data.get("errorMessage") or data.get("msg") or data.get("message") or data
                raise Seedance25TaskFailedError(f"KIE Seedance 2.5 task failed: {detail}")

            if time.monotonic() - started >= SEEDANCE25_TIMEOUT_SECONDS:
                raise Seedance25TaskPendingError(
                    f"Seedance 2.5 task is not terminal yet for taskId={task_id}. "
                    f"Last state: {last_state or 'unknown'}; detail: {last_poll_error or 'polling timeout'}"
                )
            await asyncio.sleep(max(1.0, SEEDANCE25_POLL_SECONDS))


async def _run_task(*, input_payload: Dict[str, Any], on_task_id: Optional[Callable[[str], Any]] = None, resume_task_id: str = "") -> str:
    task_id = str(resume_task_id or "").strip()
    if not task_id:
        body: Dict[str, Any] = {"model": SEEDANCE25_MODEL_ID, "input": input_payload}
        if SEEDANCE25_CALLBACK_URL:
            body["callBackUrl"] = SEEDANCE25_CALLBACK_URL
        timeout = httpx.Timeout(max(60.0, SEEDANCE25_TIMEOUT_SECONDS + 120.0), connect=60.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            created = await _request_json(client, "POST", "/api/v1/jobs/createTask", payload=body)
        task_id = _extract_task_id(created)
        if not task_id:
            raise Seedance25KieError(f"KIE Seedance 2.5 createTask did not return taskId: {created}")
        await _notify_task_id(on_task_id, task_id)
    return await wait_seedance25_task(task_id)


def _base_payload(*, prompt: Any, model: Any, duration: Any, aspect_ratio: Any) -> Dict[str, Any]:
    return {
        "prompt": _clean_prompt(prompt),
        "return_last_frame": False,
        "generate_audio": True,
        "resolution": seedance25_resolution(model),
        "aspect_ratio": normalize_seedance25_aspect_ratio(aspect_ratio),
        "duration": normalize_seedance25_duration(duration),
        "output_format": "mp4",
        "web_search": False,
    }


async def run_seedance25_text_to_video(*, model: Any = None, resolution: Any = None, prompt: str, duration: Any, aspect_ratio: Any = "adaptive", on_task_id: Optional[Callable[[str], Any]] = None, resume_task_id: str = "") -> str:
    selected = model if model is not None else resolution
    normalized_model = normalize_seedance25_model(selected or "seedance25-720p")
    payload = _base_payload(prompt=prompt, model=normalized_model, duration=duration, aspect_ratio=aspect_ratio)
    return await _run_task(input_payload=payload, on_task_id=on_task_id, resume_task_id=resume_task_id)


async def run_seedance25_omni_reference(
    *,
    user_id: int,
    model: Any = None,
    resolution: Any = None,
    prompt: str,
    duration: Any,
    aspect_ratio: Any = "adaptive",
    reference_images: Sequence[bytes] | None = None,
    reference_videos: Sequence[bytes] | None = None,
    reference_audios: Sequence[bytes] | None = None,
    on_task_id: Optional[Callable[[str], Any]] = None,
    resume_task_id: str = "",
) -> str:
    selected = model if model is not None else resolution
    normalized_model = normalize_seedance25_model(selected or "seedance25-720p")
    image_refs = [bytes(x) for x in list(reference_images or []) if x]
    video_refs = [bytes(x) for x in list(reference_videos or []) if x]
    audio_refs = [bytes(x) for x in list(reference_audios or []) if x]
    total = len(image_refs) + len(video_refs) + len(audio_refs)
    if total <= 0:
        raise Seedance25KieError("Seedance 2.5 Omni Reference requires at least one reference")
    if len(image_refs) > SEEDANCE25_MAX_IMAGE_REFS:
        raise Seedance25KieError(f"Seedance 2.5 supports maximum {SEEDANCE25_MAX_IMAGE_REFS} image references")
    if len(video_refs) > SEEDANCE25_MAX_VIDEO_REFS:
        raise Seedance25KieError(f"Seedance 2.5 supports maximum {SEEDANCE25_MAX_VIDEO_REFS} video references")
    if len(audio_refs) > SEEDANCE25_MAX_AUDIO_REFS:
        raise Seedance25KieError(f"Seedance 2.5 supports maximum {SEEDANCE25_MAX_AUDIO_REFS} audio references")
    if total > SEEDANCE25_MAX_TOTAL_REFS:
        raise Seedance25KieError(f"Seedance 2.5 supports maximum {SEEDANCE25_MAX_TOTAL_REFS} total references")

    image_urls = await _upload_files(int(user_id), image_refs, "image", limit=SEEDANCE25_MAX_IMAGE_REFS)
    video_urls = await _upload_files(int(user_id), video_refs, "video", limit=SEEDANCE25_MAX_VIDEO_REFS)
    audio_urls = await _upload_files(int(user_id), audio_refs, "audio", limit=SEEDANCE25_MAX_AUDIO_REFS)
    _validate_ref_aliases(prompt, len(image_urls), len(video_urls), len(audio_urls))

    payload = _base_payload(prompt=prompt, model=normalized_model, duration=duration, aspect_ratio=aspect_ratio)
    if image_urls:
        payload["reference_image_urls"] = image_urls
    if video_urls:
        payload["reference_video_urls"] = video_urls
    if audio_urls:
        payload["reference_audio_urls"] = audio_urls
    return await _run_task(input_payload=payload, on_task_id=on_task_id, resume_task_id=resume_task_id)

async def run_seedance25_omni_reference_urls(
    *,
    user_id: int,
    model: Any = None,
    resolution: Any = None,
    prompt: str,
    duration: Any,
    aspect_ratio: Any = "adaptive",
    reference_image_urls: Sequence[str] | None = None,
    reference_video_urls: Sequence[str] | None = None,
    reference_audio_urls: Sequence[str] | None = None,
    on_task_id: Optional[Callable[[str], Any]] = None,
    resume_task_id: str = "",
) -> str:
    """Run Omni Reference from already-uploaded public/signed URLs.

    This path is used by workers after references have been persisted, so the
    potentially large media bytes can be released before the long KIE poll.
    """
    selected = model if model is not None else resolution
    normalized_model = normalize_seedance25_model(selected or "seedance25-720p")
    image_urls = _clean_reference_urls(reference_image_urls, limit=SEEDANCE25_MAX_IMAGE_REFS)
    video_urls = _clean_reference_urls(reference_video_urls, limit=SEEDANCE25_MAX_VIDEO_REFS)
    audio_urls = _clean_reference_urls(reference_audio_urls, limit=SEEDANCE25_MAX_AUDIO_REFS)
    total = len(image_urls) + len(video_urls) + len(audio_urls)
    if total <= 0:
        raise Seedance25KieError("Seedance 2.5 Omni Reference requires at least one reference")
    if total > SEEDANCE25_MAX_TOTAL_REFS:
        raise Seedance25KieError(f"Seedance 2.5 supports maximum {SEEDANCE25_MAX_TOTAL_REFS} total references")
    _validate_ref_aliases(prompt, len(image_urls), len(video_urls), len(audio_urls))

    payload = _base_payload(prompt=prompt, model=normalized_model, duration=duration, aspect_ratio=aspect_ratio)
    if image_urls:
        payload["reference_image_urls"] = image_urls
    if video_urls:
        payload["reference_video_urls"] = video_urls
    if audio_urls:
        payload["reference_audio_urls"] = audio_urls
    return await _run_task(input_payload=payload, on_task_id=on_task_id, resume_task_id=resume_task_id)


# Backward-compatible helpers used by the Telegram/workspace integration.
def seedance25_resolution_from_model(model: Any) -> str:
    return seedance25_resolution(model)


def seedance25_tokens_for_run(resolution_or_model: Any, duration: Any, *, input_video_duration_sec: Any = 0) -> int:
    raw = str(resolution_or_model or "").strip().lower()
    model = "seedance25-480p" if "480" in raw else "seedance25-720p"
    return seedance25_tokens_for_duration(model, duration, input_video_duration_sec=input_video_duration_sec)
