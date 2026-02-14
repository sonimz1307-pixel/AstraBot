from __future__ import annotations

import time
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException

from app.services.socials_extract import fetch_and_extract_website_data
from app.services.market_model_builder import build_brand_model_from_yandex_items
from app.services.apify_client import run_actor_sync_get_dataset_items, ApifyError
from app.services.mi_storage import create_job, insert_raw_items, get_supabase

router = APIRouter()

def _insert_raw_items_compat(**kwargs):
    """Compatibility wrapper: older insert_raw_items may not accept some kwargs (e.g., place_key/job_id)."""
    try:
        return insert_raw_items(**kwargs)
    except TypeError:
        # Drop unknown keys progressively
        for k in ["place_key", "job_id"]:
            if k in kwargs:
                kwargs = dict(kwargs)
                kwargs.pop(k, None)
                try:
                    return insert_raw_items(**kwargs)
                except TypeError:
                    pass
        raise



@router.get("/ping")
async def ping():
    return {"ok": True, "service": "leads", "ping": "pong"}


@router.get("/extract_site")
async def extract_site(url: str):
    return await fetch_and_extract_website_data(url)


@router.post("/build_brand")
async def build_brand_endpoint(payload: Dict[str, Any] = Body(...)):
    """
    Legacy: builds brand model from already prepared items (usually yandex/2gis-normalized).
    """
    items = payload.get("items") or []
    return await build_brand_model_from_yandex_items(items)


