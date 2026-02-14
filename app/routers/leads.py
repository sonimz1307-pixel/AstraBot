from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException

from app.services.socials_extract import fetch_and_extract_website_data
from app.services.market_model_builder import build_brand_model_from_yandex_items
from app.services.apify_client import run_actor_sync_get_dataset_items, ApifyError
from app.services.mi_storage import create_job, insert_raw_items

router = APIRouter()


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
        saved = insert_raw_items(
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
