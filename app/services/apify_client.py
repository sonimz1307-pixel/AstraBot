"""
Apify API helper.

This module is intentionally small and dependency-light. It provides a stable
interface for the rest of the app:

- run_actor_sync_get_dataset_items(actor_id, actor_input, **kwargs)

Env:
- APIFY_TOKEN (required)
"""
from __future__ import annotations

import os
import json
from typing import Any, Dict, List, Optional, Union, Tuple

import requests


class ApifyError(RuntimeError):
    def __init__(self, message: str, status_code: Optional[int] = None, response_text: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


def _get_token(explicit_token: Optional[str] = None) -> str:
    token = explicit_token or os.getenv("APIFY_TOKEN") or os.getenv("APIFY_API_TOKEN")
    if not token:
        raise ApifyError("APIFY_TOKEN is not set in environment")
    return token


def _actor_to_url_path(actor_id: str) -> str:
    """
    actor_id can be like:
      - "m_mamaev~2gis-places-scraper" (recommended)
      - "username~actorname"
      - "actorId" (Apify internal id)
    """
    actor_id = actor_id.strip()
    if not actor_id:
        raise ApifyError("actor_id is empty")
    return actor_id


def run_actor_sync_get_dataset_items(
    actor_id: str,
    actor_input: Dict[str, Any],
    *,
    token: Optional[str] = None,
    timeout_sec: int = 300,
    clean: bool = True,
    format: str = "json",
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Runs an Actor synchronously and returns dataset items.

    Uses:
      POST https://api.apify.com/v2/acts/{actor_id}/run-sync-get-dataset-items?token=...

    Returns:
      list of dataset items (dicts)

    Raises:
      ApifyError on HTTP errors / invalid responses.
    """
    token_val = _get_token(token)
    actor_path = _actor_to_url_path(actor_id)

    url = f"https://api.apify.com/v2/acts/{actor_path}/run-sync-get-dataset-items"
    params: Dict[str, Any] = {"token": token_val, "format": format}
    if clean:
        params["clean"] = "true"
    if limit is not None:
        params["limit"] = int(limit)

    try:
        resp = requests.post(url, params=params, json=actor_input, timeout=timeout_sec)
    except requests.RequestException as e:
        raise ApifyError(f"Apify request failed: {e}") from e

    if resp.status_code >= 400:
        # Apify typically returns JSON with error details
        text = resp.text
        try:
            j = resp.json()
            text = json.dumps(j, ensure_ascii=False)
        except Exception:
            pass
        raise ApifyError("Apify HTTP error", status_code=resp.status_code, response_text=text)

    # If format=json, body is JSON array
    try:
        data = resp.json()
    except Exception as e:
        raise ApifyError(f"Failed to parse Apify JSON response: {e}", status_code=resp.status_code, response_text=resp.text)

    if not isinstance(data, list):
        raise ApifyError("Unexpected Apify response type (expected list)", status_code=resp.status_code, response_text=resp.text)

    # Ensure items are dicts
    items: List[Dict[str, Any]] = []
    for it in data:
        if isinstance(it, dict):
            items.append(it)
        else:
            items.append({"value": it})
    return items


# Backward-compatible alias (some older code may import this name)
run_actor_sync = run_actor_sync_get_dataset_items
