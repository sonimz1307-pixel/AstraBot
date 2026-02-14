from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode

import httpx

SOCIAL_HOSTS = {
    "instagram.com": "instagram",
    "www.instagram.com": "instagram",
    "t.me": "telegram",
    "telegram.me": "telegram",
    "wa.me": "whatsapp",
    "api.whatsapp.com": "whatsapp",
    "chat.whatsapp.com": "whatsapp",
    "youtube.com": "youtube",
    "www.youtube.com": "youtube",
    "youtu.be": "youtube",
    "vk.com": "vk",
    "www.vk.com": "vk",
    "ok.ru": "ok",
    "www.ok.ru": "ok",
    "tiktok.com": "tiktok",
    "www.tiktok.com": "tiktok",
}

_SKIP_QUERY_KEYS = {
    "utm_source", "utm_medium", "utm_campaign",
    "utm_term", "utm_content", "igsh", "fbclid"
}

PHONE_REGEX = re.compile(r"\+?\d[\d\s\-\(\)]{8,}\d")
EMAIL_REGEX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
URL_REGEX = re.compile(r"(https?://[^\s\"'<>]+)", re.IGNORECASE)


class HrefParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        for k, v in attrs:
            if k.lower() == "href" and v:
                self.links.append(v.strip())


def clean_url(url: str) -> str:
    if not url:
        return ""
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url

    parsed = urlparse(url)
    if not parsed.scheme:
        return ""

    query = [
        (k, v)
        for (k, v) in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in _SKIP_QUERY_KEYS
    ]

    cleaned = parsed._replace(query=urlencode(query), fragment="")
    return urlunparse(cleaned)


def normalize_instagram(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    parts = path.split("/")

    if len(parts) >= 2 and parts[0] in ("p", "reel", "tv"):
        return url, "content"

    if parts and parts[0]:
        return f"https://instagram.com/{parts[0]}", "profile"

    return url, "unknown"


def normalize_telegram(url: str) -> str:
    parsed = urlparse(url)
    username = parsed.path.strip("/").split("/")[0]
    if username:
        return f"https://t.me/{username}"
    return url


def normalize_whatsapp(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc.lower() == "wa.me":
        return f"https://wa.me/{parsed.path.strip('/')}"
    return url


def detect_social(url: str):
    url = clean_url(url)
    if not url:
        return None

    parsed = urlparse(url)
    host = parsed.netloc.lower()

    platform = SOCIAL_HOSTS.get(host)
    if not platform:
        return None

    if platform == "instagram":
        normalized, kind = normalize_instagram(url)
        return {"platform": platform, "url": clean_url(normalized), "kind": kind}

    if platform == "telegram":
        return {"platform": platform, "url": clean_url(normalize_telegram(url)), "kind": "profile"}

    if platform == "whatsapp":
        return {"platform": platform, "url": clean_url(normalize_whatsapp(url)), "kind": "profile"}

    return {"platform": platform, "url": url, "kind": "profile"}


def _important_page_hint(href: str) -> bool:
    h = (href or "").lower()
    return any(k in h for k in [
        "price", "цены", "стоимость", "abon", "абон",
        "contact", "контакт",
        "schedule", "распис",
        "service", "услуг", "classes", "program",
        "about", "о-нас", "о_нас", "о%20нас", "о нас",
        "адрес", "location", "branch", "filial"
    ])


async def fetch_and_extract_website_data(website_url: str, timeout: float = 20.0) -> dict:
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

    parser = HrefParser()
    parser.feed(html)

    raw_links = set(parser.links + URL_REGEX.findall(html))

    socials = []
    seen = set()
    for link in raw_links:
        if link.startswith("/"):
            link = urljoin(website_url, link)
        social = detect_social(link)
        if not social:
            continue
        key = (social["platform"], social["url"])
        if key in seen:
            continue
        seen.add(key)
        socials.append(social)

    phones = sorted(set(PHONE_REGEX.findall(html)))
    emails = sorted(set(EMAIL_REGEX.findall(html)))

    important_pages = []
    for href in parser.links:
        if not _important_page_hint(href):
            continue
        link = href
        if link.startswith("/"):
            link = urljoin(website_url, link)
        link = clean_url(link)
        if link:
            important_pages.append(link)
    important_pages = sorted(set(important_pages))

    return {
        "ok": True,
        "website": clean_url(website_url),
        "socials": socials,
        "phones": phones,
        "emails": emails,
        "important_pages": important_pages,
    }
