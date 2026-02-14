from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from app.services.socials_extract import fetch_and_extract_website_data


def _norm_str(x: Any) -> str:
    return (x or "").strip()


def _pick_primary_website(branch_items: List[Dict[str, Any]]) -> str:
    sites = []
    for it in branch_items:
        w = _norm_str(it.get("website"))
        if w:
            sites.append(w)
    if not sites:
        return ""

    # Prefer non-taplink
    for s in sites:
        host = urlparse(s).netloc.lower()
        if "taplink" not in host and "linktr.ee" not in host and "mssg.me" not in host:
            return s
    return sites[0]


def _digital_score(socials: List[Dict[str, Any]], website_url: str) -> int:
    score = 0
    if website_url:
        score += 1

    platforms = {s.get("platform") for s in socials or []}
    for p in ("instagram", "vk", "telegram", "tiktok", "youtube", "whatsapp", "ok"):
        if p in platforms:
            score += 1
    return score


async def build_brand_model_from_yandex_items(
    yandex_items: List[Dict[str, Any]],
    enrich_websites: bool = True,
) -> Dict[str, Any]:

    items = yandex_items or []
    if not items:
        return {"ok": False, "error": "no_items"}

    titles = [
        _norm_str(it.get("title") or it.get("place_name") or it.get("name"))
        for it in items
    ]
    titles = [t for t in titles if t]
    brand_name = max(set(titles), key=titles.count) if titles else ""

    website = _pick_primary_website(items)

    branches = []
    ratings = []
    reviews = []

    for it in items:
        b = {
            "source": "yandex",
            "name": _norm_str(it.get("title") or it.get("place_name") or it.get("name")),
            "address": _norm_str(it.get("address")),
            "phone": _norm_str(it.get("phone")),
            "rating": it.get("rating") or it.get("totalScore"),
            "reviews_count": it.get("reviews") or it.get("reviewsCount"),
            "website": _norm_str(it.get("website")),
            "yandex_url": _norm_str(it.get("url")),
        }
        branches.append(b)

        if isinstance(b["rating"], (int, float)):
            ratings.append(float(b["rating"]))
        if isinstance(b["reviews_count"], (int, float)):
            reviews.append(float(b["reviews_count"]))

    avg_rating = sum(ratings) / len(ratings) if ratings else None
    total_reviews = int(sum(reviews)) if reviews else None

    website_enrichment = None
    socials = []
    phones = []
    emails = []

    if enrich_websites and website:
        website_enrichment = await fetch_and_extract_website_data(website)
        if website_enrichment and website_enrichment.get("ok"):
            socials = website_enrichment.get("socials") or []
            phones = website_enrichment.get("phones") or []
            emails = website_enrichment.get("emails") or []

    digital_score = _digital_score(socials, website)

    return {
        "ok": True,
        "brand": {
            "name": brand_name,
            "website": website,
            "avg_rating": avg_rating,
            "total_reviews": total_reviews,
        },
        "branches": branches,
        "socials": socials,
        "phones": phones,
        "emails": emails,
        "digital_score": digital_score,
        "website_enrichment": website_enrichment,
    }
