from __future__ import annotations

from urllib.parse import urljoin, urlparse

import httpx

from .extract_utils import (
    HrefParser,
    URL_REGEX,
    BARE_SOCIAL_REGEX,
    TG_DEEPLINK_REGEX,
    WA_DEEPLINK_REGEX,
    clean_url,
    force_https_if_bare,
    detect_social,
    extract_ru_phones,
    extract_emails,
    extract_tel_mailto_from_links,
)

from .taplink_extract import extract_taplink_data


# -----------------------------
# Smart Landing Engine
# -----------------------------

LANDING_HOST_KEYWORDS = (
    "taplink",
    "linktr.ee",
    "mssg.me",
    "nethouse.id",
    "hipolink",
    "teletype.in",  # иногда используют как лендинг
)


def detect_landing_type(website_url: str) -> str:
    try:
        host = urlparse(website_url).netloc.lower()
    except Exception:
        return "generic"

    for k in LANDING_HOST_KEYWORDS:
        if k in host:
            return "taplink"
    return "generic"


# -----------------------------
# Generic Website Extractor (HTML based)
# -----------------------------

def _important_page_hint(href: str) -> bool:
    h = (href or "").lower()
    return any(k in h for k in [
        "price", "цены", "стоимость", "abon", "абон",
        "contact", "контакт",
        "schedule", "распис",
        "service", "услуг", "classes", "program",
        "about", "о-нас", "о_нас", "о%20нас", "о нас",
        "адрес", "location", "branch", "filial", "филиал",
        "trial", "пробн", "free", "бесплат",
        "rent", "аренд",
        "loyalty", "bonus", "лояль", "бонус",
        "subscription", "подпис", "абонем"
    ])


def _same_domain(a: str, b: str) -> bool:
    try:
        ha = urlparse(a).netloc.lower()
        hb = urlparse(b).netloc.lower()
        return bool(ha) and ha == hb
    except Exception:
        return False


async def extract_generic_website_data(website_url: str, timeout: float = 20.0) -> dict:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; MarketIntelBot/1.0)",
        "Accept": "text/html,application/xhtml+xml",
    }

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
        r = await client.get(website_url)
        r.raise_for_status()
        html = r.text or ""

    parser = HrefParser()
    parser.feed(html)

    # links: href + URLs in text + bare social mentions
    raw_links = set(parser.links)
    raw_links |= set(URL_REGEX.findall(html))
    raw_links |= set(BARE_SOCIAL_REGEX.findall(html))

    # deep links
    for m in TG_DEEPLINK_REGEX.findall(html or ""):
        raw_links.add(f"https://t.me/{m}")
    for m in WA_DEEPLINK_REGEX.findall(html or ""):
        raw_links.add(f"whatsapp://send?phone={m}")

    # normalize to absolute + clean_url
    normalized_links: set[str] = set()
    for link in raw_links:
        link = (link or "").strip()
        if not link:
            continue

        # "instagram.com/x" without scheme
        if not link.startswith(("http://", "https://", "//")) and (
            "instagram.com" in link.lower()
            or "t.me" in link.lower()
            or "telegram.me" in link.lower()
            or "vk.com" in link.lower()
            or "ok.ru" in link.lower()
            or "tiktok.com" in link.lower()
            or "youtu.be" in link.lower()
            or "youtube.com" in link.lower()
            or "wa.me" in link.lower()
        ):
            link = force_https_if_bare(link)

        # site-relative urls
        if link.startswith("/"):
            link = urljoin(website_url, link)

        link = clean_url(link)
        if link:
            normalized_links.add(link)

    # socials
    socials = []
    seen = set()
    for link in normalized_links:
        social = detect_social(link)
        if not social:
            continue
        key = (social["platform"], social["url"])
        if key in seen:
            continue
        seen.add(key)
        socials.append(social)

    # phones/emails
    phones_html = extract_ru_phones(html)
    emails_html = extract_emails(html)
    phones_link, emails_link = extract_tel_mailto_from_links(parser.links)

    phones = sorted(set(phones_html) | set(phones_link))
    emails = sorted(set(emails_html) | set(emails_link))

    # important pages (same host only)
    important_pages: set[str] = set()
    for href in parser.links:
        if not _important_page_hint(href):
            continue

        link = href.strip()
        if link.startswith("/"):
            link = urljoin(website_url, link)
        else:
            if link.startswith(("http://", "https://", "//")):
                link = force_https_if_bare(link)
            else:
                link = urljoin(website_url, link)

        link = clean_url(link)
        if link and _same_domain(link, website_url):
            important_pages.add(link)

    return {
        "ok": True,
        "website": clean_url(website_url),
        "source_type": "generic",
        "socials": sorted(socials, key=lambda x: (x.get("platform", ""), x.get("url", ""))),
        "phones": phones,
        "emails": emails,
        "important_pages": sorted(important_pages),
    }


# -----------------------------
# Public API used by router
# -----------------------------

async def fetch_and_extract_website_data(website_url: str, timeout: float = 20.0) -> dict:
    """
    Backward compatible entrypoint:
    Returns the same keys as before, plus optional 'source_type'.
    """
    if not website_url:
        return {"ok": False, "error": "no_url"}

    landing_type = detect_landing_type(website_url)

    try:
        if landing_type == "taplink":
            return await extract_taplink_data(website_url, timeout=max(timeout, 25.0))
        return await extract_generic_website_data(website_url, timeout=timeout)
    except httpx.HTTPError as e:
        return {"ok": False, "website": clean_url(website_url), "error": f"http_error: {type(e).__name__}"}
    except Exception as e:
        return {"ok": False, "website": clean_url(website_url), "error": f"extract_error: {type(e).__name__}"}
