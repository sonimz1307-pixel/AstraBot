from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

KIE_API_BASE = (os.getenv("KIE_API_BASE") or "https://api.kie.ai").rstrip("/")
KIE_API_TOKEN = (os.getenv("KIE_API_TOKEN") or os.getenv("KIE_API_KEY") or "").strip()
KIE_GPT_IMAGE_2_T2I_MODEL = (os.getenv("KIE_GPT_IMAGE_2_T2I_MODEL") or "gpt-image-2-text-to-image").strip() or "gpt-image-2-text-to-image"
KIE_GPT_IMAGE_2_I2I_MODEL = (os.getenv("KIE_GPT_IMAGE_2_I2I_MODEL") or "gpt-image-2-image-to-image").strip() or "gpt-image-2-image-to-image"
KIE_GPT_IMAGE_2_CALLBACK_URL = (os.getenv("KIE_GPT_IMAGE_2_CALLBACK_URL") or "").strip()
KIE_GPT_IMAGE_2_CREATE_TIMEOUT_SECONDS = float(os.getenv("KIE_GPT_IMAGE_2_CREATE_TIMEOUT_SECONDS", "60") or "60")
KIE_GPT_IMAGE_2_MAX_WAIT_SECONDS = float(os.getenv("KIE_GPT_IMAGE_2_MAX_WAIT_SECONDS", "900") or "900")
KIE_GPT_IMAGE_2_MAX_REFS = max(1, min(16, int(os.getenv("KIE_GPT_IMAGE_2_MAX_REFS", "16") or "16")))
KIE_GPT_IMAGE_2_MAX_INPUT_MB = max(1, int(os.getenv("KIE_GPT_IMAGE_2_MAX_INPUT_MB", "30") or "30"))
KIE_GPT_IMAGE_2_MAX_INPUT_BYTES = KIE_GPT_IMAGE_2_MAX_INPUT_MB * 1024 * 1024

_ALLOWED_RESOLUTIONS = {"2K", "4K"}
# KIE docs list these aspect ratios. We intentionally do not expose/use `auto`,
# because KIE documents that auto/no aspect ratio is 1K-only, while this product
# exposes only 2K/4K.
_ALLOWED_ASPECT_RATIOS = {
    "1:1",
    "4:3",
    "3:4",
    "16:9",
    "9:16",
}
_ALLOWED_REFERENCE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
_ALLOWED_REFERENCE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
_POLLING_STATES = {"waiting", "queuing", "queued", "generating", "processing", "pending", "submitted", "created", "running"}
_FAIL_STATES = {"fail", "failed", "failure", "error"}


class GptImage2ProviderError(RuntimeError):
    pass


def _reference_ext_from_bytes(payload: bytes) -> str:
    head = bytes(payload[:16] if payload else b"")
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"RIFF") and len(head) >= 12 and head[8:12] == b"WEBP":
        return "webp"
    if len(payload or b"") >= 12 and payload[4:8] == b"ftyp":
        brand = payload[8:12].lower()
        if brand in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}:
            return "heic"
    return ""


def _reference_ext_from_name(filename: Any) -> str:
    raw = str(filename or "").strip().lower().rsplit(".", 1)
    if len(raw) != 2:
        return ""
    ext = raw[-1].strip().lstrip(".")
    return "jpg" if ext == "jpeg" else ext


def _reference_ext_from_mime(content_type: Any) -> str:
    raw = str(content_type or "").strip().lower().split(";", 1)[0]
    if raw == "image/jpeg":
        return "jpg"
    if raw == "image/png":
        return "png"
    if raw == "image/webp":
        return "webp"
    if raw in {"image/heic", "image/heif"}:
        return "heic"
    return ""


def validate_gpt_image_2_kie_reference_bytes(
    payload: bytes,
    *,
    filename: Any = None,
    content_type: Any = None,
    source_label: str = "reference image",
) -> Tuple[str, str]:
    """Validate a user-uploaded reference before exposing its public URL to KIE.

    KIE GPT Image 2 currently accepts JPG/JPEG, PNG and WEBP references.
    We reject HEIC/HEIF and oversize files before tokens are charged or a worker slot is occupied.
    """
    if not isinstance(payload, (bytes, bytearray)) or not payload:
        raise GptImage2ProviderError(f"{source_label}: пустой файл изображения")
    size = len(payload)
    if size > KIE_GPT_IMAGE_2_MAX_INPUT_BYTES:
        raise GptImage2ProviderError(f"{source_label}: файл больше {KIE_GPT_IMAGE_2_MAX_INPUT_MB} МБ")

    ext = _reference_ext_from_bytes(bytes(payload)) or _reference_ext_from_name(filename) or _reference_ext_from_mime(content_type)
    if ext == "jpeg":
        ext = "jpg"
    if ext not in _ALLOWED_REFERENCE_EXTENSIONS:
        raise GptImage2ProviderError(
            f"{source_label}: неподдерживаемый формат. Для Gpt Image 2 нужны JPG, PNG или WEBP; HEIC/HEIF не отправляем в KIE."
        )
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(ext, "image/jpeg")
    return ext, mime