@router.post("/run_apify_build_brand")
async def run_apify_build_brand(payload: Dict[str, Any] = Body(...)):
    """
    Runs an Apify Actor (sync), saves RAW items into Supabase mi_raw_items,
    and returns a lightweight summary.

    Expected payload:
    {
      "actor_id": "m_mamaev~2gis-places-scraper",
      "actor_input": {...},
      "meta": {"city": "астрахань", "queries": ["школа танцев", "танцы"]}   # optional
    }
    """
    actor_id = (payload.get("actor_id") or "").strip()
    actor_input = payload.get("actor_input") or {}
    meta = payload.get("meta") or {}

    # Optional: bind run to a конкретному пользователю TG-бота
    tg_user_id = payload.get("tg_user_id") or meta.get("tg_user_id")
    if tg_user_id is not None:
        try:
            tg_user_id = int(tg_user_id)
        except Exception:
            raise HTTPException(status_code=400, detail="tg_user_id must be an integer")

    if not actor_id:
        raise HTTPException(status_code=400, detail="actor_id is required")
    if not isinstance(actor_input, dict):
        raise HTTPException(status_code=400, detail="actor_input must be an object")

    # Derive city/queries if not provided explicitly
    city = (meta.get("city") or actor_input.get("locationQuery") or actor_input.get("location") or "").strip().lower()
    queries: List[str] = []

    q = meta.get("queries")
    if isinstance(q, list):
        queries = [str(x).strip() for x in q if str(x).strip()]
    elif isinstance(q, str) and q.strip():
        queries = [q.strip()]

    if not queries:
        # Actor uses "query" (array) in m_mamaev~2gis-places-scraper
        aq = actor_input.get("query")
        if isinstance(aq, list):
            queries = [str(x).strip() for x in aq if str(x).strip()]
        elif isinstance(aq, str) and aq.strip():
            queries = [aq.strip()]
        else:
            # Some actors use "search"
            s = actor_input.get("search")
            if isinstance(s, str) and s.strip():
                queries = [s.strip()]

    if not city:
        # Keep DB constraint happy; you can replace with real city later
        city = "unknown"

    run_id = f"sync_{int(time.time())}"

    job_id = None
    if tg_user_id is not None:
        try:
            # query (single) stored for convenience; queries (list) stored fully
            job_id = create_job(tg_user_id=tg_user_id, city=city, query=(queries[0] if queries else None), queries=queries)
        except Exception as e:
            raise HTTPException(status_code=500, detail={"error": "job_create_failed", "message": str(e)})

    try:
        items = run_actor_sync_get_dataset_items(actor_id=actor_id, actor_input=actor_input)
    except ApifyError as e:
        # Return readable error to shell (so you don't see only "Internal Server Error")
        raise HTTPException(
            status_code=400,
            detail={
                "error": "apify_http_error",
                "message": str(e),
                "status_code": getattr(e, "status_code", None),
                "response": getattr(e, "response_text", None),
            },
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": "apify_unexpected_error", "message": str(e)})

    saved = {"ok": True, "inserted": 0}
    try:
        saved = _insert_raw_items_compat(
            job_id=job_id,
            source="apify_2gis",
            city=city,
            queries=queries,
            actor_id=actor_id,
            run_id=run_id,
            items=items,
        )
    except Exception as e:
        # Do NOT fail the whole endpoint if saving fails; return debug.
        saved = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    return {
        "ok": True,
        "job": {"id": job_id, "tg_user_id": tg_user_id},
        "meta": {"source": "apify_2gis", "city": city, "queries": queries},
        "apify": {"actor_id": actor_id, "run_id": run_id, "items_count": len(items)},
        "saved": saved,
        "sample_items": items[:3],
    }

@router.post("/run_apify_yandex_for_place")
async def run_apify_yandex_for_place(payload: Dict[str, Any] = Body(...)):
    """
    Runs Yandex Maps Places Scraper for a single 2GIS place inside a job, and saves results as RAW items
    with the same job_id + place_key (2GIS firm id).
    Expected payload:
    {
      "job_id": "...uuid...",
      "place_key": "70000001028864385",
      "actor_id": "m_mamaev~yandex-maps-places-scraper",   # optional
      "maxItems": 6                                       # optional
    }
    """
    job_id = payload.get("job_id")
    place_key = payload.get("place_key")
    if not job_id or not place_key:
        raise HTTPException(status_code=400, detail="job_id and place_key are required")

    actor_id = (payload.get("actor_id") or "m_mamaev~yandex-maps-places-scraper").strip()
    max_items = int(payload.get("maxItems") or 6)

    sb = get_supabase()

    # Fetch the 2GIS source item for this place_key
    resp = (
        sb.table("mi_raw_items")
        .select("item, city")
        .eq("job_id", job_id)
        .eq("place_key", str(place_key))
        .eq("source", "apify_2gis")
        .limit(1)
        .execute()
    )
    if not resp.data:
        raise HTTPException(status_code=404, detail="2GIS item not found for job_id + place_key")

    base_item = resp.data[0].get("item") or {}
    city = (resp.data[0].get("city") or base_item.get("city") or "").strip()
    title = (base_item.get("title") or base_item.get("name") or "").strip()
    if not city:
        city = "unknown"
    if not title:
        raise HTTPException(status_code=400, detail="2GIS item has no title/name to form yandex query")

    yandex_query = f"{title} {city}".strip()

    actor_input = {
        "enableGlobalDataset": False,
        "language": "RU",
        "maxItems": max_items,
        "query": yandex_query,
    }

    run_id = f"sync_{int(time.time())}"

    try:
        y_items = run_actor_sync_get_dataset_items(actor_id=actor_id, actor_input=actor_input)
    except ApifyError as e:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "apify_http_error",
                "message": str(e),
                "status_code": getattr(e, "status_code", None),
                "response": getattr(e, "response_text", None),
            },
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": "apify_unexpected_error", "message": str(e)})

    # Ensure each item has a stable id for generated item_id column (extract org id from URL)
    def _y_id(it: Dict[str, Any]) -> str:
        url = (it.get("url") or "").strip()
        m = re.search(r"/org/(\d+)/", url)
        if m:
            return m.group(1)
        return url or (it.get("title") or "unknown")

    for it in y_items:
        if not it.get("id"):
            it["id"] = _y_id(it)

    saved = {"ok": True, "inserted": 0}
    try:
        saved = _insert_raw_items_compat(
            job_id=job_id,
            place_key=str(place_key),
            source="apify_yandex",
            city=city.lower(),
            queries=[yandex_query],
            actor_id=actor_id,
            run_id=run_id,
            items=y_items,
        )
    except Exception as e:
        saved = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    return {
        "ok": True,
        "job_id": job_id,
        "place_key": str(place_key),
        "yandex_query": yandex_query,
        "apify": {"actor_id": actor_id, "run_id": run_id, "items_count": len(y_items)},
        "saved": saved,
        "sample_items": y_items[:3],
    }
