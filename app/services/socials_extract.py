from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import (
    urljoin,
    urlparse,
    urlunparse,
    parse_qsl,
    urlencode,
    unquote,
)

import httpx


# -----------------------------
# Social detection
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

# Вытягиваем URL из html (в т.ч. //domain/path)
URL_REGEX = re.compile(r"((?:https?:)?//[^\s\"'<>]+)", re.IGNORECASE)

# Доп. “голые” соц-упоминания без схемы (осторожно, только известные домены)
BARE_SOCIAL_REGEX = re.compile(
    r"\b(?:instagram\.com|t\.me|telegram\.me|vk\.com|ok\.ru|tiktok\.com|youtu\.be|youtube\.com|wa\.me)\s*/[A-Za-z0-9._\-/?=&%+#]+\b",
    re.IGNORECASE,
)

# tg:// / whatsapp:// диплинки
TG_DEEPLINK_REGEX = re.compile(r"\btg://resolve\?domain=([A-Za-z0-9_]{3,})\b", re.IGNORECASE)
WA_DEEPLINK_REGEX = re.compile(r"\bwhatsapp://send\?phone=([0-9+]{10,16})\b", re.IGNORECASE)

# Email (кандидаты)
EMAIL_CANDIDATE_REGEX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Телефон-кандидат (достаточно широкий)
PHONE_CANDIDATE_REGEX = re.compile(r"(?:\+?\d[\d\s\-\(\)]{7,}\d)")

# чтобы не принять картинку/ассет за email
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
    - добавляет https: к //...
    - выкидывает utm/igsh/fbclid
    - убирает fragment
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


def _force_https_if_bare(url_or_hostpath: str) -> str:
    """
    Превращает 'instagram.com/x' -> 'https://instagram.com/x'
    """
    s = (url_or_hostpath or "").strip()
    if not s:
        return ""
    if s.startswith("http://") or s.startswith("https://"):
        return s
    if s.startswith("//"):
        return "https:" + s
    # голая запись домена/пути
    return "https://" + s.lstrip("/")


