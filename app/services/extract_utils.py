from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import (
    urlparse,
    urlunparse,
    parse_qsl,
    urlencode,
    unquote,
)

# -----------------------------
# Shared extraction utilities
# -----------------------------

SOCIAL_HOSTS = {
    "instagram.com": "instagram",
    "www.instagram.com": "instagram",

    "t.me": "telegram",
    "telegram.me": "telegram",
    "www.t.me": "telegram",

    "wa.me": "whatsapp",
    "api.whatsapp.com": "whatsapp",
    "chat.whatsapp.com": "whatsapp",

    "youtube.com": "youtube",
    "www.youtube.com": "youtube",
    "youtu.be": "youtube",

    "vk.com": "vk",
    "www.vk.com": "vk",
    "m.vk.com": "vk",

    "ok.ru": "ok",
    "www.ok.ru": "ok",

    "tiktok.com": "tiktok",
    "www.tiktok.com": "tiktok",
}

_SKIP_QUERY_KEYS = {
    "utm_source", "utm_medium", "utm_campaign",
    "utm_term", "utm_content", "igsh", "fbclid",
}

# URLs from html (supports //domain/path)
URL_REGEX = re.compile(r"((?:https?:)?//[^\s\"'<>]+)", re.IGNORECASE)

# Bare social mentions without scheme (only known domains)
BARE_SOCIAL_REGEX = re.compile(
    r"\b(?:instagram\.com|t\.me|telegram\.me|vk\.com|ok\.ru|tiktok\.com|youtu\.be|youtube\.com|wa\.me)\s*/[A-Za-z0-9._\-/?=&%+#]+\b",
    re.IGNORECASE,
)

# Deep links
TG_DEEPLINK_REGEX = re.compile(r"\btg://resolve\?domain=([A-Za-z0-9_]{3,})\b", re.IGNORECASE)
WA_DEEPLINK_REGEX = re.compile(r"\bwhatsapp://send\?phone=([0-9+]{10,16})\b", re.IGNORECASE)

# Email candidates
EMAIL_CANDIDATE_REGEX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Phone candidates (wide)
PHONE_CANDIDATE_REGEX = re.compile(r"(?:\+?\d[\d\s\-\(\)]{7,}\d)")

_BAD_EMAIL_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".ico", ".css", ".js", ".woff", ".woff2", ".ttf")
_BAD_EMAIL_LOCAL_HINTS = ("@2x", "@3x")


class HrefParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        href = None
        for k, v in attrs:
            if k.lower() == "href" and v:
                href = v.strip()
                break
        if href:
            self.links.append(href)


def _digits_only(s: str) -> str:
    return re.sub(r"\D+", "", s or "")


def clean_url(url: str) -> str:
    """
    - adds https: to //...
    - drops common tracking params
    - removes fragment
    """
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


def force_https_if_bare(url_or_hostpath: str) -> str:
    """
    Converts 'instagram.com/x' -> 'https://instagram.com/x'
    """
    s = (url_or_hostpath or "").strip()
    if not s:
        return ""
    if s.startswith("http://") or s.startswith("https://"):
        return s
    if s.startswith("//"):
        return "https:" + s
    return "https://" + s.lstrip("/")


