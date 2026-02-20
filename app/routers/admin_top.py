import os
import re
import time
import logging
import hashlib
import math
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Header, HTTPException, Body, Query

from db_supabase import supabase as sb

# Reuse existing extraction services (already used in leads.py pipeline)
from app.services.socials_extract import fetch_and_extract_website_data
from app.services.extract_utils import clean_url, force_https_if_bare, detect_social
from app.services.mi_storage import insert_raw_items

router = APIRouter()
LOG = logging.getLogger("uvicorn.error")


# -----------------------------
# Auth
# -----------------------------
def _require_admin(x_admin_token: Optional[str]) -> None:
    expected = os.getenv("ADMIN_TOKEN")
    if not expected:
        raise HTTPException(status_code=500, detail="ADMIN_TOKEN is not configured")
    if not x_admin_token or x_admin_token != expected:
        raise HTTPException(status_code=403, detail="forbidden")


# -----------------------------
# Normalizers (canonical tg/ig)
# -----------------------------
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


# -----------------------------
# DB helpers
# -----------------------------
def _get_job_city(job_id: str) -> str:
    if sb is None:
        return "unknown"
    try:
        r = sb.table("mi_jobs").select("city").eq("id", job_id).limit(1).execute().data or []
        city = (r[0] or {}).get("city") if r else None
        return (city or "unknown").strip().lower()
    except Exception:
        return "unknown"


def _load_yandex_raw_candidates(job_id: str, place_key: str) -> List[str]:
    """
    Pull extra candidate links from mi_raw_items(apify_yandex) for this place.
    Important: mi_places.social_links can be empty, while Yandex raw often includes taplink/urls/socialLinks.
    """
    if sb is None:
        return []
    try:
        rows = (
            sb.table("mi_raw_items")
            .select("item")
            .eq("job_id", job_id)
            .eq("place_key", place_key)
            .eq("source", "apify_yandex")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        ).data or []
        if not rows:
            return []
        item = (rows[0] or {}).get("item")

        # item can be list(dataset items) or dict
        if isinstance(item, list) and item:
            first = item[0]
        else:
            first = item

        cands: List[str] = []
        if isinstance(first, dict):
            for key in ("website", "url", "yandexUrl"):
                v = first.get(key)
                if isinstance(v, str) and v:
                    cands.append(v)

            urls = first.get("urls")
            if isinstance(urls, list):
                for u in urls:
                    if isinstance(u, str) and u:
                        cands.append(u)

            sl = first.get("socialLinks")
            if isinstance(sl, list):
                for it in sl:
                    if isinstance(it, dict):
                        u = it.get("url")
                        r = it.get("readable")
                        if isinstance(u, str) and u:
                            cands.append(u)
                        if isinstance(r, str) and r:
                            cands.append(r)

        # de-dup
        return list(dict.fromkeys([x.strip() for x in cands if isinstance(x, str) and x.strip()]))
    except Exception:
        return []


def _site_extract_item_id(place_key: str, url: str) -> str:
    h = hashlib.sha1((place_key + "|" + (url or "")).encode("utf-8")).hexdigest()[:16]
    return f"site:{h}"


