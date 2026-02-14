from __future__ import annotations

import re
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

# Taplink/Link-in-bio pages are often JS-heavy, but they still contain:
# - lots of outbound links in <a href=...>
# - URLs embedded in JSON blobs in <script> tags
# Strategy: fetch HTML, harvest *all* URLs, normalize, then detect socials + contact artifacts.
SCRIPT_URL_REGEX = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


def _landing_links_from_html(html: str) -> set[str]:
    parser = HrefParser()
    parser.feed(html or "")

    raw_links = set(parser.links)
    raw_links |= set(URL_REGEX.findall(html or ""))
    raw_links |= set(BARE_SOCIAL_REGEX.findall(html or ""))
    raw_links |= set(SCRIPT_URL_REGEX.findall(html or ""))

    # deep links present in some templates
    for m in TG_DEEPLINK_REGEX.findall(html or ""):
        raw_links.add(f"https://t.me/{m}")
    for m in WA_DEEPLINK_REGEX.findall(html or ""):
        raw_links.add(f"whatsapp://send?phone={m}")

    return raw_links


async def extract_taplink_data(website_url: str, timeout: float = 25.0) -> dict:
    if not website_url:
        return {"ok": False, "error": "no_url"}

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; MarketIntelBot/1.0)",
        "Accept": "text/html,application/xhtml+xml",
    }

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
        r = await client.get(website_url)
        r.raise_for_status()
        html = r.text or ""

    # links
    raw_links = _landing_links_from_html(html)

    normalized_links: set[str] = set()
    for link in raw_links:
        link = (link or "").strip()
        if not link:
            continue

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

    # phones/emails: taplink often shows them as plain text + tel/mailto links
    parser = HrefParser()
    parser.feed(html or "")
    phones_html = extract_ru_phones(html)
    emails_html = extract_emails(html)
    phones_link, emails_link = extract_tel_mailto_from_links(parser.links)

    phones = sorted(set(phones_html) | set(phones_link))
    emails = sorted(set(emails_html) | set(emails_link))

    # "important_pages" for taplink are less meaningful; keep top outbound non-social links as potential CTAs
    # but only if they are same landing domain (rare) – so default to [] for cleanliness.
    important_pages: list[str] = []

    return {
        "ok": True,
        "website": clean_url(website_url),
        "source_type": "taplink",
        "socials": sorted(socials, key=lambda x: (x.get("platform", ""), x.get("url", ""))),
        "phones": phones,
        "emails": emails,
        "important_pages": important_pages,
    }