def normalize_gpt_image_2_kie_resolution(value: Any, default: str = "2K") -> str:
    raw = str(value or default).strip().upper() or default
    return raw if raw in _ALLOWED_RESOLUTIONS else default


def normalize_gpt_image_2_kie_aspect_ratio(value: Any, default: str = "16:9") -> str:
    raw = str(value or default).strip() or default
    # `match_input_image` is a local UI alias used by other image providers.
    # KIE GPT Image 2 expects real aspect ratios for 2K/4K.
    if raw in {"auto", "match_input_image"}:
        raw = default
    return raw if raw in _ALLOWED_ASPECT_RATIOS else default


def normalize_gpt_image_2_kie_options(
    resolution: Any,
    aspect_ratio: Any,
    *,
    default_resolution: str = "2K",
    default_aspect: str = "16:9",
) -> Tuple[str, str]:
    normalized_resolution = normalize_gpt_image_2_kie_resolution(resolution, default=default_resolution)
    normalized_aspect = normalize_gpt_image_2_kie_aspect_ratio(aspect_ratio, default=default_aspect)
    # KIE docs: 1:1 cannot be converted to 4K. Keep the request creatable.
    if normalized_resolution == "4K" and normalized_aspect == "1:1":
        normalized_aspect = default_aspect
    return normalized_resolution, normalized_aspect


def gpt_image_2_kie_cost(resolution: Any) -> int:
    return 2 if normalize_gpt_image_2_kie_resolution(resolution) == "4K" else 1


def _auth_headers() -> Dict[str, str]:
    if not KIE_API_TOKEN:
        raise GptImage2ProviderError("Gpt Image 2 provider token is not configured.")
    return {
        "Authorization": f"Bearer {KIE_API_TOKEN}",
        "Content-Type": "application/json",
    }


