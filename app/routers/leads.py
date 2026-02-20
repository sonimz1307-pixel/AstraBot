from __future__ import annotations

import time
import os
import re
import difflib
import json
import traceback
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException

from app.services.socials_extract import fetch_and_extract_website_data
from app.services.market_model_builder import build_brand_model_from_yandex_items
from app.services.apify_client import (
    run_actor_sync_get_dataset_items,
    run_actor_fire_and_poll_get_dataset_items,
    ApifyError,
)
from app.services.mi_storage import create_job, insert_raw_items, get_supabase

router = APIRouter()

logger = logging.getLogger("leads")

# Keep logs quiet by default (Render can be noisy); enable detailed events with LEADS_DEBUG=1
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO)


def _dbg_enabled() -> bool:
    return str(os.getenv("LEADS_DEBUG", "")).strip().lower() in ("1", "true", "yes")


def _log_evt(evt: str, **kw):
    """Debug events: OFF by default to avoid log spam on Render."""
    if not _dbg_enabled():
        return
    payload = {"evt": evt, **kw}
    try:
        logger.info(json.dumps(payload, ensure_ascii=False, default=str))
    except Exception:
        logger.info(f"{evt} {kw}")


def _job_state_upsert(sb, job_id: str, **fields):
    data = {"job_id": job_id, **fields}
    if "updated_at" not in data:
        data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return sb.table("mi_job_state").upsert(data, on_conflict="job_id").execute()


def _job_state_get(sb, job_id: str) -> Dict[str, Any]:
    resp = sb.table("mi_job_state").select("*").eq("job_id", job_id).limit(1).execute()
    if not resp.data:
        return {}
    return resp.data[0] or {}


def _job_state_try_claim(sb, job_id: str) -> bool:
    """Claim queued -> running to avoid duplicate execution across workers."""
    try:
        resp = (
            sb.table("mi_job_state")
            .update({"status": "running"})
            .eq("job_id", job_id)
            .eq("status", "queued")
            .execute()
        )
        return bool(getattr(resp, "data", None))
    except Exception:
        return False


def _insert_raw_items_compat(**kwargs):
    """Compatibility wrapper: older insert_raw_items may not accept some kwargs."""
    try:
        return insert_raw_items(**kwargs)
    except TypeError:
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


@router.get("/job/{job_id}/status")
async def job_status(job_id: str):
    sb = get_supabase()
    st = _job_state_get(sb, job_id)
    if not st:
        raise HTTPException(status_code=404, detail="job state not found")
    return {"ok": True, "job_id": job_id, "state": st}


@router.get("/extract_site")
async def extract_site(url: str):
    return await fetch_and_extract_website_data(url)


@router.post("/build_brand")
async def build_brand_endpoint(payload: Dict[str, Any] = Body(...)):
    items = payload.get("items") or []
    return await build_brand_model_from_yandex_items(items)


@router.post("/run_apify_build_brand")
async def run_apify_build_brand(payload: Dict[str, Any] = Body(...)):
    actor_id = (payload.get("actor_id") or "").strip()
    actor_input = payload.get("actor_input") or {}
    meta = payload.get("meta") or {}

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

    city = (meta.get("city") or actor_input.get("locationQuery") or actor_input.get("location") or "").strip().lower()
    queries: List[str] = []

    q = meta.get("queries")
    if isinstance(q, list):
        queries = [str(x).strip() for x in q if str(x).strip()]
    elif isinstance(q, str) and q.strip():
        queries = [q.strip()]

    if not queries:
        aq = actor_input.get("query")
        if isinstance(aq, list):
            queries = [str(x).strip() for x in aq if str(x).strip()]
        elif isinstance(aq, str) and aq.strip():
            queries = [aq.strip()]
        else:
            s = actor_input.get("search")
            if isinstance(s, str) and s.strip():
                queries = [s.strip()]

    if not city:
        city = "unknown"

    run_id = f"sync_{int(time.time())}"

    job_id = None
    if tg_user_id is not None:
        try:
            job_id = create_job(tg_user_id=tg_user_id, city=city, query=(queries[0] if queries else None), queries=queries)
        except Exception as e:
            raise HTTPException(status_code=500, detail={"error": "job_create_failed", "message": str(e)})

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
        saved = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    return {
        "ok": True,
        "job": {"id": job_id, "tg_user_id": tg_user_id},
        "meta": {"source": "apify_2gis", "city": city, "queries": queries},
        "apify": {"actor_id": actor_id, "run_id": run_id, "items_count": len(items)},
        "saved": saved,
        "sample_items": items[:3],
    }


