from __future__ import annotations

import os
from typing import Any, Dict, List, Tuple, Optional

from supabase import Client, create_client


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


def get_supabase() -> Client:
    """
    Server-side Supabase client for writes.
    Env:
      - SUPABASE_URL
      - SUPABASE_SERVICE_KEY (or SUPABASE_SERVICE_ROLE_KEY)
    """
    url = _env("SUPABASE_URL")
    key = _env("SUPABASE_SERVICE_KEY") or _env("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise RuntimeError("Missing SUPABASE_URL and SUPABASE_SERVICE_KEY (or SUPABASE_SERVICE_ROLE_KEY).")
    return create_client(url, key)


def create_job(*, tg_user_id: int, city: str, query: Optional[str] = None, queries: Optional[List[str]] = None) -> str:
    sb = get_supabase()
    payload: Dict[str, Any] = {
        "tg_user_id": int(tg_user_id),
        "city": (city or "unknown").strip().lower(),
        "query": (query or None),
        "queries": queries or None,
        "status": "running",
    }
    res = sb.table("mi_jobs").insert(payload).execute()
    if not res.data:
        raise RuntimeError("Failed to create job")
    return res.data[0]["id"]


def insert_raw_items(
    *,
    job_id: Optional[str] = None,
    place_key: Optional[str] = None,
    source: str,
    city: str,
    queries: List[str],
    actor_id: str,
    run_id: str,
    items: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Bulk upsert RAW Apify items into mi_raw_items.
    Dedup by (source, item_id) where item_id is GENERATED in DB from item->>'id'.

    - job_id: optional link to mi_jobs
    - place_key: optional link to a specific place (inside job). For 2GIS items we default to item['id'].
    """
    source = (source or "").strip()
    city = (city or "unknown").strip().lower()
    actor_id = (actor_id or "").strip()
    run_id = (run_id or "").strip()
    queries = [str(x).strip() for x in (queries or []) if str(x).strip()]

    if not source:
        raise ValueError("source is required")
    if not actor_id:
        raise ValueError("actor_id is required")
    if not run_id:
        raise ValueError("run_id is required")

    dedup: Dict[Tuple[str, str], Dict[str, Any]] = {}
    attempted = 0

    for it in items or []:
        if not isinstance(it, dict):
            continue
        item_id = it.get("id")
        if item_id is None:
            # Must have id for generated column to work
            continue
        item_id_str = str(item_id).strip()
        if not item_id_str:
            continue

        attempted += 1

        row_place_key = place_key
        if not row_place_key and source == "apify_2gis":
            row_place_key = item_id_str

        key = (source, item_id_str)
        dedup[key] = {
            "job_id": job_id,
            "place_key": row_place_key,
            "source": source,
            "city": city,
            "query": it.get("searchString") or (queries[0] if queries else None),
            "queries": queries,
            "actor_id": actor_id,
            "run_id": run_id,
            "item": it,
        }

    sb = get_supabase()
    rows = list(dedup.values())
    if not rows:
        return {"ok": True, "attempted": 0, "deduped": 0, "affected": 0}

    # IMPORTANT: do NOT pass item_id (generated column). Conflict target uses generated item_id.
    res = sb.table("mi_raw_items").upsert(rows, on_conflict="source,item_id").execute()
    affected = len(res.data or [])
    return {"ok": True, "attempted": attempted, "deduped": len(rows), "affected": affected}
