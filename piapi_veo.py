# piapi_veo.py
import os
import json
import asyncio
from typing import Any, Dict, Optional

import aiohttp


PIAPI_API_KEY = (os.getenv("PIAPI_API_KEY") or os.getenv("PIAPI_KEY") or "").strip()
PIAPI_BASE_URL = (os.getenv("PIAPI_BASE_URL") or "https://api.piapi.ai").rstrip("/")

PIAPI_HTTP_TIMEOUT_SECONDS = int(os.getenv("PIAPI_HTTP_TIMEOUT", "60"))
PIAPI_POLL_INTERVAL_SECONDS = float(os.getenv("PIAPI_POLL_INTERVAL", "2.0"))
PIAPI_MAX_WAIT_SECONDS = int(os.getenv("PIAPI_MAX_WAIT", "900"))


class PiAPIError(RuntimeError):
    pass


def _headers() -> Dict[str, str]:
    if not PIAPI_API_KEY:
        raise PiAPIError("PIAPI_API_KEY is missing (set it in Render env vars).")
    return {"X-API-Key": PIAPI_API_KEY, "Content-Type": "application/json"}


async def piapi_create_task(
    session: aiohttp.ClientSession,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    url = f"{PIAPI_BASE_URL}/api/v1/task"
    async with session.post(url, headers=_headers(), json=payload, timeout=PIAPI_HTTP_TIMEOUT_SECONDS) as r:
        text = await r.text()
        if r.status >= 400:
            raise PiAPIError(f"PiAPI POST failed ({r.status}): {text[:2000]}")
        try:
            return json.loads(text)
        except Exception as e:
            raise PiAPIError(f"PiAPI POST: invalid JSON: {e}; body={text[:2000]}") from e


async def piapi_get_task(
    session: aiohttp.ClientSession,
    task_id: str,
) -> Dict[str, Any]:
    url = f"{PIAPI_BASE_URL}/api/v1/task/{task_id}"
    async with session.get(url, headers={"X-API-Key": PIAPI_API_KEY}, timeout=PIAPI_HTTP_TIMEOUT_SECONDS) as r:
        text = await r.text()
        if r.status >= 400:
            raise PiAPIError(f"PiAPI GET failed ({r.status}): {text[:2000]}")
        try:
            return json.loads(text)
        except Exception as e:
            raise PiAPIError(f"PiAPI GET: invalid JSON: {e}; body={text[:2000]}") from e


def _extract_task_id(resp: Dict[str, Any]) -> str:
    data = resp.get("data") or {}
    tid = data.get("task_id") or data.get("taskId") or resp.get("task_id")
    if not tid:
        raise PiAPIError(f"PiAPI response missing task_id. resp={str(resp)[:1500]}")
    return str(tid)


def _status_lower(resp: Dict[str, Any]) -> str:
    data = resp.get("data") or {}
    return str(data.get("status") or "").strip().lower()


def _extract_output_url(resp: Dict[str, Any]) -> Optional[str]:
    data = resp.get("data") or {}
    out = data.get("output") or {}
    # PiAPI sometimes returns image_url (single) or image_urls (list)
    u = out.get("image_url")
    if isinstance(u, str) and u.strip():
        return u.strip()
    arr = out.get("image_urls")
    if isinstance(arr, list):
        for item in arr:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


def _extract_error_message(resp: Dict[str, Any]) -> str:
    data = resp.get("data") or {}
    err = data.get("error") or {}
    msg = err.get("message") or resp.get("message") or ""
    return str(msg)


async def piapi_run_and_wait_for_url(
    session: aiohttp.ClientSession,
    payload: Dict[str, Any],
    *,
    max_wait_seconds: int = PIAPI_MAX_WAIT_SECONDS,
    poll_interval_seconds: float = PIAPI_POLL_INTERVAL_SECONDS,
) -> str:
    created = await piapi_create_task(session, payload)
    task_id = _extract_task_id(created)

    start = asyncio.get_event_loop().time()
    last = None

    while True:
        last = await piapi_get_task(session, task_id)
        st = _status_lower(last)

        if st == "completed":
            url = _extract_output_url(last)
            if not url:
                raise PiAPIError(f"PiAPI completed but no output url. resp={str(last)[:1500]}")
            return url

        if st == "failed":
            raise PiAPIError(f"PiAPI task failed: {_extract_error_message(last)}")

        if asyncio.get_event_loop().time() - start > max_wait_seconds:
            raise TimeoutError(f"PiAPI task timeout after {max_wait_seconds}s (task_id={task_id}, status={st})")

        await asyncio.sleep(poll_interval_seconds)