async def _kie_request_json(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    url = f"{KIE_API_BASE}{path}"
    resp = await client.request(method.upper(), url, headers=_auth_headers(), params=params, json=payload)
    text = resp.text or ""
    try:
        data = resp.json() if text else {}
    except Exception:
        data = {"raw": text}
    if resp.status_code >= 400:
        detail = data.get("msg") or data.get("message") or data.get("error") or text or f"HTTP {resp.status_code}"
        raise GptImage2ProviderError(f"Gpt Image 2 request failed ({resp.status_code}): {detail}")
    if isinstance(data, dict):
        code = str(data.get("code") or "200")
        msg = str(data.get("msg") or data.get("message") or "").strip().lower()
        if code not in {"0", "200"} and msg != "success":
            detail = data.get("msg") or data.get("message") or data.get("error") or data
            raise GptImage2ProviderError(f"Gpt Image 2 API error: {detail}")
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


async def _resolve_source_urls(
    *,
    source_image_urls: Optional[Sequence[str]] = None,
    source_image_url: Optional[str] = None,
    telegram_file_ids: Optional[Sequence[str]] = None,
    telegram_file_id: Optional[str] = None,
) -> List[str]:
    resolved: List[str] = []
    seen = set()

    def _add(raw: Any) -> None:
        value = str(raw or "").strip()
        if not value or value in seen:
            return
        # Do not pass Telegram bot-file URLs to a provider: they contain the bot token.
        if "/file/bot" in value:
            return
        if not (value.startswith("http://") or value.startswith("https://")):
            return
        seen.add(value)
        resolved.append(value)

    for raw in list(source_image_urls or []):
        _add(raw)
    _add(source_image_url)

    if (telegram_file_id or telegram_file_ids) and not resolved:
        raise GptImage2ProviderError("Telegram images must be uploaded to public storage before calling Gpt Image 2 provider.")

    return resolved[:KIE_GPT_IMAGE_2_MAX_REFS]


def _extract_image_url(result: Any) -> Optional[str]:
    if isinstance(result, str):
        raw = result.strip()
        if not raw:
            return None
        if raw.startswith("{") or raw.startswith("["):
            try:
                return _extract_image_url(json.loads(raw))
            except Exception:
                return None
        if raw.startswith("http://") or raw.startswith("https://"):
            return raw
        return None
    if isinstance(result, list):
        for item in result:
            found = _extract_image_url(item)
            if found:
                return found
        return None
    if isinstance(result, dict):
        for key in ("imageUrl", "image_url", "url", "resultUrl", "result_url", "downloadUrl", "download_url"):
            found = _extract_image_url(result.get(key))
            if found:
                return found
        for key in ("resultUrls", "result_urls", "images", "outputs", "urls", "output", "data", "result"):
            found = _extract_image_url(result.get(key))
            if found:
                return found
        for value in result.values():
            found = _extract_image_url(value)
            if found:
                return found
    return None


async def _poll_task(client: httpx.AsyncClient, task_id: str) -> str:
    start_ts = time.monotonic()
    wait_schedule = [2, 3, 5, 8, 13, 21, 34, 55]
    attempt = 0
    last_state = ""
    last_detail = ""

    while True:
        payload = await _kie_request_json(client, "GET", "/api/v1/jobs/recordInfo", params={"taskId": task_id})
        data = payload.get("data") if isinstance(payload, dict) else None
        data = data if isinstance(data, dict) else {}
        state = str(data.get("state") or data.get("status") or "").strip().lower()
        if state:
            last_state = state
        if state == "success":
            result = data.get("resultJson")
            image_url = _extract_image_url(result) or _extract_image_url(data)
            if not image_url:
                raise GptImage2ProviderError(f"Gpt Image 2 task succeeded but no image url was returned: {data}")
            return image_url
        if state in _FAIL_STATES:
            fail_msg = str(data.get("failMsg") or data.get("message") or payload.get("msg") or "Gpt Image 2 task failed").strip()
            fail_code = str(data.get("failCode") or "").strip()
            detail = f"{fail_msg} ({fail_code})" if fail_code else fail_msg
            raise GptImage2ProviderError(detail or "Gpt Image 2 task failed")
        if state not in _POLLING_STATES:
            last_detail = str(payload.get("msg") or data or payload).strip()
        if (time.monotonic() - start_ts) >= KIE_GPT_IMAGE_2_MAX_WAIT_SECONDS:
            raise GptImage2ProviderError(f"Gpt Image 2 timeout. Last state: {last_state or 'unknown'} {last_detail}".strip())
        sleep_for = wait_schedule[min(attempt, len(wait_schedule) - 1)]
        attempt += 1
        await asyncio.sleep(sleep_for)


async def _download_bytes(url: str, *, timeout: float = 300.0) -> bytes:
    target = str(url or "").strip()
    if not target:
        raise GptImage2ProviderError("Empty image url")
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(target)
        resp.raise_for_status()
        return resp.content


def _detect_ext(payload: bytes, fallback: str = "jpg") -> str:
    head = bytes(payload[:16] if payload else b"")
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return fallback


async def handle_gpt_image_2_kie(
    prompt: str,
    *,
    mode: Any = "text_to_image",
    source_image_url: Optional[str] = None,
    source_image_urls: Optional[Sequence[str]] = None,
    telegram_file_id: Optional[str] = None,
    telegram_file_ids: Optional[Sequence[str]] = None,
    resolution: Any = "2K",
    aspect_ratio: Any = "16:9",
) -> Tuple[bytes, str]:
    clean_prompt = str(prompt or "").strip()
    if not clean_prompt:
        raise GptImage2ProviderError("Empty prompt")

    normalized_resolution, normalized_aspect = normalize_gpt_image_2_kie_options(resolution, aspect_ratio)
    mode_key = str(mode or "text_to_image").strip().lower()
    is_i2i = mode_key in {"image_to_image", "i2i", "edit"}

    source_urls = await _resolve_source_urls(
        source_image_urls=source_image_urls,
        source_image_url=source_image_url,
        telegram_file_ids=telegram_file_ids,
        telegram_file_id=telegram_file_id,
    )
    if is_i2i and not source_urls:
        raise GptImage2ProviderError("Gpt Image 2 Image→Image requires at least one reference image")

    input_payload: Dict[str, Any] = {
        "prompt": clean_prompt,
        "aspect_ratio": normalized_aspect,
        "resolution": normalized_resolution,
    }
    if is_i2i:
        input_payload["input_urls"] = source_urls[:KIE_GPT_IMAGE_2_MAX_REFS]

    payload: Dict[str, Any] = {
        "model": KIE_GPT_IMAGE_2_I2I_MODEL if is_i2i else KIE_GPT_IMAGE_2_T2I_MODEL,
        "input": input_payload,
    }
    if KIE_GPT_IMAGE_2_CALLBACK_URL:
        payload["callBackUrl"] = KIE_GPT_IMAGE_2_CALLBACK_URL

    timeout = httpx.Timeout(KIE_GPT_IMAGE_2_CREATE_TIMEOUT_SECONDS, connect=30.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        created = await _kie_request_json(client, "POST", "/api/v1/jobs/createTask", payload=payload)
        task_id = _extract_task_id(created)
        if not task_id:
            raise GptImage2ProviderError(f"Gpt Image 2 did not return taskId: {created}")
        result_url = await _poll_task(client, task_id)

    out_bytes = await _download_bytes(result_url)
    ext = _detect_ext(out_bytes, fallback="jpg")
    return out_bytes, ext
