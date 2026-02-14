"""Market-intel storage helpers (Supabase).

This version is aligned to the schema you currently have:
- public.mi_raw_items: stores raw JSON payloads (columns: source, city, item)
- public.mi_brands:    id, city (NOT NULL), name, name_norm, website, taplink, created_at, updated_at
- public.mi_branches:  id, brand_id, source, source_place_id, source_url, title, address, lat, lon,
                       phone, rating, reviews_count, website, taplink, created_at, updated_at

It intentionally does NOT touch your existing bot_* tables.

Env vars expected (already in Render):
- SUPABASE_URL
- SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_SERVICE_KEY)

Notes:
- We avoid inserting columns that might not exist (avg_rating/total_reviews/digital_score/etc.).
- Any extra metadata (queries, actor_id, run_id) is stored inside `item` as embedded meta.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, List, Optional

from supabase import create_client, Client


def _get_env(*names: str) -> Optional[str]:
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return None


_supabase_client: Optional[Client] = None


def get_supabase() -> Client:
    global _supabase_client
    if _supabase_client is not None:
        return _supabase_client

    url = _get_env("SUPABASE_URL")
    key = _get_env("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SERVICE_KEY", "SUPABASE_SERVICE_ROLE")

    if not url or not key:
        raise RuntimeError("Supabase env vars not set: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY")

    _supabase_client = create_client(url, key)
    return _supabase_client


_ws_re = re.compile(r"\s+")


def normalize_name(s: str) -> str:
    s = (s or "").strip().lower()
    s = _ws_re.sub(" ", s)
    return s


def insert_raw_items(*, source: str, city: str, items: List[Dict[str, Any]], meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Insert raw items into mi_raw_items.

    We only rely on columns: source, city, item.
    All other context is embedded into item['__meta'].
    """
    sb = get_supabase()

    meta = meta or {}
    rows = []
    ts = int(time.time())
    for i, it in enumerate(items or []):
        payload = dict(it) if isinstance(it, dict) else {"value": it}
        payload.setdefault("__meta", {})
        # keep existing meta but add ours
        payload["__meta"].update({
            **meta,
            "source": source,
            "city": city,
            "seq": i,
            "ts": ts,
        })
        rows.append({
            "source": source,
            "city": city,
            "item": payload,
        })

    if not rows:
        return {"ok": True, "inserted": 0}

    res = sb.table("mi_raw_items").insert(rows).execute()
    data = getattr(res, "data", None)
    return {"ok": True, "inserted": len(data) if data is not None else len(rows)}


def upsert_brand(*, city: str, name: str, website: str = "", taplink: str = "") -> str:
    """Upsert into mi_brands and return brand_id."""
    sb = get_supabase()

    city = (city or "").strip().lower()
    if not city:
        raise ValueError("city is required (mi_brands.city is NOT NULL)")

    name = (name or "").strip()
    if not name:
        raise ValueError("brand name is required")

    name_norm = normalize_name(name)

    payload = {
        "city": city,
        "name": name,
        "name_norm": name_norm,
        "website": website or "",
        "taplink": taplink or "",
    }

    # Your DB has unique index mi_brands_city_name_norm_ux.
    res = sb.table("mi_brands").upsert(payload, on_conflict="city,name_norm").select("id").execute()

    data = getattr(res, "data", None) or []
    if not data or "id" not in data[0]:
        raise RuntimeError("Supabase upsert to mi_brands returned no id")

    return data[0]["id"]


def replace_branches(*, brand_id: str, branches: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Replace branches for brand_id in mi_branches.

    We delete existing branches for this brand_id then insert the new set.
    This avoids conflicts with unknown unique constraints.
    """
    sb = get_supabase()

    # delete old
    sb.table("mi_branches").delete().eq("brand_id", brand_id).execute()

    rows = []
    for b in branches or []:
        rows.append({
            "brand_id": brand_id,
            "source": (b.get("source") or "").strip() or None,
            "source_place_id": (b.get("source_place_id") or "").strip() or None,
            "source_url": (b.get("source_url") or b.get("yandex_url") or b.get("2gis_url") or "").strip() or None,
            "title": (b.get("title") or b.get("name") or "").strip() or None,
            "address": (b.get("address") or "").strip() or None,
            "lat": b.get("lat"),
            "lon": b.get("lon"),
            "phone": (b.get("phone") or "").strip() or None,
            "rating": b.get("rating"),
            "reviews_count": b.get("reviews_count"),
            "website": (b.get("website") or "").strip() or None,
            "taplink": (b.get("taplink") or "").strip() or None,
        })

    if not rows:
        return {"ok": True, "inserted": 0}

    res = sb.table("mi_branches").insert(rows).execute()
    data = getattr(res, "data", None)
    return {"ok": True, "inserted": len(data) if data is not None else len(rows)}
