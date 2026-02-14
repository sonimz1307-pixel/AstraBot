from __future__ import annotations

import os
from typing import Any, Dict, List

from supabase import Client, create_client


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


def get_supabase() -> Client:
    url = _env("SUPABASE_URL")
    key = (
        _env("SUPABASE_SERVICE_KEY")
        or _env("SUPABASE_SERVICE_ROLE_KEY")
        or _env("SUPABASE_SERVICE_ROLE")
        or _env("SUPABASE_ANON_KEY")
    )
    if not url or not key:
        raise RuntimeError(
            "Missing SUPABASE_URL and SUPABASE_SERVICE_KEY (or SUPABASE_SERVICE_ROLE_KEY)."
        )
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

    sb = get_supabase()

    rows: List[Dict[str, Any]] = []
    for it in items or []:
        rows.append(
            {
                "source": source,
                "city": city,
                "query": it.get("searchString"),  # <-- важно
                "queries": queries,
                "actor_id": actor_id,
                "run_id": run_id,
                "item": it,
            }
        )

    if not rows:
        return {"ok": True, "attempted": 0}

    # Ключевой момент — UPSERT вместо INSERT
    res = (
        sb.table("mi_raw_items")
        .upsert(rows, on_conflict="source,item_id")
        .execute()
    )

    attempted = len(rows)
    affected = len(getattr(res, "data", []) or []) if res else 0

    return {
        "ok": True,
        "attempted": attempted,
        "affected": affected,
    }
