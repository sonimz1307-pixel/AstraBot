from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode, unquote

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

# Кандидаты для телефона: +7 (xxx) xxx-xx-xx и т.п.
PHONE_CANDIDATE_REGEX = re.compile(r"(?:\+?\d[\d\s\-\(\)]{7,}\d)")
# Email (кандидаты)
EMAIL_CANDIDATE_REGEX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
URL_REGEX = re.compile(r"(https?://[^\s\"'<>]+)", re.IGNORECASE)

# чтобы не принять картинку/ассет за email
_BAD_EMAIL_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".ico", ".css", ".js", ".woff", ".woff2", ".ttf")
_BAD_EMAIL_LOCAL_HINTS = ("@2x", "@3x")

# защита от случайных "временных" чисел
_BAD_PHONE_SUBSTRINGS = ("202", "197", "198", "199")  # мягкий фильтр, основной — валидатор длины/префикса


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
    parts = [p for p in path.split("/") if p]

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


def _digits_only(s: str) -> str:
    return re.sub(r"\D+", "", s or "")


def _normalize_ru_phone_digits(digits: str) -> str | None:
    """
    Принимает digits (только цифры), возвращает E.164 для РФ (+7XXXXXXXXXX) или None.
    """
    if not digits:
        return None

    # +7XXXXXXXXXX (11 цифр, начинается с 7)
    if len(digits) == 11 and digits.startswith("7"):
        return f"+{digits}"

    # 8XXXXXXXXXX -> +7XXXXXXXXXX
    if len(digits) == 11 and digits.startswith("8"):
        return f"+7{digits[1:]}"

    # иногда на сайтах пишут без 8/7: 10 цифр
    if len(digits) == 10:
        return f"+7{digits}"

    return None


def _is_plausible_ru_mobile(digits_e164: str) -> bool:
    """
    Мягкая проверка: после +7 номер обычно 9XXXXXXXXX для мобильных,
    но оставим и городские (4/3-значные коды) как допустимые.
    """
    if not digits_e164.startswith("+7") or len(digits_e164) != 12:
        return False
    # Не блокируем городские, но отсекаем совсем неадекватные типа +70000000000
    tail = digits_e164[2:]
    if tail == "0000000000":
        return False
    return True


def extract_ru_phones(html: str) -> list[str]:
    candidates = set(PHONE_CANDIDATE_REGEX.findall(html or ""))
    out: set[str] = set()

    for raw in candidates:
        digits = _digits_only(raw)

        # быстрые отсечки мусора
        if len(digits) < 10 or len(digits) > 11:
            continue

        # иногда в html встречаются последовательности типа 700000010288..., режем
        if len(digits) == 11 and digits.startswith("7000000"):
            continue

        normalized = _normalize_ru_phone_digits(digits)
        if not normalized:
            continue

        if not _is_plausible_ru_mobile(normalized):
            continue

        out.add(normalized)

    return sorted(out)


def _looks_like_asset_email(email: str) -> bool:
    e = (email or "").strip().lower()
    if not e:
        return True

    # локальная часть может содержать @3x / @2x в именах ассетов
    local = e.split("@", 1)[0]
    if any(h in local for h in _BAD_EMAIL_LOCAL_HINTS):
        return True

    # если в домене/хвосте встречается расширение ассета — это почти наверняка имя файла
    if any(ext in e for ext in _BAD_EMAIL_EXT):
        return True

    # если домен "png" и т.п. (крайние случаи)
    for ext in _BAD_EMAIL_EXT:
        if e.endswith(ext):
            return True

    return False


def extract_emails(html: str) -> list[str]:
    candidates = set(EMAIL_CANDIDATE_REGEX.findall(html or ""))
    out: set[str] = set()

    for email in candidates:
        e = email.strip()
        e_low = e.lower()

        if _looks_like_asset_email(e_low):
            continue

        # базовая здравость
        if ".." in e_low:
            continue
        if e_low.startswith(".") or e_low.endswith("."):
            continue

        out.add(e)

    return sorted(out)


def extract_tel_mailto_from_links(links: list[str]) -> tuple[list[str], list[str]]:
    """
    Дополнительно достаём телефоны/emails из tel:/mailto: ссылок.
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
            if normalized and _is_plausible_ru_mobile(normalized):
                phones.add(normalized)

        if h.lower().startswith("mailto:"):
            val = unquote(h[7:]).split("?", 1)[0].strip()
            if val and not _looks_like_asset_email(val.lower()):
                if EMAIL_CANDIDATE_REGEX.fullmatch(val):
                    emails.add(val)

    return sorted(phones), sorted(emails)


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

    # собираем ссылки: из href и из "голых" URL в тексте
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

    # телефоны/емейлы: строгая очистка
    phones_html = extract_ru_phones(html)
    emails_html = extract_emails(html)

    phones_link, emails_link = extract_tel_mailto_from_links(parser.links)

    phones = sorted(set(phones_html) | set(phones_link))
    emails = sorted(set(emails_html) | set(emails_link))

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