def _normalize_text(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^0-9a-zа-яё ,.\-/#]", "", s)
    return s.strip()


def _best_match_yandex(base_title: str, base_addr: str, items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not items:
        return None

    bt = _normalize_text(base_title)
    ba = _normalize_text(base_addr)

    best = None
    best_score = -1.0

    for it in items:
        if not isinstance(it, dict):
            continue
        yt = _normalize_text(str(it.get("title") or it.get("name") or ""))
        ya = _normalize_text(str(it.get("address") or it.get("addressText") or it.get("fullAddress") or ""))

        addr_score = difflib.SequenceMatcher(None, ba, ya).ratio() if ba and ya else 0.0
        title_score = difflib.SequenceMatcher(None, bt, yt).ratio() if bt and yt else 0.0

        score = addr_score * 0.75 + title_score * 0.25
        if score > best_score:
            best_score = score
            best = it

    return best or items[0]


def _extract_site_urls_and_socials(y_it: Dict[str, Any]) -> tuple[list[str], dict]:
    site_urls: list[str] = []
    social: dict = {}

    def _push_url(u: Any):
        if isinstance(u, str):
            u2 = u.strip()
            if u2 and u2 not in site_urls:
                site_urls.append(u2)

    if not isinstance(y_it, dict):
        return site_urls, social

    for k in ("website", "site", "url", "webSite", "web", "websites", "site_urls"):
        v = y_it.get(k)
        if isinstance(v, str):
            _push_url(v)
        elif isinstance(v, list):
            for x in v:
                _push_url(x)

    for k in ("links", "externalLinks", "contactLinks", "contacts"):
        v = y_it.get(k)
        if isinstance(v, dict):
            for vv in v.values():
                _push_url(vv)
        elif isinstance(v, list):
            for it in v:
                if isinstance(it, dict):
                    for vv in it.values():
                        _push_url(vv)

    for k in ("socials", "socialLinks", "social_links", "social", "links"):
        v = y_it.get(k)
        if v:
            social[k] = v

    for k in ("telegram", "instagram", "vk", "whatsapp", "youtube", "tiktok", "facebook", "ok", "rutube"):
        v = y_it.get(k)
        if v:
            social[k] = v

    return site_urls, social


def _dedup_preserve_order(items: list[str]) -> list[str]:
    out: list[str] = []
    seen = set()
    for x in items or []:
        if not isinstance(x, str):
            continue
        x = x.strip()
        if not x or x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def _merge_dict(a: dict | None, b: dict | None) -> dict:
    res: dict = {}
    if isinstance(a, dict):
        res.update(a)
    if isinstance(b, dict):
        for k, v in b.items():
            if k in res and res[k] not in (None, "", [], {}) and v in (None, "", [], {}):
                continue
            res[k] = v
    return res


async def _enrich_place_site(
    *,
    sb,
    job_id: str,
    place_key: str,
    candidate_urls: list[str],
) -> dict:
    urls = _dedup_preserve_order(candidate_urls)[:2]
    if not urls:
        return {"ok": True, "skipped": "no_urls"}

    extracted_all: list[dict] = []
    merged_social: dict = {}
    merged_sites: list[str] = []

    for url in urls:
        try:
            data = await fetch_and_extract_website_data(url)
        except Exception as e:
            data = {"ok": False, "error": f"{type(e).__name__}: {e}", "url": url}

        extracted_all.append(data if isinstance(data, dict) else {"ok": True, "data": data, "url": url})

        if isinstance(data, dict):
            s = data.get("social_links") or data.get("socials") or {}
            if isinstance(s, dict):
                merged_social = _merge_dict(merged_social, s)

            su = data.get("site_urls") or data.get("websites") or []
            if isinstance(su, list):
                merged_sites.extend([str(x) for x in su if x])
            elif isinstance(su, str):
                merged_sites.append(su)

    merged_sites = _dedup_preserve_order(urls + merged_sites)

    try:
        _insert_raw_items_compat(
            job_id=str(job_id),
            place_key=str(place_key),
            source="site_extract",
            city=None,
            queries=None,
            actor_id="site_extract",
            run_id=f"site_{int(time.time())}",
            items=extracted_all,
        )
    except Exception:
        pass

    payload = {
        "job_id": str(job_id),
        "place_key": str(place_key),
        "site_urls": merged_sites,
        "social_links": merged_social,
    }

    try:
        sb.table("mi_places").upsert(payload, on_conflict="job_id,place_key").execute()
    except Exception:
        payload.pop("site_data", None)
        sb.table("mi_places").upsert(payload, on_conflict="job_id,place_key").execute()

    return {"ok": True, "urls": urls, "extracted_count": len(extracted_all)}


def _collect_place_internal(
    *,
    sb,
    job_id: str,
    place_key: str,
    actor_id: str,
    max_items: int = 6,
    actor_input_yandex_override: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    run_id: str | None = None
    yandex_query: str | None = None

    resp = (
        sb.table("mi_raw_items")
        .select("item, city")
        .eq("job_id", str(job_id))
        .eq("place_key", str(place_key))
        .eq("source", "apify_2gis")
        .limit(1)
        .execute()
    )
    if not resp.data:
        raise HTTPException(status_code=404, detail="2GIS item not found for job_id + place_key")

    base_item = resp.data[0].get("item") or {}
    city = (resp.data[0].get("city") or base_item.get("city") or "").strip().lower() or "unknown"
    title = (base_item.get("title") or base_item.get("name") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="2GIS item has no title/name to form yandex query")

    yandex_query = f"{title} {city}".strip()
    actor_input: Dict[str, Any] = {
        "enableGlobalDataset": False,
        "language": "RU",
        "maxItems": int(max_items),
        "query": yandex_query,
    }
    if isinstance(actor_input_yandex_override, dict) and actor_input_yandex_override:
        actor_input.update(actor_input_yandex_override)

    try:
        run_id, y_items = run_actor_fire_and_poll_get_dataset_items(actor_id=actor_id, actor_input=actor_input)
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

    if not isinstance(y_items, list):
        y_items = [] if y_items is None else [y_items]

    def _y_id(it: Dict[str, Any]) -> str:
        url = (it.get("url") or "").strip()
        m = re.search(r"/org/(\d+)/", url)
        if m:
            return m.group(1)
        return url or (it.get("title") or "unknown")

    for it in y_items:
        if isinstance(it, dict) and not it.get("id"):
            it["id"] = _y_id(it)

    _insert_raw_items_compat(
        job_id=str(job_id),
        place_key=str(place_key),
        source="apify_yandex",
        city=city,
        queries=[yandex_query],
        actor_id=actor_id,
        run_id=run_id,
        items=y_items,
    )

    base_addr = (
        base_item.get("address")
        or base_item.get("addressText")
        or (base_item.get("address") or {}).get("address")
        or ""
    )
    best_y = _best_match_yandex(title, str(base_addr or ""), y_items or [])

    site_urls, social_links = _extract_site_urls_and_socials(best_y or {})

    payload_min = {
        "job_id": str(job_id),
        "place_key": str(place_key),
        "best_2gis": base_item,
        "best_yandex": best_y,
        "site_urls": site_urls,
        "social_links": social_links,
    }

    sb.table("mi_places").upsert(payload_min, on_conflict="job_id,place_key").execute()

    try:
        sb.table("mi_job_places").upsert(
            {"job_id": str(job_id), "place_key": str(place_key), "selected": True, "source": "auto"},
            on_conflict="job_id,place_key",
        ).execute()
    except Exception:
        pass

    return {
        "job_id": str(job_id),
        "place_key": str(place_key),
        "yandex_query": yandex_query,
        "apify": {"actor_id": actor_id, "run_id": run_id, "items_count": len(y_items or [])},
        "best_yandex": best_y,
        "site_urls": site_urls,
        "social_links": social_links,
    }


async def _orchestrate_full_job(
    *,
    job_id: str,
    tg_user_id: int,
    city: str,
    niche: str,
    limit: int | None,
    yandex_max_items: int,
    yandex_actor_id: str,
    actor_id_2gis: str,
    actor_input_2gis_override: Dict[str, Any] | None = None,
    actor_input_yandex_override: Dict[str, Any] | None = None,  # ✅ server-2 fix
    max_places: int | None = None,
    max_seconds: int | None = None,
    yandex_retries: int = 1,
    sleep_ms: int = 0,
):
    sb = get_supabase()

    if not _job_state_try_claim(sb, job_id):
        return

    started_ts = time.time()
    try:
        _job_state_upsert(
            sb,
            job_id,
            status="running",
            total=0,
            done=0,
            failed=0,
            meta={
                "tg_user_id": tg_user_id,
                "city": city,
                "niche": niche,
                "limit": limit,
                "yandex_maxItems": yandex_max_items,
                "yandex_actor_id": yandex_actor_id,
                "actor_id_2gis": actor_id_2gis,
                "phase": "2gis",
            },
        )

        actor_input: Dict[str, Any] = {
            "domain": "2gis.ru",
            "enableGlobalDataset": True,
            "filterRating": "rating_rating_excellent",
            "locationQuery": city,
            "query": [niche],
             "includeContacts": True,
        }
        if actor_input_2gis_override:
            actor_input.update(actor_input_2gis_override)
        if limit is not None:
            actor_input["maxItems"] = int(limit)

        run_id_2gis = f"sync_{int(time.time())}"
        items_2gis = run_actor_sync_get_dataset_items(actor_id=actor_id_2gis, actor_input=actor_input)

        _insert_raw_items_compat(
            job_id=job_id,
            source="apify_2gis",
            city=city.lower(),
            queries=[niche],
            actor_id=actor_id_2gis,
            run_id=run_id_2gis,
            items=items_2gis,
        )

        place_keys = [str(it["id"]) for it in (items_2gis or []) if isinstance(it, dict) and it.get("id") is not None]
        place_keys = list(dict.fromkeys([pk for pk in place_keys if pk]))
        if max_places is not None and max_places > 0 and len(place_keys) > max_places:
            place_keys = place_keys[:max_places]

        try:
            if place_keys:
                rows = [{"job_id": job_id, "place_key": pk, "selected": True, "source": "auto"} for pk in place_keys]
                sb.table("mi_job_places").upsert(rows, on_conflict="job_id,place_key").execute()
        except Exception:
            pass

        _job_state_upsert(
            sb,
            job_id,
            total=len(place_keys),
            meta={
                "tg_user_id": tg_user_id,
                "city": city,
                "niche": niche,
                "limit": limit,
                "yandex_maxItems": yandex_max_items,
                "yandex_actor_id": yandex_actor_id,
                "actor_id_2gis": actor_id_2gis,
                "phase": "yandex",
                "place_keys_total": len(place_keys),
            },
        )

        processed = 0
        failed = 0

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

        stopped_reason = None
        for pk in place_keys:
            if pk in existing_keys:
                processed += 1
                _job_state_upsert(sb, job_id, done=processed, failed=failed)
                continue

            y_exist = (
                sb.table("mi_raw_items")
                .select("id")
                .eq("job_id", job_id)
                .eq("place_key", pk)
                .eq("source", "apify_yandex")
                .limit(1)
                .execute()
            )
            if getattr(y_exist, "data", None):
                processed += 1
                _job_state_upsert(sb, job_id, done=processed, failed=failed)
                continue

            if max_seconds is not None and max_seconds > 0 and (time.time() - started_ts) >= max_seconds:
                stopped_reason = "max_seconds_reached"
                break

            last_err = None
            result: Dict[str, Any] | None = None

            for attempt in range(max(0, yandex_retries) + 1):
                try:
                    result = _collect_place_internal(
                        sb=sb,
                        job_id=job_id,
                        place_key=pk,
                        actor_id=yandex_actor_id,
                        max_items=yandex_max_items,
                        actor_input_yandex_override=actor_input_yandex_override,
                    )
                    last_err = None
                    break
                except HTTPException as e:
                    last_err = {"status_code": e.status_code, "detail": e.detail}
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

            if last_err is None and isinstance(result, dict):
                try:
                    site_candidates = result.get("site_urls") or []
                    if site_candidates:
                        await _enrich_place_site(
                            sb=sb,
                            job_id=str(job_id),
                            place_key=str(pk),
                            candidate_urls=site_candidates,
                        )
                except Exception:
                    pass
            if last_err is not None:
                failed += 1

            processed += 1
            _job_state_upsert(
                sb,
                job_id,
                done=processed,
                failed=failed,
                meta={
                    "phase": "yandex",
                    "stopped_reason": stopped_reason,
                    "elapsed_seconds": round(time.time() - started_ts, 2),
                    "place_key": pk,
                    "last_error": last_err,
                },
            )

            if sleep_ms and sleep_ms > 0:
                time.sleep(sleep_ms / 1000.0)

        status = "done" if failed == 0 and stopped_reason is None else ("failed" if stopped_reason is None else "done")
        _job_state_upsert(
            sb,
            job_id,
            status=status,
            meta={
                "phase": "done",
                "stopped_reason": stopped_reason,
                "elapsed_seconds": round(time.time() - started_ts, 2),
                "failed_count": failed,
            },
        )
    except Exception as e:
        _job_state_upsert(
            sb,
            job_id,
            status="failed",
            meta={
                "phase": "failed",
                "error": f"{type(e).__name__}: {e}",
                "elapsed_seconds": round(time.time() - started_ts, 2),
            },
        )
        raise


@router.post("/run_full_job")
async def run_full_job(payload: Dict[str, Any] = Body(...)):
    """
    Server-2 mode (QUEUE):
    - creates mi_jobs record
    - creates/updates mi_job_state (queued)
    - ENQUEUE into mi_tasks (status=queued), worker executes _orchestrate_full_job
    """
    tg_user_id = payload.get("tg_user_id")
    city = (payload.get("city") or "").strip()
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

    yandex_max_items = payload.get("yandex_maxItems") or payload.get("maxItems") or 6
    try:
        yandex_max_items = int(yandex_max_items)
    except Exception:
        raise HTTPException(status_code=400, detail="yandex_maxItems must be an integer")

    max_places = payload.get("max_places") or payload.get("maxPlaces")
    max_seconds = payload.get("max_seconds") or payload.get("maxSeconds")
    yandex_retries = payload.get("yandex_retries")
    if yandex_retries is None:
        yandex_retries = payload.get("yandexRetries")
    if yandex_retries is None:
        yandex_retries = 1
    sleep_ms = payload.get("sleep_ms") or payload.get("sleepMs") or 0

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

    actor_id_2gis = (payload.get("actor_id_2gis") or os.getenv("APIFY_2GIS_ACTOR_ID") or "m_mamaev~2gis-places-scraper").strip()
    actor_id_yandex = (payload.get("actor_id_yandex") or os.getenv("APIFY_YANDEX_ACTOR_ID") or "m_mamaev~yandex-maps-places-scraper").strip()

    actor_input_2gis_override = payload.get("actor_input_2gis") if isinstance(payload.get("actor_input_2gis"), dict) else None

    actor_input_yandex_override = payload.get("actor_input_yandex_override")
    if actor_input_yandex_override is None and isinstance(payload.get("actor_input_yandex"), dict):
        actor_input_yandex_override = payload.get("actor_input_yandex")
    if actor_input_yandex_override is not None and not isinstance(actor_input_yandex_override, dict):
        raise HTTPException(status_code=400, detail="actor_input_yandex_override must be an object")

    try:
        job_id = create_job(tg_user_id=tg_user_id, city=city.lower(), query=niche, queries=[niche])
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": "job_create_failed", "message": str(e)})

    sb = get_supabase()

    _job_state_upsert(
        sb,
        job_id,
        status="queued",
        total=0,
        done=0,
        failed=0,
        meta={"tg_user_id": tg_user_id, "city": city, "niche": niche, "limit": limit, "phase": "queued"},
    )

    task_payload = {
        "job_id": job_id,
        "tg_user_id": tg_user_id,
        "city": city,
        "niche": niche,
        "limit": limit,
        "yandex_max_items": yandex_max_items,
        "yandex_actor_id": actor_id_yandex,
        "actor_id_2gis": actor_id_2gis,
        "actor_input_2gis_override": actor_input_2gis_override,
        "actor_input_yandex_override": actor_input_yandex_override,
        "max_places": max_places,
        "max_seconds": max_seconds,
        "yandex_retries": yandex_retries,
        "sleep_ms": sleep_ms,
    }

    try:
        sb.table("mi_tasks").insert(
            {
                "job_id": job_id,
                "task_type": "run_full_job",
                "payload": task_payload,
                "status": "queued",
                "attempts": 0,
            }
        ).execute()
    except Exception as e:
        remember = f"{type(e).__name__}: {e}"
        _job_state_upsert(sb, job_id, status="failed", meta={"phase": "enqueue_failed", "error": remember})
        raise HTTPException(status_code=500, detail={"error": "enqueue_failed", "message": remember})

    return {"ok": True, "job_id": job_id, "state": "queued"}


@router.post("/collect_place")
async def collect_place(payload: Dict[str, Any] = Body(...)):
    job_id = payload.get("job_id")
    place_key = payload.get("place_key")
    if not job_id or not place_key:
        raise HTTPException(status_code=400, detail="job_id and place_key are required")

    actor_id = (payload.get("actor_id") or "m_mamaev~yandex-maps-places-scraper").strip()
    max_items = int(payload.get("maxItems") or 6)

    actor_input_yandex_override = payload.get("actor_input_yandex_override")
    if actor_input_yandex_override is None and isinstance(payload.get("actor_input_yandex"), dict):
        actor_input_yandex_override = payload.get("actor_input_yandex")

    sb = get_supabase()

    result = _collect_place_internal(
        sb=sb,
        job_id=str(job_id),
        place_key=str(place_key),
        actor_id=actor_id,
        max_items=max_items,
        actor_input_yandex_override=actor_input_yandex_override if isinstance(actor_input_yandex_override, dict) else None,
    )

    try:
        site_candidates = result.get("site_urls") or []
        await _enrich_place_site(sb=sb, job_id=str(job_id), place_key=str(place_key), candidate_urls=site_candidates)
    except Exception:
        pass

    return {"ok": True, **result}
