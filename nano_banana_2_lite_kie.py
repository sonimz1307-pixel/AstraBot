from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

KIE_API_BASE = (os.getenv("KIE_API_BASE") or "https://api.kie.ai").rstrip("/")
KIE_API_TOKEN = (os.getenv("KIE_API_TOKEN") or os.getenv("KIE_API_KEY") or "").strip()
KIE_NB2_LITE_MODEL = (os.getenv("KIE_NANO_BANANA_2_LITE_MODEL") or "nano-banana-2-lite").strip() or "nano-banana-2-lite"
KIE_NB2_LITE_CALLBACK_URL = (os.getenv("KIE_NANO_BANANA_2_LITE_CALLBACK_URL") or "").strip()
KIE_NB2_LITE_CREATE_TIMEOUT_SECONDS = float(os.getenv("KIE_NANO_BANANA_2_LITE_CREATE_TIMEOUT_SECONDS", "60") or "60")
KIE_NB2_LITE_MAX_WAIT_SECONDS = float(os.getenv("KIE_NANO_BANANA_2_LITE_MAX_WAIT_SECONDS", "600") or "600")


_ALLOWED_ASPECT_RATIOS = {
    "1:1", "1:4", "1:8", "2:3", "3:2", "3:4", "4:1", "4:3", "4:5", "5:4",
    "8:1", "9:16", "16:9", "21:9", "auto",
}
_POLLING_STATES = {"waiting", "queuing", "generating", "processing", "pending", "submitted"}


class NanoBanana2LiteError(RuntimeError):
    pass


def normalize_nano_banana_2_lite_aspect_ratio(value: Any, default: str = "auto") -> str:
    raw = str(value or default).strip().lower() or default
    if raw == "match_input_image":
        return "auto"
    return raw if raw in _ALLOWED_ASPECT_RATIOS else default


def nano_banana_2_lite_cost() -> int:
    return 1


def _auth_headers() -> Dict[str, str]:
    if not KIE_API_TOKEN:
        raise NanoBanana2LiteError("KIE API token is not configured. Set KIE_API_TOKEN or KIE_API_KEY.")
    return {
        "Authorization": f"Bearer {KIE_API_TOKEN}",
        "Content-Type": "application/json",
    }