def normalize_instagram(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    parts = [p for p in path.split("/") if p]

    # контентные ссылки
    if len(parts) >= 2 and parts[0] in ("p", "reel", "tv"):
        return clean_url(url), "content"

    # профиль
    if parts and parts[0]:
        return clean_url(f"https://instagram.com/{parts[0]}"), "profile"

    return clean_url(url), "unknown"


def normalize_telegram(url: str) -> str:
    # t.me/<user> OR tg://resolve?domain=<user>
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

    # 10 цифр без префикса
    if len(digits) == 10:
        return f"+7{digits}"

    return None


def _is_junk_ru_phone(e164: str) -> bool:
    """
    Более строгая фильтрация мусора:
    - все цифры одинаковые
    - очевидные шаблоны 1234567890 / 9999999999 / 0000000000
    - слишком “нулевые” хвосты
    """
    if not e164.startswith("+7") or len(e164) != 12:
        return True

    tail = e164[2:]  # 10 цифр
    if tail == "0000000000":
        return True
    if tail in {"9999999999", "1234567890", "0987654321"}:
        return True
    if len(set(tail)) == 1:
        return True
    # частые “заглушки”
    if tail.endswith("0000") or tail.endswith("9999"):
        return True

    return False


def extract_ru_phones(html: str) -> list[str]:
    candidates = set(PHONE_CANDIDATE_REGEX.findall(html or ""))
    out: set[str] = set()

    for raw in candidates:
        digits = _digits_only(raw)

        # быстрые отсечки мусора
        if len(digits) < 10 or len(digits) > 11:
            continue

        # иногда в html встречаются длинные цепочки, обрезанные регэкспом — отсекаем типичные
        if len(digits) == 11 and digits.startswith("7000000"):
            continue

        normalized = _normalize_ru_phone_digits(digits)
        if not normalized:
            continue

        if _is_junk_ru_phone(normalized):
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


def extract_emails(html: str) -> list[str]:
    candidates = set(EMAIL_CANDIDATE_REGEX.findall(html or ""))
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
            if normalized and not _is_junk_ru_phone(normalized):
                phones.add(normalized)

        if h.lower().startswith("mailto:"):
            val = unquote(h[7:]).split("?", 1)[0].strip()
            if val and not _looks_like_asset_email(val.lower()):
                if EMAIL_CANDIDATE_REGEX.fullmatch(val):
                    emails.add(val)

    return sorted(phones), sorted(emails)


def normalize_whatsapp(url: str) -> tuple[str, str | None]:
    """
    Возвращает:
    - нормализованный url (предпочтительно https://wa.me/<digits>)
    - phone (E.164 +7... если распознали РФ) иначе None
    """
    u = clean_url(url)
    if not u:
        return "", None

    parsed = urlparse(u)
    host = parsed.netloc.lower()

    # wa.me/<digits>
    if host == "wa.me":
        digits = _digits_only(parsed.path.strip("/"))
        phone = _normalize_ru_phone_digits(digits) if digits else None
        return f"https://wa.me/{digits}" if digits else u, phone

    # api.whatsapp.com/send?phone=<digits>
    if host == "api.whatsapp.com":
        qs = dict(parse_qsl(parsed.query, keep_blank_values=True))
        digits = _digits_only(qs.get("phone", ""))
        phone = _normalize_ru_phone_digits(digits) if digits else None
        if digits:
            return f"https://wa.me/{digits}", phone
        return u, phone

    # whatsapp://send?phone=...
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


# -----------------------------
# Important pages
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


def _same_reg_domain(a: str, b: str) -> bool:
    """
    Без внешних зависимостей: сравниваем host целиком.
    Для твоей задачи этого хватает (мы не делаем PSL).
    """
    try:
        ha = urlparse(a).netloc.lower()
        hb = urlparse(b).netloc.lower()
        return bool(ha) and ha == hb
    except Exception:
        return False


# -----------------------------
# Main extractor
# -----------------------------

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

    # 1) собираем ссылки: href + URL в тексте + “голые” соц-упоминания
    raw_links = set(parser.links)
    raw_links |= set(URL_REGEX.findall(html))
    raw_links |= set(BARE_SOCIAL_REGEX.findall(html))

    # 2) добавим диплинки
    for m in TG_DEEPLINK_REGEX.findall(html or ""):
        raw_links.add(f"https://t.me/{m}")
    for m in WA_DEEPLINK_REGEX.findall(html or ""):
        raw_links.add(f"whatsapp://send?phone={m}")

    # 3) нормализация относительных ссылок + clean_url
    normalized_links: set[str] = set()
    for link in raw_links:
        link = (link or "").strip()
        if not link:
            continue

        # "instagram.com/x" без схемы
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
            link = _force_https_if_bare(link)

        # относительные урлы сайта
        if link.startswith("/"):
            link = urljoin(website_url, link)

        link = clean_url(link)
        if link:
            normalized_links.add(link)

    # 4) соцсети
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

    # 5) телефоны/емейлы
    phones_html = extract_ru_phones(html)
    emails_html = extract_emails(html)
    phones_link, emails_link = extract_tel_mailto_from_links(parser.links)

    phones = sorted(set(phones_html) | set(phones_link))
    emails = sorted(set(emails_html) | set(emails_link))

    # 6) important_pages (только тот же домен)
    important_pages: set[str] = set()
    for href in parser.links:
        if not _important_page_hint(href):
            continue

        link = href.strip()
        if link.startswith("/"):
            link = urljoin(website_url, link)
        else:
            # если внешний абсолютный — оставим только если тот же host
            if link.startswith(("http://", "https://", "//")):
                link = _force_https_if_bare(link)
            else:
                link = urljoin(website_url, link)

        link = clean_url(link)
        if link and _same_reg_domain(link, website_url):
            important_pages.add(link)

    return {
        "ok": True,
        "website": clean_url(website_url),
        "socials": sorted(socials, key=lambda x: (x.get("platform", ""), x.get("url", ""))),
        "phones": phones,
        "emails": emails,
        "important_pages": sorted(important_pages),
    }
