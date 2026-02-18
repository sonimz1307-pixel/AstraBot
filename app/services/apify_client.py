from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional, Tuple

import requests


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name, str(default))
    try:
        return int(raw)
    except Exception:
        return default


def _clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


class ApifyError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        response_text: Optional[str] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


def _token() -> str:
    tok = _env("APIFY_TOKEN") or _env("APIFY_API_TOKEN")
    if not tok:
        raise ApifyError("Missing APIFY_TOKEN env var")
    return tok


def _act_path(actor_id: str) -> str:
    """
    Actor ID formats:
      - "user~actor-name" (preferred)
      - "user/actor-name"
    We keep it as-is; Apify accepts both in URL path.
    """
    return actor_id.strip()


def _default_timeout_for_actor(actor_id: str) -> int:
    """
    Default timeouts:
      - APIFY_RUNSYNC_TIMEOUT_SECS: global default (fallback 180)
      - APIFY_RUNSYNC_TIMEOUT_YANDEX_SECS: override for yandex actors (fallback 600)

    Simple heuristic: if 'yandex' in actor_id -> Yandex actor.
    """
    global_default = _env_int("APIFY_RUNSYNC_TIMEOUT_SECS", 180)
    yandex_default = _env_int("APIFY_RUNSYNC_TIMEOUT_YANDEX_SECS", 600)
    if "yandex" in (actor_id or "").lower():
        return yandex_default
    return global_default


def _default_retries_for_actor(actor_id: str) -> int:
    """
    Retries (transport-level):
      - APIFY_HTTP_RETRIES: global retries (fallback 2)
      - APIFY_HTTP_RETRIES_YANDEX: override for yandex actors (fallback 3)
    """
    global_r = _env_int("APIFY_HTTP_RETRIES", 2)
    yandex_r = _env_int("APIFY_HTTP_RETRIES_YANDEX", 3)
    if "yandex" in (actor_id or "").lower():
        return yandex_r
    return global_r


def _sleep_backoff(attempt: int) -> None:
    """
    Exponential backoff with capped sleep:
      base: APIFY_HTTP_RETRY_BASE_SLEEP_SECS (default 2)
      max : APIFY_HTTP_RETRY_MAX_SLEEP_SECS  (default 30)
    """
    base = _env_int("APIFY_HTTP_RETRY_BASE_SLEEP_SECS", 2)
    max_sleep = _env_int("APIFY_HTTP_RETRY_MAX_SLEEP_SECS", 30)
    sleep = base * (2 ** max(0, attempt))
    sleep = _clamp(int(sleep), 0, int(max_sleep))
    # tiny deterministic "jitter"
    sleep = min(int(max_sleep), sleep + (attempt % 3))
    time.sleep(float(sleep))


def run_actor_sync_get_dataset_items(
    *,
    actor_id: str,
    actor_input: Dict[str, Any],
    timeout_secs: Optional[int] = None,
    items_format: str = "json",
    clean: bool = True,
    connect_timeout_secs: int = 10,
    retries: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Runs Apify actor synchronously and returns dataset items.
    Uses: /run-sync-get-dataset-items

    Timeouts:
      - requests timeout is (connect_timeout_secs, read_timeout_secs)
      - read_timeout_secs chosen from env defaults if timeout_secs is None

    Retries:
      - Retries transient HTTP errors and transport errors:
        HTTP 429/502/503/504 and requests.RequestException
      - retries defaults from env (see _default_retries_for_actor)
    """
    tok = _token()
    act = _act_path(actor_id)
    url = f"https://api.apify.com/v2/acts/{act}/run-sync-get-dataset-items"
    params = {"token": tok, "format": items_format}
    if clean:
        params["clean"] = "true"

    read_timeout = int(timeout_secs) if timeout_secs is not None else _default_timeout_for_actor(actor_id)
    timeout: Tuple[int, int] = (int(connect_timeout_secs), int(read_timeout))

    max_retries = int(retries) if retries is not None else _default_retries_for_actor(actor_id)
    max_retries = _clamp(max_retries, 0, 10)

    last_exc: Optional[Exception] = None

    for attempt in range(0, max_retries + 1):
        try:
            resp = requests.post(url, params=params, json=actor_input, timeout=timeout)
        except requests.RequestException as e:
            last_exc = e
            if attempt >= max_retries:
                raise ApifyError(f"Apify request failed: {e}") from e
            _sleep_backoff(attempt)
            continue

        # Retry transient gateway / rate-limit
        if resp.status_code in (429, 502, 503, 504):
            if attempt >= max_retries:
                raise ApifyError(
                    f"Apify HTTP {resp.status_code}",
                    status_code=resp.status_code,
                    response_text=resp.text[:2000],
                )

            if resp.status_code == 429:
                ra = resp.headers.get("Retry-After")
                if ra:
                    try:
                        sleep = float(ra)
                        sleep = _clamp(int(sleep), 1, _env_int("APIFY_HTTP_RETRY_MAX_SLEEP_SECS", 30))
                        time.sleep(float(sleep))
                    except Exception:
                        _sleep_backoff(attempt)
                else:
                    _sleep_backoff(attempt)
            else:
                _sleep_backoff(attempt)
            continue

        if resp.status_code >= 400:
            raise ApifyError(
                f"Apify HTTP {resp.status_code}",
                status_code=resp.status_code,
                response_text=resp.text[:2000],
            )

        try:
            data = resp.json()
        except ValueError as e:
            raise ApifyError(
                "Apify returned non-JSON response",
                status_code=resp.status_code,
                response_text=resp.text[:2000],
            ) from e

        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]

        if isinstance(data, dict) and data.get("error"):
            raise ApifyError(
                f"Apify error: {data.get('error')}",
                status_code=resp.status_code,
                response_text=str(data)[:2000],
            )

        return []

    if last_exc:
        raise ApifyError(f"Apify request failed: {last_exc}") from last_exc
    return []