async def _kie_request_json(client: httpx.AsyncClient, method: str, path: str, *, params: Optional[Dict[str, Any]] = None, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"{KIE_API_BASE}{path}"
    resp = await client.request(method.upper(), url, headers=_auth_headers(), params=params, json=payload)
    text = resp.text or ""
    try:
        data = resp.json() if text else {}
    except Exception:
        data = {"raw": text}
    if resp.status_code >= 400:
        detail = data.get("msg") or data.get("message") or data.get("error") or text or f"HTTP {resp.status_code}"
        raise NanoBanana2LiteError(f"KIE request failed ({resp.status_code}): {detail}")
    if isinstance(data, dict):
        # KIE common task-detail responses may return non-200 numeric codes with msg=success.
        # Treat only non-success messages as provider errors.
        code = str(data.get("code") or "200")
        msg = str(data.get("msg") or data.get("message") or "").strip().lower()
        if code not in {"0", "200"} and msg != "success":
            detail = data.get("msg") or data.get("message") or data.get("error") or data
            raise NanoBanana2LiteError(f"KIE API error: {detail}")
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


async def _resolve_source_urls(*, source_image_urls: Optional[Sequence[str]] = None, source_image_url: Optional[str] = None, telegram_file_ids: Optional[Sequence[str]] = None, telegram_file_id: Optional[str] = None, require_source_image: bool = False) -> List[str]:
    resolved: List[str] = []
    seen = set()

    def _add(raw: Any) -> None:
        value = str(raw or "").strip()
        if not value or value in seen:
            return
        # Never send Telegram bot-file URLs to an external provider: they contain the bot token.
        if "/file/bot" in value:
            return
        if not (value.startswith("http://") or value.startswith("https://")):
            return
        seen.add(value)
        resolved.append(value)

    for raw in list(source_image_urls or []):
        _add(raw)
    _add(source_image_url)

    has_telegram_ids = bool(telegram_file_id) or any(str(raw or "").strip() for raw in list(telegram_file_ids or []))
    if has_telegram_ids and not resolved:
        raise NanoBanana2LiteError(
            "Telegram reference images must be uploaded to public storage before calling Nano Banana 2 Lite provider."
        )

    if require_source_image and not resolved:
        raise NanoBanana2LiteError("Nano Banana 2 Lite Image→Image requires at least one reference image URL.")

    return resolved[:10]


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
        for key in (
            "imageUrl", "image_url", "url", "resultUrl", "result_url", "downloadUrl", "download_url",
        ):
            found = _extract_image_url(result.get(key))
            if found:
                return found
        for key in ("resultUrls", "result_urls", "images", "outputs", "urls", "output"):
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
                raise NanoBanana2LiteError(f"KIE task succeeded but no image url was returned: {data}")
            return image_url
        if state == "fail":
            fail_msg = str(data.get("failMsg") or data.get("message") or payload.get("msg") or "KIE task failed").strip()
            fail_code = str(data.get("failCode") or "").strip()
            detail = f"{fail_msg} ({fail_code})" if fail_code else fail_msg
            raise NanoBanana2LiteError(detail or "KIE task failed")
        if state not in _POLLING_STATES:
            last_detail = str(payload.get("msg") or data or payload).strip()
        if (time.monotonic() - start_ts) >= KIE_NB2_LITE_MAX_WAIT_SECONDS:
            raise NanoBanana2LiteError(f"KIE Nano Banana 2 Lite timeout. Last state: {last_state or 'unknown'} {last_detail}".strip())
        sleep_for = wait_schedule[min(attempt, len(wait_schedule) - 1)]
        attempt += 1
        await asyncio.sleep(sleep_for)


async def _download_bytes(url: str, *, timeout: float = 300.0) -> bytes:
    target = str(url or "").strip()
    if not target:
        raise NanoBanana2LiteError("Empty image url")
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


async def handle_nano_banana_2_lite(
    prompt: str,
    *,
    source_image_url: Optional[str] = None,
    source_image_urls: Optional[Sequence[str]] = None,
    telegram_file_id: Optional[str] = None,
    telegram_file_ids: Optional[Sequence[str]] = None,
    aspect_ratio: Any = "auto",
    require_source_image: bool = False,
) -> Tuple[bytes, str]:
    clean_prompt = str(prompt or "").strip()
    if not clean_prompt:
        raise NanoBanana2LiteError("Empty prompt")

    normalized_aspect = normalize_nano_banana_2_lite_aspect_ratio(aspect_ratio, default="auto")
    source_urls = await _resolve_source_urls(
        source_image_urls=source_image_urls,
        source_image_url=source_image_url,
        telegram_file_ids=telegram_file_ids,
        telegram_file_id=telegram_file_id,
        require_source_image=bool(require_source_image),
    )

    input_payload: Dict[str, Any] = {
        "prompt": clean_prompt,
        "aspect_ratio": normalized_aspect,
    }
    if source_urls:
        input_payload["image_urls"] = source_urls[:10]

    payload: Dict[str, Any] = {
        "model": KIE_NB2_LITE_MODEL,
        "input": input_payload,
    }
    if KIE_NB2_LITE_CALLBACK_URL:
        payload["callBackUrl"] = KIE_NB2_LITE_CALLBACK_URL

    timeout = httpx.Timeout(KIE_NB2_LITE_CREATE_TIMEOUT_SECONDS, connect=30.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        created = await _kie_request_json(client, "POST", "/api/v1/jobs/createTask", payload=payload)
        task_id = _extract_task_id(created)
        if not task_id:
            raise NanoBanana2LiteError(f"KIE did not return taskId: {created}")
        result_url = await _poll_task(client, task_id)

    out_bytes = await _download_bytes(result_url)
    ext = _detect_ext(out_bytes, fallback="jpg")
    return out_bytes, ext
