from __future__ import annotations

import os
from typing import Any, Dict, List

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
    source: str,
    city: str,
    queries: List[str],
    actor_id: str,
    run_id: str,
    items: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Bulk insert raw Apify items into public.mi_raw_items.
    Table is expected to have columns:
      source text, city text, queries text[], actor_id text, run_id text, item jsonb
    """
    sb = get_supabase()
    rows: List[Dict[str, Any]] = []
    for it in items or []:
        rows.append(
            {
                "source": source,
                "city": city,
                "queries": queries,
                "actor_id": actor_id,
                "run_id": run_id,
                "item": it,
            }
        )

    if not rows:
        return {"ok": True, "inserted": 0}

    res = sb.table("mi_raw_items").insert(rows).execute()
    # supabase-py returns PostgrestResponse-like object; be defensive
    inserted = len(getattr(res, "data", []) or []) if res is not None else len(rows)
    return {"ok": True, "inserted": inserted}
