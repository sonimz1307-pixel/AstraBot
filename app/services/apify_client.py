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


def _env_float(name: str, default: float) -> float:
    raw = _env(name, str(default))
    try:
        return float(raw)
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


def _is_yandex(actor_id: str) -> bool:
    return "yandex" in (actor_id or "").lower()


def _default_timeout_for_actor(actor_id: str) -> int:
    """
    Read timeout (seconds) for long-running operations.

      - APIFY_RUNSYNC_TIMEOUT_SECS: global default (fallback 180)
      - APIFY_RUNSYNC_TIMEOUT_YANDEX_SECS: yandex override (fallback 600)
    """
    global_default = _env_int("APIFY_RUNSYNC_TIMEOUT_SECS", 180)
    yandex_default = _env_int("APIFY_RUNSYNC_TIMEOUT_YANDEX_SECS", 600)
    return yandex_default if _is_yandex(actor_id) else global_default


def _default_retries_for_actor(actor_id: str) -> int:
    """
    Transport-level retries:
      - APIFY_HTTP_RETRIES: global (fallback 2)
      - APIFY_HTTP_RETRIES_YANDEX: yandex override (fallback 3)
    """
    global_r = _env_int("APIFY_HTTP_RETRIES", 2)
    yandex_r = _env_int("APIFY_HTTP_RETRIES_YANDEX", 3)
    return yandex_r if _is_yandex(actor_id) else global_r


def _default_poll_interval_for_actor(actor_id: str) -> float:
    """
    Polling interval (seconds) for fire-and-poll:
      - APIFY_POLL_INTERVAL_SECS: global (fallback 3.0)
      - APIFY_POLL_INTERVAL_YANDEX_SECS: yandex override (fallback 5.0)
    """
    global_i = _env_float("APIFY_POLL_INTERVAL_SECS", 3.0)
    yandex_i = _env_float("APIFY_POLL_INTERVAL_YANDEX_SECS", 5.0)
    return yandex_i if _is_yandex(actor_id) else global_i


def _sleep_backoff(attempt: int) -> None:
    """
    Exponential backoff with capped sleep.
      base: APIFY_HTTP_RETRY_BASE_SLEEP_SECS (default 2)
      max : APIFY_HTTP_RETRY_MAX_SLEEP_SECS  (default 30)
    """
    base = _env_int("APIFY_HTTP_RETRY_BASE_SLEEP_SECS", 2)
    max_sleep = _env_int("APIFY_HTTP_RETRY_MAX_SLEEP_SECS", 30)
    sleep = base * (2 ** max(0, attempt))
    sleep = _clamp(int(sleep), 0, int(max_sleep))
    # tiny deterministic jitter
    sleep = min(int(max_sleep), sleep + (attempt % 3))
    time.sleep(float(sleep))


