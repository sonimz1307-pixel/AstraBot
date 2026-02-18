import os
import re
import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter, Header, HTTPException, Body, Query

from db_supabase import supabase as sb

router = APIRouter()
LOG = logging.getLogger("uvicorn.error")


def _require_admin(x_admin_token: Optional[str]) -> None:
    expected = os.getenv("ADMIN_TOKEN")
    if not expected:
        raise HTTPException(status_code=500, detail="ADMIN_TOKEN is not configured")
    if not x_admin_token or x_admin_token != expected:
        raise HTTPException(status_code=403, detail="forbidden")


def _norm_tg(url_or_handle: str) -> Optional[str]:
    if not url_or_handle:
        return None
    s = str(url_or_handle).strip()

    m = re.match(r"^@([A-Za-z0-9_]{4,})$", s)
    if m:
        return f"https://t.me/{m.group(1)}"

    m = re.search(r"(?:https?://)?(?:www\.)?(t\.me|telegram\.me)/([A-Za-z0-9_]{4,})", s)
    if m:
        return f"https://t.me/{m.group(2)}"

    return None


def _norm_ig(url: str) -> Optional[str]:
    if not url:
        return None
    s = str(url).strip()
    m = re.search(r"(?:https?://)?(?:www\.)?(instagram\.com|instagr\.am)/([A-Za-z0-9_.]+)", s)
    if not m:
        return None
    handle = m.group(2).strip("/")
    handle = handle.split("/")[0]
    if not handle:
        return None
    return f"https://instagram.com/{handle}"


def _extract_from_any(obj: Any) -> List[str]:
    out: List[str] = []
    if obj is None:
        return out
    if isinstance(obj, str):
        out.append(obj)
        return out
    if isinstance(obj, dict):
        for k in ("url", "href", "link", "readable", "value"):
            v = obj.get(k)
            if isinstance(v, str) and v:
                out.append(v)
        for v in obj.values():
            out.extend(_extract_from_any(v))
        return out
    if isinstance(obj, list):
        for it in obj:
            out.extend(_extract_from_any(it))
        return out
    return out


async def _fetch_taplink_links(url: str) -> Tuple[List[str], Optional[Dict[str, Any]]]:
    if not url:
        return [], None
    u = str(url).strip()
    if "taplink" not in u:
        return [], None

    headers = {"User-Agent": "Mozilla/5.0 (compatible; AstraBot/1.0)"}
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
            r = await client.get(u, headers=headers)
            html = r.text or ""
            found: List[str] = []
            found += re.findall(r"(https?://t\.me/[A-Za-z0-9_]{4,})", html)
            found += re.findall(r"(https?://(?:www\.)?instagram\.com/[A-Za-z0-9_.]+)", html)
            meta = {"url": u, "status": r.status_code, "len": len(html)}
            # dedup
            found = list(dict.fromkeys(found))
            return found, meta
    except Exception as e:
        return [], {"url": u, "error": f"{type(e).__name__}: {e}"}


@router.get("/jobs")
def list_jobs(
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
    limit: int = Query(default=50, ge=1, le=200),
):
    _require_admin(x_admin_token)
    if sb is None:
        raise HTTPException(status_code=500, detail="Supabase client is not configured")

    r = (
        sb.table("mi_job_state")
        .select("job_id,status,total,done,failed,updated_at,meta")
        .order("updated_at", desc=True)
        .limit(limit)
        .execute()
    )
    return {"ok": True, "jobs": r.data or []}