def normalize_instagram(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    parts = [p for p in path.split("/") if p]

    # content links
    if len(parts) >= 2 and parts[0] in ("p", "reel", "tv"):
        return clean_url(url), "content"

    # profile
    if parts and parts[0]:
        return clean_url(f"https://instagram.com/{parts[0]}"), "profile"

    return clean_url(url), "unknown"


def normalize_telegram(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme.lower() == "tg":
        m = TG_DEEPLINK_REGEX.search(url)
        if m:
            return f"https://t.me/{m.group(1)}"
        return clean_url(url)

    username = parsed.path.strip("/").split("/")[0]
    if username:
        return f"https://t.me/{username}"
    return clean_url(url)


def _normalize_ru_phone_digits(digits: str) -> str | None:
    """
    Accepts digits-only, returns E.164 for RU (+7XXXXXXXXXX) or None.
    """
    if not digits:
        return None

    if len(digits) == 11 and digits.startswith("7"):
        return f"+{digits}"

    if len(digits) == 11 and digits.startswith("8"):
        return f"+7{digits[1:]}"

    if len(digits) == 10:
        return f"+7{digits}"

    return None


def is_junk_ru_phone(e164: str) -> bool:
    """
    Stricter junk filtering:
    - all digits same
    - obvious templates
    - ends with 0000/9999 (often placeholders)
    """
    if not e164.startswith("+7") or len(e164) != 12:
        return True

    tail = e164[2:]
    if tail == "0000000000":
        return True
    if tail in {"9999999999", "1234567890", "0987654321"}:
        return True
    if len(set(tail)) == 1:
        return True
    if tail.endswith("0000") or tail.endswith("9999"):
        return True

    return False


def extract_ru_phones(text: str) -> list[str]:
    candidates = set(PHONE_CANDIDATE_REGEX.findall(text or ""))
    out: set[str] = set()

    for raw in candidates:
        digits = _digits_only(raw)

        if len(digits) < 10 or len(digits) > 11:
            continue

        normalized = _normalize_ru_phone_digits(digits)
        if not normalized:
            continue

        if is_junk_ru_phone(normalized):
            continue

        out.add(normalized)

    return sorted(out)


def _looks_like_asset_email(email: str) -> bool:
    e = (email or "").strip().lower()
    if not e:
        return True

    local = e.split("@", 1)[0]
    if any(h in local for h in _BAD_EMAIL_LOCAL_HINTS):
        return True

    if any(ext in e for ext in _BAD_EMAIL_EXT):
        return True

    for ext in _BAD_EMAIL_EXT:
        if e.endswith(ext):
            return True

    return False


def extract_emails(text: str) -> list[str]:
    candidates = set(EMAIL_CANDIDATE_REGEX.findall(text or ""))
    out: set[str] = set()

    for email in candidates:
        e = email.strip()
        e_low = e.lower()

        if _looks_like_asset_email(e_low):
            continue
        if ".." in e_low:
            continue
        if e_low.startswith(".") or e_low.endswith("."):
            continue

        out.add(e)

    return sorted(out)


def extract_tel_mailto_from_links(links: list[str]) -> tuple[list[str], list[str]]:
    """
    Extract phones/emails from tel:/mailto:
    """
    phones: set[str] = set()
    emails: set[str] = set()

    for href in links or []:
        h = (href or "").strip()
        if not h:
            continue

        if h.lower().startswith("tel:"):
            val = unquote(h[4:])
            digits = _digits_only(val)
            normalized = _normalize_ru_phone_digits(digits)
            if normalized and not is_junk_ru_phone(normalized):
                phones.add(normalized)

        if h.lower().startswith("mailto:"):
            val = unquote(h[7:]).split("?", 1)[0].strip()
            if val and not _looks_like_asset_email(val.lower()):
                if EMAIL_CANDIDATE_REGEX.fullmatch(val):
                    emails.add(val)

    return sorted(phones), sorted(emails)


def normalize_whatsapp(url: str) -> tuple[str, str | None]:
    """
    Returns:
    - normalized url (prefer https://wa.me/<digits>)
    - phone (E.164 +7...) if RU recognized else None
    """
    u = clean_url(url)
    if not u:
        return "", None

    parsed = urlparse(u)
    host = parsed.netloc.lower()

    if host == "wa.me":
        digits = _digits_only(parsed.path.strip("/"))
        phone = _normalize_ru_phone_digits(digits) if digits else None
        return (f"https://wa.me/{digits}" if digits else u), phone

    if host == "api.whatsapp.com":
        qs = dict(parse_qsl(parsed.query, keep_blank_values=True))
        digits = _digits_only(qs.get("phone", ""))
        phone = _normalize_ru_phone_digits(digits) if digits else None
        if digits:
            return f"https://wa.me/{digits}", phone
        return u, phone

    m = WA_DEEPLINK_REGEX.search(u)
    if m:
        digits = _digits_only(m.group(1))
        phone = _normalize_ru_phone_digits(digits) if digits else None
        if digits:
            return f"https://wa.me/{digits}", phone

    return u, None


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
        return {"platform": platform, "url": normalized, "kind": kind}

    if platform == "telegram":
        return {"platform": platform, "url": clean_url(normalize_telegram(url)), "kind": "profile"}

    if platform == "whatsapp":
        wa_url, phone = normalize_whatsapp(url)
        payload = {"platform": platform, "url": clean_url(wa_url) or url, "kind": "profile"}
        if phone:
            payload["phone"] = phone
        return payload

    return {"platform": platform, "url": url, "kind": "profile"}