def _request_with_retries(
    *,
    method: str,
    url: str,
    params: Dict[str, Any],
    json_body: Optional[Dict[str, Any]] = None,
    timeout: Tuple[int, int] = (10, 30),
    retries: int = 2,
) -> requests.Response:
    """
    Retries: 429/502/503/504 and requests.RequestException
    """
    retries = _clamp(int(retries), 0, 10)

    for attempt in range(0, retries + 1):
        try:
            resp = requests.request(method, url, params=params, json=json_body, timeout=timeout)
        except requests.RequestException as e:
            if attempt >= retries:
                raise ApifyError(f"Apify request failed: {e}") from e
            _sleep_backoff(attempt)
            continue

        if resp.status_code in (429, 502, 503, 504):
            if attempt >= retries:
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

        return resp

    # unreachable
    raise ApifyError("Apify request failed: unknown error")


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
    Synchronous (single long HTTP request):
      POST /run-sync-get-dataset-items

    Use for quick actors. For long/heavy actors (e.g., Yandex) prefer fire-and-poll.
    """
    tok = _token()
    act = _act_path(actor_id)
    url = f"https://api.apify.com/v2/acts/{act}/run-sync-get-dataset-items"
    params: Dict[str, Any] = {"token": tok, "format": items_format}
    if clean:
        params["clean"] = "true"

    read_timeout = int(timeout_secs) if timeout_secs is not None else _default_timeout_for_actor(actor_id)
    timeout: Tuple[int, int] = (int(connect_timeout_secs), int(read_timeout))

    max_retries = int(retries) if retries is not None else _default_retries_for_actor(actor_id)

    resp = _request_with_retries(
        method="POST",
        url=url,
        params=params,
        json_body=actor_input,
        timeout=timeout,
        retries=max_retries,
    )

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


def run_actor_fire_and_poll_get_dataset_items(
    *,
    actor_id: str,
    actor_input: Dict[str, Any],
    timeout_secs: Optional[int] = None,
    poll_interval_secs: Optional[float] = None,
    items_format: str = "json",
    clean: bool = True,
    connect_timeout_secs: int = 10,
    retries: Optional[int] = None,
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Fire-and-poll strategy (recommended for long actors):
      1) Start run (fast) -> run_id
      2) Poll run status until finished
      3) Fetch dataset items from run.defaultDatasetId

    Returns: (run_id, items)
    """
    tok = _token()
    act = _act_path(actor_id)

    read_timeout = int(timeout_secs) if timeout_secs is not None else _default_timeout_for_actor(actor_id)
    poll_interval = float(poll_interval_secs) if poll_interval_secs is not None else _default_poll_interval_for_actor(actor_id)
    poll_interval = max(0.5, poll_interval)

    max_retries = int(retries) if retries is not None else _default_retries_for_actor(actor_id)

    # 1) Start run (short read timeout)
    start_url = f"https://api.apify.com/v2/acts/{act}/runs"
    start_params: Dict[str, Any] = {"token": tok, "waitForFinish": 0}
    start_timeout: Tuple[int, int] = (int(connect_timeout_secs), int(_env_int("APIFY_START_READ_TIMEOUT_SECS", 30)))

    start_resp = _request_with_retries(
        method="POST",
        url=start_url,
        params=start_params,
        json_body=actor_input,
        timeout=start_timeout,
        retries=max_retries,
    )

    if start_resp.status_code >= 400:
        raise ApifyError(
            f"Apify HTTP {start_resp.status_code}",
            status_code=start_resp.status_code,
            response_text=start_resp.text[:2000],
        )

    try:
        start_json = start_resp.json()
    except ValueError as e:
        raise ApifyError(
            "Apify start run returned non-JSON response",
            status_code=start_resp.status_code,
            response_text=start_resp.text[:2000],
        ) from e

    run_id = None
    if isinstance(start_json, dict):
        run_id = ((start_json.get("data") or {}).get("id")) or (start_json.get("id"))
    if not run_id:
        raise ApifyError(f"Apify start run missing run_id: {str(start_json)[:500]}")

    # 2) Poll status
    run_url = f"https://api.apify.com/v2/actor-runs/{run_id}"
    run_params: Dict[str, Any] = {"token": tok}
    run_timeout: Tuple[int, int] = (int(connect_timeout_secs), int(_env_int("APIFY_POLL_READ_TIMEOUT_SECS", 30)))

    terminal = {"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"}
    started_at = time.time()
    last_status = None
    dataset_id = None

    while True:
        if (time.time() - started_at) > float(read_timeout):
            raise ApifyError(f"Apify run poll timeout after {read_timeout}s (last_status={last_status})", status_code=None)

        run_resp = _request_with_retries(
            method="GET",
            url=run_url,
            params=run_params,
            json_body=None,
            timeout=run_timeout,
            retries=max_retries,
        )

        if run_resp.status_code >= 400:
            raise ApifyError(
                f"Apify HTTP {run_resp.status_code}",
                status_code=run_resp.status_code,
                response_text=run_resp.text[:2000],
            )

        try:
            run_json = run_resp.json()
        except ValueError as e:
            raise ApifyError(
                "Apify run status returned non-JSON response",
                status_code=run_resp.status_code,
                response_text=run_resp.text[:2000],
            ) from e

        data = (run_json.get("data") if isinstance(run_json, dict) else None) or {}
        last_status = (data.get("status") or "").upper() or None
        dataset_id = data.get("defaultDatasetId") or dataset_id

        if last_status in terminal:
            break

        time.sleep(poll_interval)

    if last_status != "SUCCEEDED":
        raise ApifyError(f"Apify run finished with status={last_status}", status_code=None, response_text=str(run_json)[:2000])

    if not dataset_id:
        raise ApifyError(f"Apify SUCCEEDED but defaultDatasetId missing for run_id={run_id}")

    # 3) Fetch dataset items
    items_url = f"https://api.apify.com/v2/datasets/{dataset_id}/items"
    items_params: Dict[str, Any] = {"token": tok, "format": items_format}
    if clean:
        items_params["clean"] = "true"

    items_timeout: Tuple[int, int] = (int(connect_timeout_secs), int(_env_int("APIFY_ITEMS_READ_TIMEOUT_SECS", 60)))

    items_resp = _request_with_retries(
        method="GET",
        url=items_url,
        params=items_params,
        json_body=None,
        timeout=items_timeout,
        retries=max_retries,
    )

    if items_resp.status_code >= 400:
        raise ApifyError(
            f"Apify HTTP {items_resp.status_code}",
            status_code=items_resp.status_code,
            response_text=items_resp.text[:2000],
        )

    try:
        items_json = items_resp.json()
    except ValueError as e:
        raise ApifyError(
            "Apify dataset items returned non-JSON response",
            status_code=items_resp.status_code,
            response_text=items_resp.text[:2000],
        ) from e

    if isinstance(items_json, list):
        return run_id, [x for x in items_json if isinstance(x, dict)]

    if isinstance(items_json, dict) and items_json.get("error"):
        raise ApifyError(
            f"Apify error: {items_json.get('error')}",
            status_code=items_resp.status_code,
            response_text=str(items_json)[:2000],
        )

    return run_id, []
