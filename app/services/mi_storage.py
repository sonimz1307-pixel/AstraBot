from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from supabase import Client, create_client


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


def get_supabase() -> Client:
    """
    Server-side Supabase client for writes.
    Supports your existing env naming:
      - SUPABASE_URL
      - SUPABASE_SERVICE_KEY (Render)
    Also supports:
      - SUPABASE_SERVICE_ROLE_KEY
      - SUPABASE_SERVICE_ROLE
      - SUPABASE_ANON_KEY (fallback, not recommended for writes)
    """
    url = _env("SUPABASE_URL")
    key = (
        _env("SUPABASE_SERVICE_KEY")
        or _env("SUPABASE_SERVICE_ROLE_KEY")
        or _env("SUPABASE_SERVICE_ROLE")
        or _env("SUPABASE_ANON_KEY")
    )
    if not url or not key:
        raise RuntimeError("Missing SUPABASE_URL and SUPABASE_SERVICE_KEY (or SUPABASE_SERVICE_ROLE_KEY).")
    return create_client(url, key)


def insert_raw_items(
    *,
    job_id: Optional[str] = None,
    source: str,
    city: str,
    queries: List[str],
    actor_id: str,
    run_id: str,
    items: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Bulk upsert raw Apify items into public.mi_raw_items.

    Why you saw error code 21000:
      Apify 2GIS dataset can contain the same place multiple times (across different queries).
      If duplicates are present inside one upsert batch, Postgres raises:
        'ON CONFLICT DO UPDATE command cannot affect row a second time'

    Fix:
      Deduplicate items by (source, item['id']) BEFORE upsert.
    """
    sb = get_supabase()

    # Build rows and dedupe by (source, item_id)
    dedup: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for it in items or []:
        item_id = (it or {}).get("id")
        if not item_id:
            # item_id is NOT NULL in DB now, so skip items without id
            continue

        key = (source, str(item_id))
        # Keep last occurrence
        dedup[key] = {
            "job_id": job_id,
            "source": source,
            "city": city,
            "query": it.get("searchString"),
            "queries": queries,
            "actor_id": actor_id,
            "run_id": run_id,
            "item": it,
        }

    rows = list(dedup.values())

    if not rows:
        return {"ok": True, "attempted": 0, "deduped": 0, "affected": 0}

    attempted = len(items or [])
    deduped = len(rows)

    res = sb.table("mi_raw_items").upsert(rows, on_conflict="source,item_id").execute()
    affected = len(getattr(res, "data", []) or []) if res is not None else 0

    return {"ok": True, "attempted": attempted, "deduped": deduped, "affected": affected}


def create_job(
    *,
    tg_user_id: int,
    city: str,
    query: Optional[str] = None,
    queries: Optional[List[str]] = None,
) -> str:
    """Create a new market-intel run (job) and return its UUID."""
    sb = get_supabase()
    payload: Dict[str, Any] = {
        "tg_user_id": int(tg_user_id),
        "city": (city or "unknown").strip().lower(),
        "status": "running",
    }
    if query is not None:
        payload["query"] = str(query)
    if queries is not None:
        payload["queries"] = queries

    res = sb.table("mi_jobs").insert(payload).execute()
    data = getattr(res, "data", None) or []
    if not data or not isinstance(data, list) or not data[0].get("id"):
        raise RuntimeError("Failed to create mi_jobs row")
    return str(data[0]["id"])
