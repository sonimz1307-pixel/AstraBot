
"""
Market Intelligence storage layer for Supabase.

Design goals:
- NO conflicts with existing bot_* tables/services: we use mi_* tables only.
- Safe to import anywhere: lazy-init Supabase client.
- Works with service role key (recommended on backend).

Env:
- SUPABASE_URL
- SUPABASE_SERVICE_ROLE_KEY (preferred) or SUPABASE_KEY
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

try:
    from supabase import create_client  # supabase-py
except Exception:  # pragma: no cover
    create_client = None  # type: ignore


_SB = None


def _get_sb():
    global _SB
    if _SB is not None:
        return _SB
    if create_client is None:
        raise RuntimeError("supabase-py is not installed. Add 'supabase' to requirements.txt")
    url = (os.getenv("SUPABASE_URL") or "").strip()
    key = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY") or "").strip()
    if not url or not key:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set")
    _SB = create_client(url, key)
    return _SB


_norm_ws = re.compile(r"\s+", re.UNICODE)


def normalize_name(s: str) -> str:
    s = (s or "").strip().lower()
    s = _norm_ws.sub(" ", s)
    return s


def upsert_brand(*, city: str, name: str, category: str | None = None, website: str | None = None) -> Dict[str, Any]:
    sb = _get_sb()
    payload: Dict[str, Any] = {
        "city": city,
        "name": name,
        "name_norm": normalize_name(name),
    }
    if category:
        payload["category"] = category
    if website:
        payload["website"] = website

    # upsert by unique (city, name_norm)
    r = sb.table("mi_brands").upsert(payload, on_conflict="city,name_norm").execute()
    if not r.data:
        # fetch existing row if supabase doesn't return it
        r2 = sb.table("mi_brands").select("*").eq("city", city).eq("name_norm", payload["name_norm"]).limit(1).execute()
        return (r2.data[0] if r2.data else payload)
    return r.data[0]


def upsert_branch(
    *,
    brand_id: str,
    source: str,
    source_place_id: str | None = None,
    source_url: str | None = None,
    title: str | None = None,
    address: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    phone: str | None = None,
    rating: float | None = None,
    reviews_count: int | None = None,
    website: str | None = None,
    taplink: str | None = None,
) -> Dict[str, Any]:
    sb = _get_sb()
    payload: Dict[str, Any] = {
        "brand_id": brand_id,
        "source": source,
        "source_place_id": source_place_id,
        "source_url": source_url,
        "title": title,
        "address": address,
        "lat": lat,
        "lon": lon,
        "phone": phone,
        "rating": rating,
        "reviews_count": reviews_count,
        "website": website,
        "taplink": taplink,
    }

    # choose conflict target dynamically
    if source_place_id:
        r = sb.table("mi_branches").upsert(payload, on_conflict="source,source_place_id").execute()
    elif source_url:
        r = sb.table("mi_branches").upsert(payload, on_conflict="source,source_url").execute()
    else:
        # no stable key -> insert
        r = sb.table("mi_branches").insert(payload).execute()

    if not r.data:
        return payload
    return r.data[0]


def insert_raw_item(
    *,
    source: str,
    item: Dict[str, Any],
    city: str | None = None,
    query: str | None = None,
    actor_id: str | None = None,
    run_id: str | None = None,
) -> None:
    sb = _get_sb()
    sb.table("mi_raw_items").insert(
        {
            "source": source,
            "city": city,
            "query": query,
            "actor_id": actor_id,
            "run_id": run_id,
            "item": item,
        }
    ).execute()


def upsert_brand_social(
    *,
    brand_id: str,
    platform: str,
    url: str,
    kind: str | None = None,
) -> None:
    sb = _get_sb()
    sb.table("mi_brand_socials").upsert(
        {
            "brand_id": brand_id,
            "platform": platform,
            "url": url,
            "kind": kind,
        },
        on_conflict="brand_id,platform,url",
    ).execute()


def insert_web_snapshot(*, branch_id: str, url: str, status: str | None, payload: Dict[str, Any] | None) -> None:
    sb = _get_sb()
    sb.table("mi_web_snapshots").insert(
        {"branch_id": branch_id, "url": url, "status": status, "payload": payload}
    ).execute()
