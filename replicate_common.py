from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx


REPLICATE_API_BASE = os.getenv("REPLICATE_API_BASE", "https://api.replicate.com/v1").rstrip("/")
REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN", "").strip()

DEFAULT_CREATE_TIMEOUT_SEC = float(os.getenv("REPLICATE_CREATE_TIMEOUT_SEC", "60"))
DEFAULT_POLL_TIMEOUT_SEC = int(os.getenv("REPLICATE_POLL_TIMEOUT_SEC", "1800"))
DEFAULT_POLL_SEC = float(os.getenv("REPLICATE_POLL_SEC", "5"))

TERMINAL_STATUSES = {"succeeded", "failed", "canceled", "aborted"}


class ReplicateError(RuntimeError):
    """Base Replicate error."""


class ReplicateTimeoutError(ReplicateError):
    """Replicate polling timeout."""


@dataclass(slots=True)
class ReplicatePrediction:
    id: str
    status: str
    raw: dict[str, Any]
    output: Any = None
    error: str = ""
    logs: str = ""


def _headers() -> dict[str, str]:
    if not REPLICATE_API_TOKEN:
        raise ReplicateError("REPLICATE_API_TOKEN is not set")
    return {
        "Authorization": f"Bearer {REPLICATE_API_TOKEN}",
        "Content-Type": "application/json",
        "Prefer": "wait=0",
    }


def _prediction_from_json(data: dict[str, Any]) -> ReplicatePrediction:
    return ReplicatePrediction(
        id=str(data.get("id") or "").strip(),
        status=str(data.get("status") or "").strip().lower(),
        raw=data,
        output=data.get("output"),
        error=str(data.get("error") or "").strip(),
        logs=str(data.get("logs") or ""),
    )


def first_http_url(value: Any) -> Optional[str]:
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("http://") or s.startswith("https://"):
            return s
        return None

    if isinstance(value, list):
        for item in value:
            got = first_http_url(item)
            if got:
                return got
        return None

    if isinstance(value, dict):
        for key in ("url", "file_url", "fileUrl", "output", "video", "image"):
            got = first_http_url(value.get(key))
            if got:
                return got
        for item in value.values():
            got = first_http_url(item)
            if got:
                return got
    return None


async def create_prediction(
    *,
    version: str,
    input_data: dict[str, Any],
    webhook: Optional[str] = None,
    webhook_events_filter: Optional[list[str]] = None,
    timeout_sec: float = DEFAULT_CREATE_TIMEOUT_SEC,
) -> ReplicatePrediction:
    body: dict[str, Any] = {
        "version": str(version or "").strip(),
        "input": dict(input_data or {}),
    }
    if webhook:
        body["webhook"] = webhook
    if webhook_events_filter:
        body["webhook_events_filter"] = list(webhook_events_filter)

    async with httpx.AsyncClient(timeout=timeout_sec) as client:
        r = await client.post(
            f"{REPLICATE_API_BASE}/predictions",
            headers=_headers(),
            json=body,
        )

    if r.status_code >= 300:
        raise ReplicateError(f"Replicate create_prediction failed: {r.status_code} {r.text[:1200]}")

    data = r.json()
    pred = _prediction_from_json(data)
    if not pred.id:
        raise ReplicateError(f"Replicate did not return prediction id: {data}")
    return pred


async def get_prediction(prediction_id: str, *, timeout_sec: float = 60.0) -> ReplicatePrediction:
    pid = str(prediction_id or "").strip()
    if not pid:
        raise ReplicateError("prediction_id is empty")

    async with httpx.AsyncClient(timeout=timeout_sec) as client:
        r = await client.get(
            f"{REPLICATE_API_BASE}/predictions/{pid}",
            headers=_headers(),
        )

    if r.status_code >= 300:
        raise ReplicateError(f"Replicate get_prediction failed: {r.status_code} {r.text[:1200]}")

    data = r.json()
    pred = _prediction_from_json(data)
    if not pred.id:
        raise ReplicateError(f"Replicate returned invalid prediction payload: {data}")
    return pred


async def poll_prediction(
    prediction_id: str,
    *,
    timeout_sec: int = DEFAULT_POLL_TIMEOUT_SEC,
    sleep_sec: float = DEFAULT_POLL_SEC,
) -> ReplicatePrediction:
    started = time.time()
    last: Optional[ReplicatePrediction] = None

    while True:
        last = await get_prediction(prediction_id)
        if last.status in TERMINAL_STATUSES:
            return last

        if time.time() - started > timeout_sec:
            raise ReplicateTimeoutError(
                f"Replicate timeout after {timeout_sec}s (prediction_id={prediction_id}, status={last.status})"
            )

        await asyncio.sleep(max(1.0, float(sleep_sec)))


async def create_and_poll_prediction(
    *,
    version: str,
    input_data: dict[str, Any],
    webhook: Optional[str] = None,
    webhook_events_filter: Optional[list[str]] = None,
    create_timeout_sec: float = DEFAULT_CREATE_TIMEOUT_SEC,
    poll_timeout_sec: int = DEFAULT_POLL_TIMEOUT_SEC,
    poll_sleep_sec: float = DEFAULT_POLL_SEC,
) -> ReplicatePrediction:
    created = await create_prediction(
        version=version,
        input_data=input_data,
        webhook=webhook,
        webhook_events_filter=webhook_events_filter,
        timeout_sec=create_timeout_sec,
    )
    if created.status in TERMINAL_STATUSES:
        return created
    return await poll_prediction(
        created.id,
        timeout_sec=poll_timeout_sec,
        sleep_sec=poll_sleep_sec,
    )


async def download_bytes(url: str, *, timeout_sec: float = 300.0) -> bytes:
    target = str(url or "").strip()
    if not target:
        raise ReplicateError("download url is empty")

    async with httpx.AsyncClient(timeout=timeout_sec, follow_redirects=True) as client:
        r = await client.get(target)

    if r.status_code >= 300:
        raise ReplicateError(f"Replicate download failed: {r.status_code} {r.text[:1200]}")
    return r.content


def extract_output_url(prediction_payload: dict[str, Any] | ReplicatePrediction) -> Optional[str]:
    if isinstance(prediction_payload, ReplicatePrediction):
        return first_http_url(prediction_payload.output)

    if isinstance(prediction_payload, dict):
        return first_http_url(prediction_payload.get("output"))
    return None