@router.get("/job/{job_id}")
def job_detail(
    job_id: str,
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
    include_raw: bool = Query(default=False),
    raw_limit: int = Query(default=200, ge=1, le=2000),
):
    _require_admin(x_admin_token)
    if sb is None:
        raise HTTPException(status_code=500, detail="Supabase client is not configured")

    st = (
        sb.table("mi_job_state")
        .select("job_id,status,total,done,failed,updated_at,meta")
        .eq("job_id", job_id)
        .limit(1)
        .execute()
    ).data
    state = (st[0] if st else None)

    # tg_links/ig_links may not exist yet — if so, Supabase will error. We fallback to base select.
    try:
        places = (
            sb.table("mi_places")
            .select("job_id,place_key,site_urls,social_links,tg_links,ig_links,created_at")
            .eq("job_id", job_id)
            .order("created_at", desc=False)
            .limit(2000)
            .execute()
        ).data or []
    except Exception:
        places = (
            sb.table("mi_places")
            .select("job_id,place_key,site_urls,social_links,created_at")
            .eq("job_id", job_id)
            .order("created_at", desc=False)
            .limit(2000)
            .execute()
        ).data or []

    raw_items = []
    if include_raw:
        raw_items = (
            sb.table("mi_raw_items")
            .select("id,job_id,source,place_key,run_id,actor_id,created_at,item")
            .eq("job_id", job_id)
            .order("created_at", desc=False)
            .limit(raw_limit)
            .execute()
        ).data or []

    return {"ok": True, "state": state, "places": places, "raw_items": raw_items}


@router.post("/run_job")
async def run_job(
    payload: Dict[str, Any] = Body(...),
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    _require_admin(x_admin_token)

    port = os.getenv("PORT", "10000")
    base_url = os.getenv("SELF_BASE_URL", f"http://127.0.0.1:{port}")
    url = f"{base_url}/api/leads/run_full_job"

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, json=payload)
        if r.status_code >= 400:
            raise HTTPException(status_code=r.status_code, detail=r.text)
        return r.json()


@router.post("/extract_socials")
async def extract_socials(
    job_id: str = Body(..., embed=True),
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
    max_places: int = Body(default=2000, embed=True),
    taplink_fetch: bool = Body(default=True, embed=True),
):
    """Extract canonical Telegram/Instagram links for all places in a job."""
    _require_admin(x_admin_token)
    if sb is None:
        raise HTTPException(status_code=500, detail="Supabase client is not configured")

    places = (
        sb.table("mi_places")
        .select("job_id,place_key,site_urls,social_links")
        .eq("job_id", job_id)
        .limit(max_places)
        .execute()
    ).data or []

    updated = 0
    errors: List[Dict[str, Any]] = []

    for p in places:
        place_key = str(p.get("place_key") or "")
        site_urls = p.get("site_urls")
        social_links = p.get("social_links")

        candidates: List[str] = []
        candidates += _extract_from_any(site_urls)
        candidates += _extract_from_any(social_links)

        if taplink_fetch:
            for u in list(dict.fromkeys(candidates)):
                if "taplink" in str(u):
                    extra, _meta = await _fetch_taplink_links(str(u))
                    candidates += extra

        tg_set: List[str] = []
        ig_set: List[str] = []
        for c in candidates:
            tg = _norm_tg(c)
            if tg and tg not in tg_set:
                tg_set.append(tg)
            ig = _norm_ig(c)
            if ig and ig not in ig_set:
                ig_set.append(ig)

        if not tg_set and not ig_set:
            continue

        # Try updating tg_links/ig_links
        try:
            sb.table("mi_places").upsert(
                {
                    "job_id": job_id,
                    "place_key": place_key,
                    "tg_links": tg_set,
                    "ig_links": ig_set,
                },
                on_conflict="job_id,place_key",
            ).execute()
            updated += 1
            continue
        except Exception as e:
            # Fallback: merge into social_links only
            try:
                merged = []
                merged += _extract_from_any(social_links)
                merged += tg_set + ig_set
                merged = list(dict.fromkeys([m for m in merged if isinstance(m, str) and m]))

                sb.table("mi_places").upsert(
                    {
                        "job_id": job_id,
                        "place_key": place_key,
                        "social_links": merged,
                    },
                    on_conflict="job_id,place_key",
                ).execute()
                updated += 1
                errors.append(
                    {"place_key": place_key, "warn": f"tg_links/ig_links not written: {type(e).__name__}: {e}"}
                )
            except Exception as e2:
                errors.append({"place_key": place_key, "error": f"{type(e2).__name__}: {e2}"})

    return {"ok": True, "job_id": job_id, "updated": updated, "errors": errors}
