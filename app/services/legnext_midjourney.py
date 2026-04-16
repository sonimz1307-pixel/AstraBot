from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Optional

import httpx

LEGNEXT_BASE_URL = (os.getenv("LEGNEXT_BASE_URL", "https://api.legnext.ai/api/v1") or "https://api.legnext.ai/api/v1").strip().rstrip("/")
LEGNEXT_API_KEY = (os.getenv("LEGNEXT_API_KEY") or "").strip()
LEGNEXT_HTTP_TIMEOUT_SEC = max(30.0, float(os.getenv("LEGNEXT_HTTP_TIMEOUT_SEC", "180") or "180"))


class LegnextMidjourneyError(RuntimeError):
    pass


def normalize_midjourney_speed_mode(value: Any, default: str = "fast") -> str:
    raw = str(value or "").strip().lower()
    if raw == "turbo":
        return "turbo"
    return default if default in {"fast", "turbo"} else "fast"


def _require_api_key() -> str:
    if not LEGNEXT_API_KEY:
        raise LegnextMidjourneyError("LEGNEXT_API_KEY is not configured")
    return LEGNEXT_API_KEY


async def _request(method: str, path: str, *, json_payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    api_key = _require_api_key()
    url = f"{LEGNEXT_BASE_URL}/{path.lstrip('/')}"
    headers = {
        "x-api-key": api_key,
        "Content-Type": "application/json",
    }
    timeout = httpx.Timeout(connect=20.0, read=LEGNEXT_HTTP_TIMEOUT_SEC, write=60.0, pool=60.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.request(method.upper(), url, headers=headers, json=json_payload)
    text = resp.text or ""
    if resp.status_code >= 400:
        detail = text[:1200]
        try:
            payload = resp.json()
            if isinstance(payload, dict):
                detail = str(payload.get("message") or payload.get("detail") or payload.get("raw_message") or detail)
        except Exception:
            pass
        raise LegnextMidjourneyError(f"Legnext API HTTP {resp.status_code}: {detail}")
    try:
        data = resp.json()
    except Exception as exc:
        raise LegnextMidjourneyError(f"Legnext API returned invalid JSON: {text[:600]}") from exc
    if not isinstance(data, dict):
        raise LegnextMidjourneyError("Legnext API returned unexpected payload")
    return data


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _normalize_http_urls(items: Iterable[Any]) -> List[str]:
    result: List[str] = []
    for item in items:
        value = str(item or "").strip()
        if not value:
            continue
        if not (value.startswith("http://") or value.startswith("https://")):
            continue
        result.append(value)
    return result


def build_midjourney_v7_prompt(
    *,
    prompt: str,
    aspect_ratio: str = "1:1",
    stylize: Any = 100,
    chaos: Any = 0,
    raw_mode: Any = False,
    negative_prompt: str = "",
    seed: Any = None,
    speed_mode: str = "fast",
    style_ref_urls: Optional[Iterable[Any]] = None,
    omni_ref_url: Optional[str] = None,
) -> str:
    base_prompt = str(prompt or "").strip()
    if not base_prompt:
        raise LegnextMidjourneyError("Midjourney prompt is empty")

    parts: List[str] = [base_prompt]
    safe_ar = str(aspect_ratio or "1:1").strip() or "1:1"
    parts.append(f"--ar {safe_ar}")
    parts.append("--v 7")

    try:
        stylize_value = max(0, min(1000, int(stylize if stylize is not None else 100)))
    except Exception:
        stylize_value = 100
    parts.append(f"--stylize {stylize_value}")

    try:
        chaos_value = max(0, min(100, int(chaos if chaos is not None else 0)))
    except Exception:
        chaos_value = 0
    if chaos_value > 0:
        parts.append(f"--chaos {chaos_value}")

    negative = str(negative_prompt or "").strip()
    if negative:
        parts.append(f"--no {negative}")

    if _boolish(raw_mode):
        parts.append("--raw")

    normalized_seed = str(seed or "").strip()
    if normalized_seed:
        try:
            seed_value = int(normalized_seed)
            if 0 <= seed_value <= 4294967295:
                parts.append(f"--seed {seed_value}")
        except Exception:
            pass

    speed = normalize_midjourney_speed_mode(speed_mode)
    parts.append("--turbo" if speed == "turbo" else "--fast")

    style_refs = _normalize_http_urls(style_ref_urls or [])
    if style_refs:
        parts.append("--sref " + " ".join(style_refs[:4]))

    omni = str(omni_ref_url or "").strip()
    if omni.startswith("http://") or omni.startswith("https://"):
        parts.append(f"--oref {omni}")

    return " ".join(part for part in parts if str(part or "").strip())


async def create_midjourney_diffusion(*, text: str, callback: Optional[str] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"text": str(text or "").strip()}
    if callback:
        payload["callback"] = str(callback).strip()
    return await _request("POST", "/diffusion", json_payload=payload)


async def create_midjourney_reroll(*, job_id: str, callback: Optional[str] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"jobId": str(job_id or "").strip()}
    if callback:
        payload["callback"] = str(callback).strip()
    return await _request("POST", "/reroll", json_payload=payload)


async def create_midjourney_variation(*, job_id: str, image_no: int, variation_type: int, remix_prompt: Optional[str] = None, callback: Optional[str] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "jobId": str(job_id or "").strip(),
        "imageNo": int(image_no),
        "type": int(variation_type),
    }
    if remix_prompt:
        payload["remixPrompt"] = str(remix_prompt).strip()
    if callback:
        payload["callback"] = str(callback).strip()
    return await _request("POST", "/variation", json_payload=payload)


async def get_midjourney_job(job_id: str) -> Dict[str, Any]:
    safe_job_id = str(job_id or "").strip()
    if not safe_job_id:
        raise LegnextMidjourneyError("Midjourney job_id is empty")
    return await _request("GET", f"/job/{safe_job_id}")
