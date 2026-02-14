from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from supabase import create_client, Client


def _norm_name(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


def get_supabase() -> Client:
    """
    Uses your existing Supabase project.
    Prefer SERVICE ROLE key for server-side writes.
    """
    url = _env("SUPABASE_URL")
    key = _env("SUPABASE_SERVICE_ROLE_KEY") or _env("SUPABASE_ANON_KEY")
    if not url or not key:
        raise RuntimeError("Missing SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_ANON_KEY).")
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
    Stores raw Apify dataset items into mi_raw_items (bulk insert).
    """
    sb = get_supabase()
    rows = []
    for it in items or []:
        rows.append({
            "source": source,
            "city": city,
            "queries": queries,
            "actor_id": actor_id,
            "run_id": run_id,
            "item": it,
        })
    if not rows:
        return {"ok": True, "inserted": 0}
    # Bulk insert; PostgREST will accept list
    sb.table("mi_raw_items").insert(rows).execute()
    return {"ok": True, "inserted": len(rows)}


def upsert_brand(model: Dict[str, Any]) -> str:
    """
    Upserts into mi_brands by normalized_name unique constraint (handled in SQL via generated column + unique index).
    Returns brand_id.
    """
    sb = get_supabase()
    brand = (model or {}).get("brand") or {}
    name = (brand.get("name") or "").strip()
    if not name:
        raise RuntimeError("brand.name is empty")

    payload = {
        "name": name,
        "website": brand.get("website"),
        "avg_rating": brand.get("avg_rating"),
        "total_reviews": brand.get("total_reviews"),
        "digital_score": model.get("digital_score"),
        "phones": model.get("phones") or [],
        "emails": model.get("emails") or [],
    }

    norm = " ".join(name.lower().split())
    existing = sb.table("mi_brands").select("id").eq("normalized_name", norm).limit(1).execute().data
    if existing:
        brand_id = existing[0]["id"]
        sb.table("mi_brands").update(payload).eq("id", brand_id).execute()
        return brand_id

    inserted = sb.table("mi_brands").insert(payload).execute().data
    return inserted[0]["id"]


def replace_branches(brand_id: str, branches: List[Dict[str, Any]]) -> int:
    sb = get_supabase()
    sb.table("mi_branches").delete().eq("brand_id", brand_id).execute()

    rows = []
    for b in branches or []:
        rows.append({
            "brand_id": brand_id,
            "name": b.get("name"),
            "address": b.get("address"),
            "phone": b.get("phone"),
            "rating": b.get("rating"),
            "reviews_count": b.get("reviews_count"),
            "website": b.get("website"),
            "yandex_url": b.get("yandex_url"),
            "two_gis_url": b.get("two_gis_url"),
            "source": b.get("source"),
        })
    if rows:
        sb.table("mi_branches").insert(rows).execute()
    return len(rows)


def upsert_brand_socials(brand_id: str, socials: List[Dict[str, Any]]) -> int:
    """
    Replaces socials for simplicity (delete + insert).
    """
    sb = get_supabase()
    sb.table("mi_brand_socials").delete().eq("brand_id", brand_id).execute()

    rows = []
    for s in socials or []:
        url = (s.get("url") or "").strip()
        platform = (s.get("platform") or "").strip()
        if not url or not platform:
            continue
        rows.append({
            "brand_id": brand_id,
            "platform": platform,
            "url": url,
            "kind": s.get("kind") or "",
        })
    if rows:
        sb.table("mi_brand_socials").insert(rows).execute()
    return len(rows)


def insert_web_snapshot(
    *,
    brand_id: str,
    website: str,
    source_type: str,
    snapshot: Dict[str, Any],
) -> None:
    sb = get_supabase()
    sb.table("mi_web_snapshots").insert({
        "brand_id": brand_id,
        "website": website,
        "source_type": source_type,
        "snapshot": snapshot,
    }).execute()
