from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

import requests


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name, str(default))
    try:
        v = int(raw)
        return v
    except Exception:
        return default


class ApifyError(RuntimeError):
    def __init__(self, message: str, *, status_code: Optional[int] = None, response_text: Optional[str] = None):
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

    We intentionally keep this heuristic simple: if 'yandex' is in actor_id, treat it as a Yandex actor.
    """
    global_default = _env_int("APIFY_RUNSYNC_TIMEOUT_SECS", 180)
    yandex_default = _env_int("APIFY_RUNSYNC_TIMEOUT_YANDEX_SECS", 600)
    if "yandex" in (actor_id or "").lower():
        return yandex_default
    return global_default


def run_actor_sync_get_dataset_items(
    *,
    actor_id: str,
    actor_input: Dict[str, Any],
    timeout_secs: Optional[int] = None,
    items_format: str = "json",
    clean: bool = True,
    connect_timeout_secs: int = 10,
) -> List[Dict[str, Any]]:
    """
    Runs Apify actor synchronously and returns dataset items.
    Uses: /run-sync-get-dataset-items

    IMPORTANT:
    - requests timeout is set as a tuple: (connect_timeout_secs, read_timeout_secs)
      so we don't hang on connect, but allow long reads for heavy actors (e.g., Yandex).
    - If timeout_secs is None, we pick a default based on env vars:
        APIFY_RUNSYNC_TIMEOUT_SECS (global, default 180)
        APIFY_RUNSYNC_TIMEOUT_YANDEX_SECS (yandex override, default 600)
    """
    tok = _token()
    act = _act_path(actor_id)
    url = f"https://api.apify.com/v2/acts/{act}/run-sync-get-dataset-items"
    params = {"token": tok, "format": items_format}
    if clean:
        params["clean"] = "true"

    read_timeout = int(timeout_secs) if timeout_secs is not None else _default_timeout_for_actor(actor_id)
    timeout: Tuple[int, int] = (int(connect_timeout_secs), int(read_timeout))

    try:
        resp = requests.post(url, params=params, json=actor_input, timeout=timeout)
    except requests.RequestException as e:
        raise ApifyError(f"Apify request failed: {e}") from e

    if resp.status_code >= 400:
        raise ApifyError(
            f"Apify HTTP {resp.status_code}",
            status_code=resp.status_code,
            response_text=resp.text[:2000],
        )

    # For format=json Apify returns JSON array
    try:
        data = resp.json()
    except ValueError as e:
        raise ApifyError(
            "Apify returned non-JSON response",
            status_code=resp.status_code,
            response_text=resp.text[:2000],
        ) from e

    if isinstance(data, list):
        # Ensure dict items
        return [x for x in data if isinstance(x, dict)]

    # Sometimes Apify returns {"error":...}
    if isinstance(data, dict) and data.get("error"):
        raise ApifyError(
            f"Apify error: {data.get('error')}",
            status_code=resp.status_code,
            response_text=str(data)[:2000],
        )

    return []