def _merge_social_links(existing: Any, socials: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Keep mi_places.social_links as dict:
      {
        "telegram": ["https://t.me/..."],
        "instagram": ["https://instagram.com/..."],
        "other": [...]
      }

    `socials` is a list of dicts from site_extract, like:
      {"url": "...", "platform": "vk|whatsapp|telegram|instagram", ...}
    """
    # start from existing
    if isinstance(existing, dict):
        out: Dict[str, Any] = dict(existing)
    elif isinstance(existing, list):
        # legacy: list of urls -> shove into "other"
        out = {"other": [str(x) for x in existing if isinstance(x, str) and x]}
    else:
        out = {}

    out.setdefault("telegram", [])
    out.setdefault("instagram", [])
    out.setdefault("other", [])

    def _push(bucket: str, url: str) -> None:
        nu = _norm_url_any(url)
        if not nu:
            return
        arr = out.get(bucket)
        if not isinstance(arr, list):
            arr = []
            out[bucket] = arr
        if nu not in arr:
            arr.append(nu)

    for s in socials or []:
        if not isinstance(s, dict):
            continue
        url = s.get("url")
        if not isinstance(url, str) or not url.strip():
            continue

        platform = (s.get("platform") or "").lower().strip()

        # prefer explicit platform tag
        if platform in ("telegram", "tg"):
            _push("telegram", url)
            continue
        if platform in ("instagram", "ig"):
            _push("instagram", url)
            continue

        # infer by url
        u0 = url.lower()
        if "t.me/" in u0 or "telegram.me/" in u0:
            _push("telegram", url)
        elif "instagram.com/" in u0:
            _push("instagram", url)
        else:
            _push("other", url)

    return out


def _norm_url_any(u: str) -> Optional[str]:
    if not isinstance(u, str):
        return None
    u = u.strip()
    if not u:
        return None
    if u.startswith("//"):
        u = "https:" + u
    elif u.startswith("@"):
        u = "https://t.me/" + u[1:]
    elif not u.startswith(("http://", "https://")):
        u = force_https_if_bare(u)
    u = clean_url(u) or ""
    u = u.strip()
    if u.endswith("/"):
        u = u[:-1]
    return u or None
def _guess_name(best: Any) -> str:
    if not isinstance(best, dict):
        return ""
    for k in ("name", "title", "org_name", "caption", "displayName", "display_name"):
        v = best.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    # sometimes nested
    for path in (("organization","name"), ("company","name"), ("data","name"), ("data","title")):
        cur = best
        ok = True
        for pk in path:
            if isinstance(cur, dict) and pk in cur:
                cur = cur[pk]
            else:
                ok = False
                break
        if ok and isinstance(cur, str) and cur.strip():
            return cur.strip()
    return ""


def _guess_address(best: Any) -> str:
    if not isinstance(best, dict):
        return ""
    for k in ("address_name", "address", "full_address", "fullAddress", "addressName"):
        v = best.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    for path in (("address","full"), ("address","text"), ("data","address")):
        cur = best
        ok = True
        for pk in path:
            if isinstance(cur, dict) and pk in cur:
                cur = cur[pk]
            else:
                ok = False
                break
        if ok and isinstance(cur, str) and cur.strip():
            return cur.strip()
    return ""


def _build_canonical(
    place_row: Dict[str, Any],
    social_links: Dict[str, Any],
    phones: List[str],
    emails: List[str],
    extracted_items: List[Dict[str, Any]],
) -> Dict[str, Any]:
    best2 = place_row.get("best_2gis") or {}
    besty = place_row.get("best_yandex") or {}

    name = _guess_name(best2) or _guess_name(besty) or ""
    address = _guess_address(best2) or _guess_address(besty) or ""

    def _is_bad_website(u: str) -> bool:
        # exclude directory/landing pages that are not the company's website
        if re.search(r"(?:^|//)(?:www\.)?yandex\.(ru|com|eu)/maps", u):
            return True
        if re.search(r"(?:^|//)(?:www\.)?2gis\.(ru|com)", u):
            return True
        return False

    def _norm_good_website(u: Any) -> Optional[str]:
        if not isinstance(u, str):
            return None
        nu = _norm_url_any(u)
        if not nu:
            return None
        if _is_bad_website(nu):
            return None
        return nu

    # website preference: first ok extracted -> else from site_urls
    website = ""
    for it in extracted_items:
        if isinstance(it, dict) and it.get("ok"):
            w = it.get("website")
            nw = _norm_good_website(w)
            if nw:
                website = nw
                break
    if not website:
        for w in _extract_from_any(place_row.get("site_urls")):
            nw = _norm_good_website(w)
            if nw:
                website = nw
                break

    tg: List[str] = []
    ig: List[str] = []
    vk: List[str] = []
    whatsapp: List[str] = []
    other: List[str] = []

    for u in _extract_from_any(social_links.get("telegram")):
        nu = _norm_url_any(u)
        if nu and nu not in tg:
            tg.append(nu)
    for u in _extract_from_any(social_links.get("instagram")):
        nu = _norm_url_any(u)
        if nu and nu not in ig:
            ig.append(nu)

    # other urls: classify
    for u in _extract_from_any(social_links.get("other")):
        nu = _norm_url_any(u)
        if not nu:
            continue
        if "t.me/" in nu:
            if nu not in tg:
                tg.append(nu)
        elif "instagram.com/" in nu:
            if nu not in ig:
                ig.append(nu)
        elif "vk.com/" in nu or "vkontakte.ru/" in nu:
            if nu not in vk:
                vk.append(nu)
        elif "wa.me/" in nu or "whatsapp.com/" in nu:
            if nu not in whatsapp:
                whatsapp.append(nu)
        else:
            if nu not in other:
                other.append(nu)

    def _norm_phone(ph: str) -> Optional[str]:
        s = (ph or "").strip()
        if not s:
            return None
        # keep only + and digits
        s = re.sub(r"[^0-9+]", "", s)
        if not s.startswith("+"):
            # allow 7XXXXXXXXXX -> +7XXXXXXXXXX
            if len(s) == 11 and s.startswith("7"):
                s = "+" + s
        # accept RU style +7XXXXXXXXXX
        if s.startswith("+7") and len(s) == 12:
            return s
        # accept generic E.164-ish (basic guard)
        if s.startswith("+") and 10 <= len(s) <= 16:
            return s
        return None

    # normalize phones/emails (dedupe, keep non-empty)
    ph_out: List[str] = []
    for ph in phones or []:
        if isinstance(ph, str):
            pph = _norm_phone(ph)
            if pph and pph not in ph_out:
                ph_out.append(pph)
            if len(ph_out) >= 5:
                break

    em_out: List[str] = []
    for em in emails or []:
        if isinstance(em, str):
            eem = em.strip().lower()
            if eem and eem not in em_out:
                em_out.append(eem)

    sources: Dict[str, Any] = {}
    if extracted_items:
        sources["site_extract"] = True
    if isinstance(best2, dict) and best2:
        sources["best_2gis"] = True
    if isinstance(besty, dict) and besty:
        sources["best_yandex"] = True

    
    # --- ratings / reviews (for analytics) ---
    def _as_float(v: Any) -> Optional[float]:
        try:
            if v is None:
                return None
            if isinstance(v, (int, float)):
                return float(v)
            s = str(v).strip().replace(",", ".")
            return float(s) if s else None
        except Exception:
            return None

    def _as_int(v: Any) -> Optional[int]:
        try:
            if v is None:
                return None
            if isinstance(v, bool):
                return None
            if isinstance(v, int):
                return int(v)
            if isinstance(v, float):
                return int(v)
            s = str(v).strip()
            # extract digits
            m = re.search(r"-?\d+", s)
            return int(m.group(0)) if m else None
        except Exception:
            return None

    def _pick(d: Dict[str, Any], keys: List[str]) -> Any:
        for k in keys:
            if k in d and d.get(k) is not None:
                return d.get(k)
        return None

    def _extract_one_rating(obj: Dict[str, Any], src: str) -> Optional[Dict[str, Any]]:
        if not isinstance(obj, dict) or not obj:
            return None

        # Common possible keys across different datasets/actors
        rating_val = _as_float(_pick(obj, ["rating", "ratingValue", "rating_value", "stars", "score"]))
        reviews_cnt = _as_int(_pick(obj, ["reviewsCount", "reviewCount", "reviews_count", "reviews", "reviewsTotal", "reviews_total", "reviewsQty", "reviews_qty"]))

        # Some actors put rating inside nested objects
        if rating_val is None:
            nested = obj.get("rating") if isinstance(obj.get("rating"), dict) else None
            if isinstance(nested, dict):
                rating_val = _as_float(_pick(nested, ["value", "rating", "score"]))
                if reviews_cnt is None:
                    reviews_cnt = _as_int(_pick(nested, ["count", "reviewsCount", "reviews"]))

        if rating_val is None and reviews_cnt is None:
            return None

        out = {"value": rating_val, "reviews": reviews_cnt}
        # Keep source in the object for transparency
        out["source"] = src
        return out

    ratings: Dict[str, Any] = {}
    r2 = _extract_one_rating(best2, "2gis")
    ry = _extract_one_rating(besty, "yandex")
    if r2:
        ratings["2gis"] = r2
    if ry:
        ratings["yandex"] = ry

    # Compute a simple comparable strength score for sorting:
    # score = rating * log(1 + reviews). If reviews unknown -> rating only.
    def _strength(r: Dict[str, Any]) -> Optional[float]:
        try:
            val = r.get("value")
            if val is None:
                return None
            cnt = r.get("reviews")
            if cnt is None or cnt < 0:
                return float(val)
            return float(val) * math.log1p(float(cnt))
        except Exception:
            return None

    rating_summary: Dict[str, Any] = {}
    if ratings:
        scored = []
        for k, r in ratings.items():
            s = _strength(r)
            scored.append((k, s, r.get("reviews") or 0, r.get("value")))
        scored.sort(key=lambda x: ((x[1] is not None), x[1] or -1, x[2], x[3] or 0), reverse=True)
        best_key = scored[0][0]
        rating_summary = {
            "best_source": best_key,
            "best": ratings.get(best_key),
            "score": scored[0][1],
        }
canonical = {
        "name": name,
        "address": address,
        "website": website,
        "phones": ph_out,
        "emails": em_out,
        "ratings": ratings,
        "rating_summary": rating_summary,
        "tg": tg,
        "ig": ig,
        "vk": vk,
        "whatsapp": whatsapp,
        "other": other,
        "sources": sources,
        "ts": int(time.time()),
    }
    return canonical

# -----------------------------
# Admin API
# -----------------------------
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



@router.get("/job/{job_id}/places")
def job_places(
    job_id: str,
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
    limit: int = Query(default=2000, ge=1, le=5000),
    only_selected: bool = Query(default=False),
    search: Optional[str] = Query(default=None),
):
    """Return competitors list for selection UI (RU WebApp).
    Joins mi_places with mi_job_places.selected (selection layer).
    """
    _require_admin(x_admin_token)
    if sb is None:
        raise HTTPException(status_code=500, detail="Supabase client is not configured")

    # Load selections (may be empty for legacy jobs)
    sel_rows = (
        sb.table("mi_job_places")
        .select("place_key,selected,source,note")
        .eq("job_id", job_id)
        .limit(limit)
        .execute()
    ).data or []

    sel_map: Dict[str, Dict[str, Any]] = {}
    for r in sel_rows:
        pk = str((r or {}).get("place_key") or "").strip()
        if not pk:
            continue
        sel_map[pk] = {
            "selected": bool((r or {}).get("selected", True)),
            "source": (r or {}).get("source") or "auto",
            "note": (r or {}).get("note"),
        }

    # Load places
    places = (
        sb.table("mi_places")
        .select("place_key,best_2gis,best_yandex,site_urls,tg_links,ig_links,created_at")
        .eq("job_id", job_id)
        .order("created_at", desc=False)
        .limit(limit)
        .execute()
    ).data or []

    def _pick_title(best_2gis: Any, best_yandex: Any) -> str:
        for obj in (best_2gis, best_yandex):
            if isinstance(obj, dict):
                for k in ("title", "name", "companyName"):
                    v = obj.get(k)
                    if isinstance(v, str) and v.strip():
                        return v.strip()
        return ""

    def _pick_address(best_2gis: Any, best_yandex: Any) -> str:
        for obj in (best_2gis, best_yandex):
            if isinstance(obj, dict):
                for k in ("address", "addressText", "fullAddress", "address_name"):
                    v = obj.get(k)
                    if isinstance(v, str) and v.strip():
                        return v.strip()
                a = obj.get("address")
                if isinstance(a, dict):
                    v = a.get("address") or a.get("text")
                    if isinstance(v, str) and v.strip():
                        return v.strip()
        return ""

    out: List[Dict[str, Any]] = []
    s = (search or "").strip().lower()
    for p in places:
        pk = str((p or {}).get("place_key") or "").strip()
        if not pk:
            continue

        meta = sel_map.get(pk) or {"selected": True, "source": "auto", "note": None}
        if only_selected and not meta.get("selected", True):
            continue

        best_2gis = (p or {}).get("best_2gis")
        best_yandex = (p or {}).get("best_yandex")
        title = _pick_title(best_2gis, best_yandex)
        addr = _pick_address(best_2gis, best_yandex)

        if s:
            hay = f"{title} {addr} {pk}".lower()
            if s not in hay:
                continue

        # website/tg/ig (first link only for UI convenience)
        website = None
        site_urls = (p or {}).get("site_urls")
        if isinstance(site_urls, list) and site_urls:
            website = site_urls[0]
        elif isinstance(site_urls, dict):
            # if stored as dict with list inside
            for v in site_urls.values():
                if isinstance(v, list) and v:
                    website = v[0]
                    break

        tg = None
        tg_links = (p or {}).get("tg_links")
        if isinstance(tg_links, list) and tg_links:
            tg = tg_links[0]

        ig = None
        ig_links = (p or {}).get("ig_links")
        if isinstance(ig_links, list) and ig_links:
            ig = ig_links[0]

        out.append(
            {
                "place_key": pk,
                "selected": bool(meta.get("selected", True)),
                "source": meta.get("source") or "auto",
                "note": meta.get("note"),
                "title": title,
                "address": addr,
                "website": website,
                "tg": tg,
                "ig": ig,
            }
        )

    return {"ok": True, "job_id": job_id, "places": out, "selection_count": len(sel_rows)}


@router.post("/job/{job_id}/places/select")
def job_places_select(
    job_id: str,
    payload: Dict[str, Any] = Body(...),
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    """Persist selection in mi_job_places.
    Payload:
      { "place_keys": [...], "selected": true|false, "replace": true|false }
    If replace=true:
      - sets all rows in this job to opposite, then applies selected to provided place_keys.
    """
    _require_admin(x_admin_token)
    if sb is None:
        raise HTTPException(status_code=500, detail="Supabase client is not configured")

    place_keys = payload.get("place_keys") or payload.get("selected_place_keys") or []
    if not isinstance(place_keys, list):
        raise HTTPException(status_code=400, detail="place_keys must be a list")

    selected = payload.get("selected")
    if selected is None:
        selected = True
    selected = bool(selected)

    replace = bool(payload.get("replace") or False)

    # Normalize keys
    keys = list(dict.fromkeys([str(x).strip() for x in place_keys if str(x).strip()]))

    affected = 0
    if replace:
        # Set all to opposite first
        sb.table("mi_job_places").update({"selected": (not selected)}).eq("job_id", job_id).execute()

    if keys:
        rows = [{"job_id": job_id, "place_key": pk, "selected": selected, "source": "auto"} for pk in keys]
        sb.table("mi_job_places").upsert(rows, on_conflict="job_id,place_key").execute()
        affected = len(rows)

    return {"ok": True, "job_id": job_id, "affected": affected, "replace": replace, "selected": selected}


@router.post("/job/{job_id}/enrich_selected")
async def enrich_selected(
    job_id: str,
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
    max_urls_per_place: int = Body(default=5, embed=True),
    timeout_sec: float = Body(default=25.0, embed=True),
    write_raw: bool = Body(default=True, embed=True),
):
    """Run site/social enrichment ONLY for selected competitors (mi_job_places.selected=true)."""
    _require_admin(x_admin_token)
    if sb is None:
        raise HTTPException(status_code=500, detail="Supabase client is not configured")

    rows = (
        sb.table("mi_job_places")
        .select("place_key")
        .eq("job_id", job_id)
        .eq("selected", True)
        .limit(5000)
        .execute()
    ).data or []
    keys = [str((r or {}).get("place_key") or "").strip() for r in rows]
    keys = [k for k in keys if k]

    if not keys:
        return {"ok": True, "job_id": job_id, "updated": 0, "scanned_places": 0, "scanned_urls": 0, "raw_affected": 0, "errors": [], "note": "no selected places"}

    # Reuse existing logic by calling enrich_sites with place_keys filter
    return await enrich_sites(
        job_id=job_id,
        place_keys=keys,
        x_admin_token=x_admin_token,
        max_places=len(keys),
        max_urls_per_place=max_urls_per_place,
        timeout_sec=timeout_sec,
        write_raw=write_raw,
    )



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


@router.post("/enrich_sites")
async def enrich_sites(
    job_id: str = Body(..., embed=True),
    place_keys: Optional[List[str]] = Body(default=None, embed=True),
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
    max_places: int = Body(default=2000, embed=True),
    max_urls_per_place: int = Body(default=5, embed=True),
    timeout_sec: float = Body(default=25.0, embed=True),
    write_raw: bool = Body(default=True, embed=True),
):
    """
    Reuse existing parsers:
      - taplink_extract.py (via fetch_and_extract_website_data)
      - socials_extract.py (generic website extraction)

    For each place:
      candidates = mi_places.site_urls + mi_places.social_links + yandex raw urls/socialLinks
      For each candidate website (http/https):
        fetch_and_extract_website_data(url) -> socials/phones/emails
      Persist:
        - mi_raw_items (source=site_extract) if write_raw=True
        - mi_places.social_links merged
        - mi_places.tg_links/ig_links filled (canonical)
    """
    _require_admin(x_admin_token)
    if sb is None:
        raise HTTPException(status_code=500, detail="Supabase client is not configured")

    city = _get_job_city(job_id)

    q = (
        sb.table("mi_places")
        .select("job_id,place_key,site_urls,social_links,tg_links,ig_links,best_2gis,best_yandex,canonical")
        .eq("job_id", job_id)
    )
    if place_keys:
        q = q.in_("place_key", [str(x) for x in place_keys if x])
    places = q.limit(max_places).execute().data or []

    run_id = f"admin_site_extract_{int(time.time())}"

    updated = 0
    scanned_places = 0
    scanned_urls = 0
    raw_affected = 0
    errors: List[Dict[str, Any]] = []

    for p in places:
        place_key = str(p.get("place_key") or "").strip()
        if not place_key:
            continue

        candidates: List[str] = []
        candidates += _extract_from_any(p.get("site_urls"))
        candidates += _extract_from_any(p.get("social_links"))
        candidates += _load_yandex_raw_candidates(job_id, place_key)

        # keep only website-like URLs; normalize; de-dup; limit
        urls: List[str] = []
        seen = set()

        for c in candidates:
            if not isinstance(c, str):
                continue
            u = c.strip()
            if not u:
                continue

            # Support bare domains like "taplink.cc/xxx" or "example.com"
            if u.startswith("//"):
                u = "https:" + u
            elif not u.startswith(("http://", "https://")):
                u = force_https_if_bare(u)

            if not u.startswith(("http://", "https://")):
                continue

            # Drop tracking params, fragments, etc.
            u = clean_url(u)
            if not u:
                continue

            # Skip pure social links; we only fetch websites/landings
            try:
                if detect_social(u):
                    continue
            except Exception:
                pass

            # ignore yandex maps (captcha noise)
            if re.search(r"(?:^|//)(?:www\.)?yandex\.(ru|com|eu)/maps", u):
                continue

            if u not in seen:
                seen.add(u)
                urls.append(u)

            if len(urls) >= max_urls_per_place:
                break
        if not urls:
            # Still build canonical from directory cards (2GIS/Yandex) + existing stored links
            social_links_existing = p.get("social_links")
            if not isinstance(social_links_existing, dict):
                social_links_existing = {}
            canonical = _build_canonical(p, social_links_existing, [], [], [])
            try:
                sb.table("mi_places").upsert(
                    {
                        "job_id": job_id,
                        "place_key": place_key,
                        "canonical": canonical,
                    },
                    on_conflict="job_id,place_key",
                ).execute()
                updated += 1
            except Exception as e:
                errors.append({"place_key": place_key, "error": f"mi_places_canonical_only: {type(e).__name__}: {e}"})
            continue

        scanned_places += 1

        extracted_items: List[Dict[str, Any]] = []
        merged_socials: List[Dict[str, Any]] = []
        merged_phones: List[str] = []
        merged_emails: List[str] = []

        for u in urls:
            scanned_urls += 1
            data = await fetch_and_extract_website_data(u, timeout=float(timeout_sec))
            # normalize failure payloads too
            if not isinstance(data, dict):
                data = {"ok": False, "website": u, "error": "bad_response"}

            socials = data.get("socials") or []
            phones = data.get("phones") or []
            emails = data.get("emails") or []

            if isinstance(socials, list):
                for s in socials:
                    if isinstance(s, dict):
                        merged_socials.append(s)

            if isinstance(phones, list):
                for ph in phones:
                    if isinstance(ph, str) and ph and ph not in merged_phones:
                        merged_phones.append(ph)

            if isinstance(emails, list):
                for em in emails:
                    if isinstance(em, str) and em and em not in merged_emails:
                        merged_emails.append(em)

            extracted_items.append(
                {
                    "id": _site_extract_item_id(place_key, u),
                    "place_key": place_key,
                    "website": data.get("website") or u,
                    "ok": bool(data.get("ok")),
                    "source_type": data.get("source_type"),
                    "socials": socials,
                    "phones": phones,
                    "emails": emails,
                    "important_pages": data.get("important_pages") or [],
                    "error": data.get("error"),
                    "ts": int(time.time()),
                }
            )

        # Merge into social_links dict
        social_links_new = _merge_social_links(p.get("social_links"), merged_socials)
        tg_links = social_links_new.get("telegram") or []
        ig_links = social_links_new.get("instagram") or []

        # Build canonical merged card (post-enrichment)
        canonical = _build_canonical(p, social_links_new, merged_phones, merged_emails, extracted_items)


        # Persist raw (site_extract)
        if write_raw:
            try:
                r = insert_raw_items(
                    job_id=job_id,
                    place_key=place_key,
                    source="site_extract",
                    city=city,
                    queries=["site_extract"],
                    actor_id="site_extract",
                    run_id=run_id,
                    items=extracted_items,
                )
                raw_affected += int((r or {}).get("affected") or 0)
            except Exception as e:
                errors.append({"place_key": place_key, "error": f"raw_save: {type(e).__name__}: {e}"})

        # Persist mi_places update
        try:
            sb.table("mi_places").upsert(
                {
                    "job_id": job_id,
                    "place_key": place_key,
                    "social_links": social_links_new,
                    "tg_links": tg_links,
                    "ig_links": ig_links,
                    "canonical": canonical,
                },
                on_conflict="job_id,place_key",
            ).execute()
            updated += 1
        except Exception as e:
            errors.append({"place_key": place_key, "error": f"mi_places_upsert: {type(e).__name__}: {e}"})

    return {
        "ok": True,
        "job_id": job_id,
        "updated": updated,
        "scanned_places": scanned_places,
        "scanned_urls": scanned_urls,
        "raw_affected": raw_affected,
        "errors": errors,
    }
