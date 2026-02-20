import re
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign",
    "utm_term", "utm_content",
    "gclid", "yclid", "ref"
}


def normalize_url(url: str) -> str | None:
    if not url:
        return None

    url = url.strip()

    # Telegram @username → https://t.me/username
    if url.startswith("@"):
        return f"https://t.me/{url[1:]}"

    if not url.startswith("http"):
        url = "https://" + url

    try:
        parsed = urlparse(url)
        query = dict(parse_qsl(parsed.query))

        # remove tracking params
        clean_query = {
            k: v for k, v in query.items()
            if k not in TRACKING_PARAMS
        }

        normalized = parsed._replace(
            scheme="https",
            query=urlencode(clean_query),
            fragment=""
        )

        clean_url = urlunparse(normalized)

        # remove trailing slash
        if clean_url.endswith("/"):
            clean_url = clean_url[:-1]

        return clean_url.lower()

    except Exception:
        return None


def dedupe_urls(urls: list[str]) -> list[str]:
    clean = []
    seen = set()

    for url in urls:
        norm = normalize_url(url)
        if norm and norm not in seen:
            seen.add(norm)
            clean.append(norm)

    return clean
