# replicate_http.py
import os
import json
import asyncio
from typing import Any, Dict, Optional, Union, List

import aiohttp


REPLICATE_API_TOKEN = (os.getenv("REPLICATE_API_TOKEN") or "").strip()

# Общие таймауты/пуллинг (можно переопределять на уровне вызывающего кода)
REPLICATE_HTTP_TIMEOUT_SECONDS = int(os.getenv("REPLICATE_HTTP_TIMEOUT", "60"))
REPLICATE_POLL_INTERVAL_SECONDS = float(os.getenv("REPLICATE_POLL_INTERVAL", "2.0"))
REPLICATE_MAX_WAIT_SECONDS = int(os.getenv("REPLICATE_MAX_WAIT", "900"))


class ReplicateHTTPError(RuntimeError):
    pass


def require_replicate_token() -> None:
    if not REPLICATE_API_TOKEN:
        raise ReplicateHTTPError("REPLICATE_API_TOKEN is missing (set it in Render Environment).")


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {REPLICATE_API_TOKEN}",
        "Content-Type": "application/json",
    }


def model_predictions_url(model_slug: str) -> str:
    """
    model_slug examples:
      - "google/veo-3-fast"
      - "google/veo-3.1"
      - "kwaivgi/kling-v1.6-standard"
    """
    return f"https://api.replicate.com/v1/models/{model_slug}/predictions"


async def post_prediction(
    session: aiohttp.ClientSession,
    model_slug: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    require_replicate_token()
    url = model_predictions_url(model_slug)

    async with session.post(url, headers=_headers(), json=payload) as r:
        text = await r.text()
        if r.status >= 400:
            raise ReplicateHTTPError(f"Replicate POST failed ({r.status}): {text}")
        try:
            return json.loads(text)
        except Exception as e:
            raise ReplicateHTTPError(f"Replicate POST: invalid JSON response: {e}; body={text[:500]}") from e


async def get_prediction(session: aiohttp.ClientSession, get_url: str) -> Dict[str, Any]:
    require_replicate_token()

    async with session.get(get_url, headers=_headers()) as r:
        text = await r.text()
        if r.status >= 400:
            raise ReplicateHTTPError(f"Replicate GET failed ({r.status}): {text}")
        try:
            return json.loads(text)
        except Exception as e:
            raise ReplicateHTTPError(f"Replicate GET: invalid JSON response: {e}; body={text[:500]}") from e


def extract_output_url(pred: Dict[str, Any]) -> Optional[str]:
    """
    Replicate output sometimes:
      - string (single URL)
      - list of strings (URLs)
      - None while running
    """
    out = pred.get("output")
    if out is None:
        return None
    if isinstance(out, str):
        return out
    if isinstance(out, list) and out:
        # take first string url
        for item in out:
            if isinstance(item, str):
                return item
    return None


async def wait_for_result_url(
    session: aiohttp.ClientSession,
    get_url: str,
    *,
    max_wait_seconds: int = REPLICATE_MAX_WAIT_SECONDS,
    poll_interval_seconds: float = REPLICATE_POLL_INTERVAL_SECONDS,
) -> str:
    """
    Poll prediction via pred["urls"]["get"] until succeeded/failed/timeout.
    Returns output URL (mp4/gif/etc) as string.
    """
    start = asyncio.get_event_loop().time()
    last_status = None

    while True:
        pred = await get_prediction(session, get_url)
        status = pred.get("status")

        if status != last_status:
            last_status = status

        if status == "succeeded":
            out_url = extract_output_url(pred)
            if not out_url:
                raise ReplicateHTTPError(f"Prediction succeeded but output missing/unexpected: {pred.get('output')}")
            return out_url

        if status in ("failed", "canceled"):
            raise ReplicateHTTPError(f"Prediction {status}: {pred.get('error') or pred}")

        elapsed = asyncio.get_event_loop().time() - start
        if elapsed > max_wait_seconds:
            raise ReplicateHTTPError(f"Timeout: waited {int(elapsed)}s > {max_wait_seconds}s. Last status={status}")

        await asyncio.sleep(poll_interval_seconds)


def get_prediction_get_url(pred: Dict[str, Any]) -> Optional[str]:
    urls = pred.get("urls") or {}
    get_url = urls.get("get")
    return get_url if isinstance(get_url, str) and get_url else None
