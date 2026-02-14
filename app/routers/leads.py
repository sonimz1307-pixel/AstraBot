from __future__ import annotations

import time
import os
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


@router.post("/run_full_job")
async def run_full_job(payload: Dict[str, Any] = Body(...)):
    """
    v2 orchestration (STEP 1.2):
    - creates mi_jobs record
    - runs 2GIS actor (sync) for the given city+niche (+limit)
    - saves RAW items into mi_raw_items under this job_id (source=apify_2gis, place_key=item.id)
    - returns job_id + place_keys

    Payload:
    {
      "tg_user_id": 1,
      "city": "астрахань",
      "niche": "школа танцев",
      "limit": 20,                 # optional
      "mode": "fast" | "full",     # optional (for now both run 2GIS)
      "actor_id_2gis": "m_mamaev~2gis-places-scraper"   # optional override
    }
    """
    tg_user_id = payload.get("tg_user_id")
    city_raw = (payload.get("city") or "").strip()
    city = city_raw.lower()
    niche = (payload.get("niche") or payload.get("query") or "").strip()

    if tg_user_id is None:
        raise HTTPException(status_code=400, detail="tg_user_id is required")
    try:
        tg_user_id = int(tg_user_id)
    except Exception:
        raise HTTPException(status_code=400, detail="tg_user_id must be an integer")

    if not city:
        raise HTTPException(status_code=400, detail="city is required")
    if not niche:
        raise HTTPException(status_code=400, detail="niche is required")

    limit = payload.get("limit")
    try:
        limit = int(limit) if limit is not None else None
    except Exception:
        raise HTTPException(status_code=400, detail="limit must be an integer")

    mode = (payload.get("mode") or "full").strip().lower()
    if mode not in ("fast", "full"):
        raise HTTPException(status_code=400, detail="mode must be 'fast' or 'full'")

    # 1) create job
    try:
        job_id = create_job(
            tg_user_id=tg_user_id,
            city=city,
            query=niche,
            queries=[niche],
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": "job_create_failed", "message": str(e)})

    # 2) run 2GIS (sync)
    actor_id = (payload.get("actor_id_2gis") or os.getenv("APIFY_2GIS_ACTOR_ID") or "m_mamaev~2gis-places-scraper").strip()
    if not actor_id:
        raise HTTPException(status_code=500, detail="actor_id_2gis resolved to empty")

    actor_input: Dict[str, Any] = {
    "domain": "2gis.ru",
    "enableGlobalDataset": True,
    "filterRating": "rating_rating_excellent",
    "locationQuery": city_raw or city,
    "query": [niche],
}

# optional override/merge from request (advanced)
actor_input_override = payload.get("actor_input_2gis")
if isinstance(actor_input_override, dict) and actor_input_override:
    actor_input.update(actor_input_override)
    if limit is not None:
        actor_input["maxItems"] = limit

    run_id = f"sync_{int(time.time())}"

    try:
        items = run_actor_sync_get_dataset_items(actor_id=actor_id, actor_input=actor_input)
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

    # 3) save RAW under this job_id
    saved = {"ok": True, "inserted": 0}
    try:
        saved = _insert_raw_items_compat(
            job_id=job_id,
            source="apify_2gis",
            city=city,
            queries=[niche],
            actor_id=actor_id,
            run_id=run_id,
            items=items,
        )
    except Exception as e:
        saved = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    place_keys: List[str] = []
    for it in items or []:
        if isinstance(it, dict) and it.get("id") is not None:
            place_keys.append(str(it["id"]))


    # 4) (STEP 1.3) if mode=full -> for each place_key run Yandex + best match + upsert mi_places
    yandex_actor_id = (payload.get("actor_id_yandex") or os.getenv("APIFY_YANDEX_ACTOR_ID") or "m_mamaev~yandex-maps-places-scraper").strip()
    yandex_max_items = int(payload.get("yandex_maxItems") or payload.get("maxItems") or 6)

# STEP 1.4 guards for large jobs (>20 places)
max_places = payload.get("max_places") or payload.get("maxPlaces") or os.getenv("MI_MAX_PLACES") or None
max_seconds = payload.get("max_seconds") or payload.get("maxSeconds") or os.getenv("MI_MAX_SECONDS") or None
yandex_retries = payload.get("yandex_retries") or payload.get("yandexRetries") or os.getenv("MI_YANDEX_RETRIES") or 1
sleep_ms = payload.get("sleep_ms") or payload.get("sleepMs") or os.getenv("MI_SLEEP_MS") or 0

try:
    max_places = int(max_places) if max_places is not None else None
except Exception:
    raise HTTPException(status_code=400, detail="max_places must be an integer")

try:
    max_seconds = int(max_seconds) if max_seconds is not None else None
except Exception:
    raise HTTPException(status_code=400, detail="max_seconds must be an integer")

try:
    yandex_retries = int(yandex_retries)
except Exception:
    raise HTTPException(status_code=400, detail="yandex_retries must be an integer")

try:
    sleep_ms = int(sleep_ms)
except Exception:
    raise HTTPException(status_code=400, detail="sleep_ms must be an integer")

started_ts = time.time()
stopped_reason = None

if max_places is not None and max_places > 0 and len(place_keys) > max_places:
    place_keys = place_keys[:max_places]

    processed = 0
    failed: List[Dict[str, Any]] = []
    if mode == "full" and place_keys:
        sb = get_supabase()

        # optional: skip already aggregated
        existing = (
            sb.table("mi_places")
            .select("place_key")
            .eq("job_id", job_id)
            .in_("place_key", place_keys)
            .execute()
        )
        try:
            existing_keys = {str(r.get("place_key")) for r in (existing.data or []) if r.get("place_key") is not None}
        except Exception:
            existing_keys = set()

        for pk in place_keys:
    if pk in existing_keys:
        continue

    # time guard
    if max_seconds is not None and max_seconds > 0 and (time.time() - started_ts) >= max_seconds:
        stopped_reason = "max_seconds_reached"
        break

    last_err = None
    for attempt in range(max(0, yandex_retries) + 1):
        try:
            _collect_place_internal(
                sb=sb,
                job_id=job_id,
                place_key=pk,
                actor_id=yandex_actor_id,
                max_items=yandex_max_items,
            )
            processed += 1
            last_err = None
            break
        except HTTPException as e:
            last_err = {"status_code": e.status_code, "detail": e.detail}
            # retry only for apify errors
            detail = e.detail or {}
            is_apify = isinstance(detail, dict) and detail.get("error") in ("apify_http_error", "apify_unexpected_error")
            if attempt < max(0, yandex_retries) and is_apify:
                time.sleep(1.5)
                continue
            break
        except Exception as e:
            last_err = {"error": f"{type(e).__name__}: {e}"}
            if attempt < max(0, yandex_retries):
                time.sleep(1.5)
                continue
            break

    if last_err is not None:
        failed.append({"place_key": pk, **last_err})

    if sleep_ms and sleep_ms > 0:
        time.sleep(sleep_ms / 1000.0)

    return {
        "ok": True,
        "job_id": job_id,
        "mode": mode,
        "apify_2gis": {"actor_id": actor_id, "run_id": run_id, "items_count": len(items)},
        "saved": saved,
        "place_keys": place_keys,
        "sample_items": items[:3],
        "next": "STEP 1.3 is executed when mode=full (Yandex loop + mi_places upsert)",
        "orchestration": {
            "mode": mode,
            "yandex_actor_id": yandex_actor_id if mode == "full" else None,
            "yandex_maxItems": yandex_max_items if mode == "full" else None,
            "places_total": len(place_keys),
            "places_processed": processed if mode == "full" else 0,
            "places_failed": len(failed) if mode == "full" else 0,
            "failed": failed[:10],
            "guards": {
                "max_places": max_places,
                "max_seconds": max_seconds,
                "yandex_retries": yandex_retries,
                "sleep_ms": sleep_ms,
                "stopped_reason": stopped_reason,
                "elapsed_seconds": round(time.time() - started_ts, 2),
            },
        },
    }



def _collect_place_internal(
    *,
    sb,
    job_id: str,
    place_key: str,
    actor_id: str = "m_mamaev~yandex-maps-places-scraper",
    max_items: int = 6,
) -> Dict[str, Any]:
    """Internal helper used by collect_place and run_full_job (STEP 1.3)."""

    # 1) Fetch 2GIS item
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
    city = (resp.data[0].get("city") or base_item.get("city") or "").strip() or "unknown"
    title = (base_item.get("title") or base_item.get("name") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="2GIS item has no title/name to form yandex query")

    yandex_query = f"{title} {city}".strip()

    # 2) Run Yandex actor
    actor_input = {
        "enableGlobalDataset": False,
        "language": "RU",
        "maxItems": int(max_items or 6),
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

    # normalize / ensure id
    def _y_id(it: Dict[str, Any]) -> str:
        url = (it.get("url") or "").strip()
        m = re.search(r"/org/(\d+)/", url)
        if m:
            return m.group(1)
        return url or (it.get("title") or "unknown")

    for it in y_items:
        if not it.get("id"):
            it["id"] = _y_id(it)

    # 3) Save RAW yandex
    saved_raw = {"ok": True, "inserted": 0}
    try:
        saved_raw = _insert_raw_items_compat(
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
        saved_raw = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # 4) Pick BEST yandex by address similarity
    addr_2gis = (base_item.get("address") or "").strip().lower()

    def _norm_addr(s: str) -> str:
        s = (s or "").lower()
        s = re.sub(r"\b(г\.|город)\b", " ", s)
        s = re.sub(r"\b(ул\.|улица|пр\.|проспект|пер\.|переулок|пл\.|площадь)\b", " ", s)
        s = re.sub(r"[^0-9a-zа-яё]+", " ", s, flags=re.IGNORECASE)
        s = re.sub(r"\s+", " ", s).strip()
        return s

    def _house_token(s: str) -> str:
        s = _norm_addr(s)
        m = re.search(r"\b(\d+[a-zа-яё]?)\b", s)
        return m.group(1) if m else ""

    n2 = _norm_addr(addr_2gis)
    h2 = _house_token(addr_2gis)

    def _score(y_addr: str) -> int:
        ny = _norm_addr(y_addr)
        hy = _house_token(y_addr)
        score = 0
        if h2 and hy and h2 == hy:
            score += 5
        t2 = set(n2.split())
        ty = set(ny.split())
        score += len(t2 & ty)
        return score

    best_item = None
    best_score = -1
    for it in y_items:
        ya = (it.get("address") or "").strip()
        sc = _score(ya)
        if sc > best_score:
            best_score = sc
            best_item = it

    if best_item is None and y_items:
        best_item = y_items[0]

    # 5) Upsert into mi_places (final card)
    if best_item:
        website = (best_item.get("website") or "").strip()
        urls = best_item.get("urls") or []
        all_urls = []
        if website:
            all_urls.append(website)
        if isinstance(urls, list):
            all_urls.extend([u for u in urls if isinstance(u, str) and u.strip()])

        seen = set()
        dedup_urls = []
        for u in all_urls:
            if u not in seen:
                seen.add(u)
                dedup_urls.append(u)

        up = {
            "job_id": job_id,
            "place_key": str(place_key),
            "best_2gis": base_item,
            "best_yandex": best_item,
            "site_urls": dedup_urls,
            "social_links": best_item.get("socialLinks") or [],
        }
        try:
            sb.table("mi_places").upsert(up, on_conflict="job_id,place_key").execute()
        except Exception as e:
            raise HTTPException(status_code=500, detail={"error": "mi_places_upsert_failed", "message": str(e)})

    return {
        "job_id": job_id,
        "place_key": str(place_key),
        "yandex_query": yandex_query,
        "apify": {"actor_id": actor_id, "run_id": run_id, "items_count": len(y_items)},
        "saved_raw": saved_raw,
        "best_match": {
            "score": best_score,
            "yandex_place_id": (best_item or {}).get("placeId") or (best_item or {}).get("id"),
            "addr_2gis": base_item.get("address"),
            "addr_yandex": (best_item or {}).get("address"),
            "website": (best_item or {}).get("website"),
        },
    }



@router.post("/collect_place")
async def collect_place(payload: Dict[str, Any] = Body(...)):
    """
    One-button pipeline for ONE company (place_key) inside an existing job:
    1) reads 2GIS item (apify_2gis) by (job_id, place_key)
    2) runs Yandex actor (1 company = 1 run)
    3) saves Yandex RAW under same (job_id, place_key)
    4) selects BEST Yandex match by address similarity
    5) upserts aggregated record into mi_places

    Payload:
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

    result = _collect_place_internal(
        sb=sb,
        job_id=str(job_id),
        place_key=str(place_key),
        actor_id=actor_id,
        max_items=max_items,
    )

    return {
        "ok": True,
        **result,
    }
