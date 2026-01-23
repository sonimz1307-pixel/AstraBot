import os
import base64
import time
import re
import json
from io import BytesIO
from typing import Optional, Literal, Dict, Any, Tuple

import httpx
from fastapi import FastAPI, Request, Response

app = FastAPI()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change_me")

IMG_SIZE_DEFAULT = os.getenv("IMG_SIZE_DEFAULT", "1024x1536")
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# ---------------- In-memory state ----------------
STATE_TTL_SECONDS = int(os.getenv("STATE_TTL_SECONDS", "1800"))  # 30 минут
STATE: Dict[Tuple[int, int], Dict[str, Any]] = {}

PosterStep = Literal["need_photo", "need_prompt"]

# Anti-duplicate (idempotency)
PROCESSED_TTL_SECONDS = int(os.getenv("PROCESSED_TTL_SECONDS", "1800"))  # 30 минут
PROCESSED_UPDATES: Dict[int, float] = {}                 # update_id -> ts
PROCESSED_MESSAGES: Dict[Tuple[int, int], float] = {}    # (chat_id, message_id) -> ts


def _now() -> float:
    return time.time()


def _cleanup_state():
    now = _now()

    expired_state = []
    for k, v in STATE.items():
        ts = float(v.get("ts", 0))
        if now - ts > STATE_TTL_SECONDS:
            expired_state.append(k)
    for k in expired_state:
        STATE.pop(k, None)

    expired_updates = [k for k, ts in PROCESSED_UPDATES.items() if now - float(ts) > PROCESSED_TTL_SECONDS]
    for k in expired_updates:
        PROCESSED_UPDATES.pop(k, None)

    expired_msgs = [k for k, ts in PROCESSED_MESSAGES.items() if now - float(ts) > PROCESSED_TTL_SECONDS]
    for k in expired_msgs:
        PROCESSED_MESSAGES.pop(k, None)


def _get_user_key(chat_id: int, user_id: int) -> Tuple[int, int]:
    return (int(chat_id), int(user_id))


def _ensure_state(chat_id: int, user_id: int) -> Dict[str, Any]:
    key = _get_user_key(chat_id, user_id)
    if key not in STATE:
        STATE[key] = {"mode": "chat", "ts": _now(), "poster": {}}
    STATE[key]["ts"] = _now()
    return STATE[key]


def _set_mode(chat_id: int, user_id: int, mode: Literal["chat", "poster"]):
    st = _ensure_state(chat_id, user_id)
    st["mode"] = mode
    st["ts"] = _now()
    if mode == "poster":
        # Визуальный режим: афиша ИЛИ обычный фото-эдит (после фото)
        st["poster"] = {"step": "need_photo", "photo_bytes": None}


# ---------------- Reply keyboard ----------------

def _main_menu_keyboard() -> dict:
    return {
        "keyboard": [
            [{"text": "ИИ (чат)"}, {"text": "Фото/Афиши"}],
            [{"text": "Помощь"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False,
        "selective": False,
    }


# ---------------- Telegram helpers ----------------

async def tg_send_message(chat_id: int, text: str, reply_markup: Optional[dict] = None):
    if not TELEGRAM_BOT_TOKEN:
        return
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=30) as client:
        await client.post(f"{TELEGRAM_API_BASE}/sendMessage", json=payload)


async def tg_send_photo_bytes(chat_id: int, image_bytes: bytes, caption: Optional[str] = None):
    if not TELEGRAM_BOT_TOKEN:
        return
    files = {"photo": ("image.png", image_bytes, "image/png")}
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
    async with httpx.AsyncClient(timeout=180) as client:
        await client.post(f"{TELEGRAM_API_BASE}/sendPhoto", data=data, files=files)


async def tg_get_file_path(file_id: str) -> str:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"{TELEGRAM_API_BASE}/getFile", params={"file_id": file_id})
    r.raise_for_status()
    data = r.json()
    return data["result"]["file_path"]


async def tg_download_file_bytes(file_path: str) -> bytes:
    url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.get(url)
    r.raise_for_status()
    return r.content


# ---------------- Prompts ----------------

UNICODE_MATH_SYSTEM_PROMPT = (
    "Ты решаешь математические задачи для Telegram.\n"
    "НЕ используй LaTeX/TeX и команды вида \\frac, \\pi, \\[ \\], \\( \\), \\mathbb и т.п.\n"
    "Пиши только обычным текстом и Unicode-символами.\n\n"
    "Используй символы: π, ℤ, ⇒, −, ×, ÷, ≤, ≥, ∈.\n"
    "Формулы пиши в одну строку, чтобы в Telegram всё читалось.\n"
    "Оформление ответа:\n"
    "1) Коротко: что делаем\n"
    "2) Решение по шагам\n"
    "3) В конце отдельной строкой: 'Ответ: ...'\n\n"
    "Если текст на фото плохо читается — попроси прислать фото ближе и ровнее."
)

VISION_GENERAL_SYSTEM_PROMPT = (
    "Ты анализируешь изображения для Telegram.\n"
    "Если пользователь просит определить объект (машина, цветок, товар и т.д.) — опиши, что на фото, "
    "и предложи наиболее вероятные варианты идентификации.\n"
    "Если уверенность низкая — честно скажи и попроси уточняющие детали.\n"
    "НЕ используй LaTeX/TeX.\n"
    "Отвечай кратко, структурировано.\n\n"
    "Формат:\n"
    "1) Что на фото\n"
    "2) Возможная идентификация (1–3 варианта)\n"
    "3) Что нужно, чтобы уточнить (если нужно)"
)

DEFAULT_TEXT_SYSTEM_PROMPT = (
    "Ты полезный ассистент для Telegram. Не используй LaTeX/TeX. "
    "Если нужна математика — пиши формулы обычным текстом."
)

VISION_DEFAULT_USER_PROMPT = (
    "Опиши, что на фото. Если это объект (машина/цветок/товар), попытайся определить что это. "
    "Если по фото нельзя уверенно определить — скажи, что нужно для уточнения."
)

VISUAL_ROUTER_SYSTEM_PROMPT = (
    "Ты классификатор запросов для режима «Фото/Афиши». Твоя задача — определить, чего хочет пользователь после отправки фото:\n\n"
    "POSTER — рекламная афиша/баннер: нужен текст на изображении (надпись, цена, поступление, акция, скидка и т.п.)\n"
    "PHOTO — обычная картинка/сцена/фото-эдит: НИКАКИХ надписей, никаких цен, никаких слоганов.\n\n"
    "Верни СТРОГО JSON без текста вокруг:\n"
    "{\"mode\":\"POSTER\"|\"PHOTO\",\"reason\":\"коротко\"}\n\n"
    "Правила:\n"
    "- Если есть слова/смысл: «афиша», «баннер», «реклама», «постер», «надпись», «напиши», «добавь текст», "
    "«цена», «₽», «руб», «поступление», «акция», «скидка», «прайс», «для магазина», «промо» → POSTER.\n"
    "- Если пользователь описывает сцену/сюжет/атмосферу/людей/добавить персонажа/предмет и НЕ просит текст/цену → PHOTO.\n"
    "- Если пользователь явно пишет: «без текста», «без надписей», «без букв», «просто картинка», «обычная картинка» → PHOTO.\n"
    "- Если сомневаешься — выбирай PHOTO (не навязывай афишу).\n"
)


# ---------------- OpenAI calls ----------------

async def openai_chat_answer(
    user_text: str,
    system_prompt: str,
    image_bytes: Optional[bytes] = None,
    temperature: float = 0.5,
    max_tokens: int = 800,
) -> str:
    if not OPENAI_API_KEY:
        return "OPENAI_API_KEY не задан в переменных окружения."

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}

    if image_bytes is not None:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        user_content = []
        if user_text:
            user_content.append({"type": "text", "text": user_text})
        user_content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})

        payload = {
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
    else:
        payload = {
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)

    if r.status_code != 200:
        return f"Ошибка OpenAI ({r.status_code}): {r.text[:1600]}"

    data = r.json()
    return (data["choices"][0]["message"]["content"] or "").strip() or "Пустой ответ от модели."


def _detect_image_type(b: bytes) -> Tuple[str, str]:
    if not b:
        return ("jpg", "image/jpeg")
    if b.startswith(b"\xFF\xD8\xFF"):
        return ("jpg", "image/jpeg")
    if b.startswith(b"\x89PNG\r\n\x1a\n"):
        return ("png", "image/png")
    if b.startswith(b"RIFF") and len(b) >= 12 and b[8:12] == b"WEBP":
        return ("webp", "image/webp")
    return ("jpg", "image/jpeg")


async def openai_edit_image(
    source_image_bytes: bytes,
    prompt: str,
    size: str,
    mask_png_bytes: Optional[bytes] = None,
) -> bytes:
    """
    Универсальный image edit (gpt-image-1).
    mask используется ТОЛЬКО для PHOTO-эдита (чтобы фон не перерисовывался).
    Для афиш mask не передаём.
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY не задан в переменных окружения.")

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}

    ext, mime = _detect_image_type(source_image_bytes)

    files = {"image": (f"source.{ext}", source_image_bytes, mime)}
    if mask_png_bytes:
        files["mask"] = ("mask.png", mask_png_bytes, "image/png")

    data = {"model": "gpt-image-1", "prompt": prompt, "size": size, "n": "1"}

    async with httpx.AsyncClient(timeout=300) as client:
        r = await client.post("https://api.openai.com/v1/images/edits", headers=headers, data=data, files=files)

    if r.status_code != 200:
        raise RuntimeError(f"Ошибка Images Edit API ({r.status_code}): {r.text[:2000]}")

    resp = r.json()
    b64_img = resp["data"][0].get("b64_json")
    if not b64_img:
        raise RuntimeError("Images Edit API вернул ответ без b64_json.")
    return base64.b64decode(b64_img)


# ---------------- Intent (chat mode) ----------------

Intent = Literal["math", "identify", "general"]


def _infer_intent_from_text(text: str) -> Intent:
    t = (text or "").strip().lower()
    if not t:
        return "identify"

    math_markers = [
        "реши", "решить", "задач", "уравнен", "найди", "вычисл", "докажи",
        "sin", "cos", "tg", "ctg", "лог", "ln", "π", "пи", "интеграл", "производн",
        "корень", "дроб", "x=", "y=",
    ]
    identify_markers = [
        "что за", "что это", "определи", "какая модель", "модель", "марка",
        "какой цветок", "что за цветок", "что за машина", "что за авто",
        "что за товар", "что за устройство", "что на фото", "что изображено",
    ]

    if any(m in t for m in math_markers):
        return "math"
    if any(m in t for m in identify_markers):
        return "identify"
    return "general"


def _is_math_request(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    hard_markers = [
        "реши", "решить", "реши задачу", "задачу реши",
        "посчитай", "вычисли", "найди ответ", "найди значение", "найди x",
        "уравнение", "неравенство", "докажи", "доказать",
    ]
    return any(m in t for m in hard_markers)


# ---------------- Poster parsing ----------------

def _wants_simple_text(text: str) -> bool:
    """
    Если пользователь явно просит плоскую/обычную надпись — выключаем премиум-типографику.
    """
    t = (text or "").lower()
    markers = [
        "обычный текст",
        "простая надпись",
        "без эффектов",
        "плоский текст",
        "просто текст",
        "как обычный шрифт",
        "без дизайна",
        "без свечения",
        "без 3d",
    ]
    return any(m in t for m in markers)


def _extract_price_any(text: str) -> str:
    """
    Цена считается ценой ТОЛЬКО если:
    - есть валюта (₽/р/руб/рублей) рядом с числом, ИЛИ
    - есть слово 'цена' рядом с числом.
    Это защищает от ложных срабатываний на 0.6/1.4/12000 в описании.
    """
    raw = (text or "")
    t = raw.lower()

    # 1) число + валюта
    m1 = re.search(r"(\d[\d\s]{1,8})\s*(₽|р\.?|руб\.?|рублей)\b", t)
    if m1:
        price_num = (m1.group(1) or "").replace(" ", "").strip()
        if price_num:
            return f"{price_num}₽"

    # 2) слово "цена" + число (валюта может отсутствовать)
    m2 = re.search(r"\bцена\b[^0-9]{0,10}(\d[\d\s]{1,8})\b", t)
    if m2:
        price_num = (m2.group(1) or "").replace(" ", "").strip()
        if price_num:
            return f"{price_num}₽"

    return ""


async def openai_extract_poster_spec(user_text: str) -> Dict[str, Any]:
    raw = (user_text or "").strip()
    if not raw:
        return {"headline": "", "style": "", "price": "", "simple_text": False}

    price = _extract_price_any(raw)
    simple_text = _wants_simple_text(raw)

    low = raw.lower()
    if "надпись" in low:
        m = re.search(r"надпись\s*[:\-]\s*(.+)$", raw, flags=re.IGNORECASE | re.DOTALL)
        if m:
            headline = m.group(1).strip().strip('"“”')
            style_part = re.split(r"надпись\s*[:\-]", raw, flags=re.IGNORECASE)[0].strip()
            return {"headline": headline, "style": style_part, "price": price, "simple_text": simple_text}

    sys = (
        "Ты парсер для рекламных афиш.\n"
        "Нужно отделить: (1) текст, который надо НАПЕЧАТАТЬ (headline), "
        "(2) пожелания к стилю (style), (3) цену (price).\n\n"
        "Правила:\n"
        "• headline — короткая надпись/слоган/название. Не включай инструкции: 'сделай', 'красиво', 'в стиле', 'хочу', 'нужно'.\n"
        "• style — всё про оформление (эко/неон/премиум/зима/фон/цвета).\n"
        "• price — только если пользователь явно указал цену. Иначе пусто.\n"
        "• Верни СТРОГО JSON без текста вокруг.\n"
        "Формат: {\"headline\":\"...\",\"style\":\"...\",\"price\":\"...\"}\n"
    )
    user = f"Текст пользователя:\n{raw}"

    out = await openai_chat_answer(user_text=user, system_prompt=sys, image_bytes=None, temperature=0.0, max_tokens=250)

    try:
        data = json.loads(out)
        headline = str(data.get("headline", "")).strip()
        style = str(data.get("style", "")).strip()
        price2 = str(data.get("price", "")).strip()

        # Если модель не распознала price, но эвристика нашла валидный price — используем эвристику
        if not price2 and price:
            price2 = price

        return {"headline": headline, "style": style, "price": price2, "simple_text": simple_text}
    except Exception:
        return {"headline": "", "style": raw, "price": price, "simple_text": simple_text}


def _poster_prompt_from_spec(spec: Dict[str, Any], extra_strict: bool = False) -> str:
    """
    Афиша с премиум-типографикой по умолчанию.
    Простой плоский текст — только если пользователь явно попросил (spec['simple_text']=True).
    extra_strict=True используется для второй попытки, если модель добавила лишние фразы.
    """
    headline = (spec.get("headline") or "").strip()
    style = (spec.get("style") or "").strip()
    price = (spec.get("price") or "").strip()
    simple_text = bool(spec.get("simple_text", False))

    if not headline:
        headline = " "

    if price:
        digits_rule = "Цифры разрешены ТОЛЬКО в цене и только как указано пользователем."
        price_rule = f"Цена разрешена пользователем: {price}. Добавь цену ОДИН раз, крупно, без изменения цифр."
    else:
        digits_rule = "Запрещено добавлять любые цифры."
        price_rule = "Пользователь НЕ указал цену. Запрещено добавлять любые цены, валюту и любые цифры."

    strict_add = ""
    if extra_strict:
        strict_add = (
            "\nДОПОЛНИТЕЛЬНОЕ СТРОГОЕ ПРАВИЛО:\n"
            "Если есть соблазн добавить любой слоган/фразу/подзаголовок — НЕ добавляй. Оставь место пустым.\n"
            "Запрещены любые дополнительные крупные надписи вне HEADLINE и PRICE.\n"
        )

    typography_block = (
        "ТИПОГРАФИКА (ПРОСТАЯ — ПО ЗАПРОСУ ПОЛЬЗОВАТЕЛЯ):\n"
        "• Плоский обычный текст.\n"
        "• Без объёма, без свечения, без декоративных эффектов.\n"
        "• Нейтральный читаемый шрифт.\n"
        "• ВАЖНО: всё равно аккуратно и как у дизайнера (ровно, чисто, без кривых деформаций).\n\n"
    ) if simple_text else (
        "ПРЕМИУМ-ТИПОГРАФИКА (ПО УМОЛЧАНИЮ — ВСЕГДА):\n"
        "• Headline — главный элемент, как в дорогих брендовых постерах.\n"
        "• Объёмные или псевдо-3D буквы (лёгкий эмбосс/тиснение), мягкое свечение по краям.\n"
        "• Лёгкая тень для глубины, чистая обводка, аккуратный кернинг.\n"
        "• Материал букв: кремово-золотистый / слоновая кость / тёплый перламутр.\n"
        "• Текст — часть композиции, выглядит дорого и современно.\n"
        "• Никакого плоского «обычного» текста.\n\n"
    )

    return (
        "Сделай профессиональную рекламную афишу/промо-баннер на основе предоставленного фото.\n\n"
        "СОХРАНЕНИЕ ТОВАРА:\n"
        "• Товар/упаковка должны остаться максимально реалистичными и узнаваемыми.\n"
        "• Запрещено менять бренд, упаковку, форму, цвета, логотипы, название, вкусы.\n"
        "• Разрешено: улучшить композицию, свет, фон, добавить атмосферные элементы/декор по стилю (НЕ текстом).\n\n"
        "ТЕКСТ НА АФИШЕ — СТРОЖАЙШЕЕ ПРАВИЛО:\n"
        "1) Печатай ТОЛЬКО:\n"
        "   • HEADLINE (ровно как указано)\n"
        "   • PRICE (только если цена разрешена)\n"
        "2) Запрещено добавлять любые другие слова/фразы/слоганы от себя.\n"
        "   НЕЛЬЗЯ: «АКЦИЯ», «СКИДКА», «ХИТ», «НОВИНКА», «ЛУЧШАЯ ЦЕНА», «МАКСИМУМ ВКУСА» и любые другие.\n"
        f"3) {digits_rule}\n"
        f"4) {price_rule}\n"
        "5) НЕ печатай стиль/инструкции (например: «сделай красиво», «в стиле эко»).\n"
        "6) Не искажай написание букв в HEADLINE.\n"
        f"{strict_add}\n"
        f"{typography_block}"
        "КОМПОЗИЦИЯ:\n"
        "• Товар — главный объект.\n"
        "• Добавь визуальные элементы вкуса/атмосферы по стилю (фрукты, сок, брызги, лёд и т.п.), но без перегруза.\n\n"
        "РАЗМЕЩЕНИЕ ТЕКСТА (печатать строго):\n"
        f"HEADLINE: {headline}\n"
        + (f"PRICE: {price}\n" if price else "PRICE: (не печатать)\n")
        + "\n"
        "СТИЛЬ/АТМОСФЕРА (НЕ ПЕЧАТАТЬ КАК ТЕКСТ, только оформление):\n"
        f"{style if style else 'Премиум, чисто, современно, без перегруза.'}\n\n"
        "ФОРМАТ: вертикальный, под сторис, высокое качество.\n"
    )


# ---------------- Visual routing + PHOTO edit prompt + Auto-mask + moderation ----------------

def _sanitize_ip_terms_for_image(text: str) -> str:
    """
    Убираем/заменяем IP-имена персонажей/брендов, которые часто ловят блок.
    """
    t = (text or "")

    replacements = {
        r"\bбэтмен\b": "человек в темном костюме супергероя в маске (без логотипов и узнаваемых знаков)",
        r"\bбатмен\b": "человек в темном костюме супергероя в маске (без логотипов и узнаваемых знаков)",
        r"\bbatman\b": "a masked vigilante in a dark suit (no logos, no recognizable symbols)",
    }

    for pattern, repl in replacements.items():
        t = re.sub(pattern, repl, t, flags=re.IGNORECASE)

    return t


def _is_moderation_blocked_error(err: Exception) -> bool:
    msg = str(err).lower()
    return ("moderation_blocked" in msg) or ("safety system" in msg) or ("image_generation_user_error" in msg)


def _wants_strict_preserve(text: str) -> bool:
    t = (text or "").lower()
    markers = [
        "остальное без изменения", "остальное без изменений",
        "ничего не меняй", "ничего не менять",
        "фон не меняй", "фон не менять",
        "всё оставь как есть", "оставь как есть",
        "только добавь", "только добавить",
        "без изменений",
    ]
    return any(m in t for m in markers)


def _infer_zone_from_text(text: str) -> str:
    """
    Простая эвристика: без участия пользователя.
    Возвращает: right/left/top/bottom/center
    """
    t = (text or "").lower()

    right_markers = ["справа", "правый", "правее", "вправо", "справа у", "справа возле", "справа около"]
    left_markers = ["слева", "левый", "левее", "влево", "слева у", "слева возле", "слева около"]
    top_markers = ["сверху", "вверху", "наверху", "верх", "под потолком"]
    bottom_markers = ["снизу", "внизу", "низ", "на полу", "внизу кадра"]

    if any(m in t for m in right_markers):
        return "right"
    if any(m in t for m in left_markers):
        return "left"
    if any(m in t for m in top_markers):
        return "top"
    if any(m in t for m in bottom_markers):
        return "bottom"
    return "center"


def _photo_edit_prompt(user_text: str, strict: bool) -> str:
    raw = (user_text or "").strip()

    strict_block = ""
    if strict:
        strict_block = (
            "\nСВЕРХ-СТРОГОЕ СОХРАНЕНИЕ ИСХОДНОГО КАДРА:\n"
            "• Сохрани фон и все детали максимально близко к исходнику.\n"
            "• НЕЛЬЗЯ менять: стены, пол, мебель, двери, свет, тени, цвета, текстуры, предметы, перспективу.\n"
            "• НЕЛЬЗЯ делать ретушь/улучшайзинг/шарп/размытие/шумодав/перекраску.\n"
            "• НЕЛЬЗЯ кадрировать, менять угол камеры, менять экспозицию/баланс белого.\n"
            "• Единственное изменение — ДОБАВИТЬ новый объект/персонажа и его естественную тень/контакт.\n"
        )

    return (
        "Сделай фотореалистичный эдит изображения по описанию пользователя.\n\n"
        "КРИТИЧЕСКИЕ ПРАВИЛА:\n"
        "• Запрещено добавлять любой текст: буквы, цифры, цены, слоганы, подписи, водяные знаки, логотипы.\n"
        "• Никаких постерных элементов: плашек, лент, заголовков, типографики, рекламных рамок.\n"
        "• Если на фото есть люди — не меняй личность/лицо/возраст/черты/кожу/пропорции.\n"
        f"{strict_block}\n"
        "Если добавляешь персонажа/предмет:\n"
        "• Реалистичный масштаб.\n"
        "• Освещение и тени должны соответствовать сцене.\n"
        "• Не менять остальную сцену.\n\n"
        "ОПИСАНИЕ ПОЛЬЗОВАТЕЛЯ:\n"
        f"{raw}\n\n"
        "ФОРМАТ: вертикальный, под сторис, высокое качество.\n"
    )


def _build_zone_mask_png(source_image_bytes: bytes, zone: str) -> Optional[bytes]:
    """
    Создаёт PNG-маску: белая зона = можно рисовать, чёрное = нельзя трогать.
    Пользователь ничего не выделяет. Зона выбирается эвристикой.
    Если Pillow недоступен/ошибка — возвращает None (fallback на обычный эдит без маски).
    """
    try:
        from PIL import Image, ImageDraw  # type: ignore
    except Exception:
        return None

    try:
        im = Image.open(BytesIO(source_image_bytes)).convert("RGBA")
        w, h = im.size

        mask = Image.new("RGBA", (w, h), (0, 0, 0, 255))
        draw = ImageDraw.Draw(mask)

        if zone == "right":
            x0 = int(w * 0.65)
            rect = (x0, 0, w, h)
        elif zone == "left":
            x1 = int(w * 0.35)
            rect = (0, 0, x1, h)
        elif zone == "top":
            y1 = int(h * 0.35)
            rect = (0, 0, w, y1)
        elif zone == "bottom":
            y0 = int(h * 0.65)
            rect = (0, y0, w, h)
        else:
            x0 = int(w * 0.30)
            x1 = int(w * 0.70)
            y0 = int(h * 0.25)
            y1 = int(h * 0.85)
            rect = (x0, y0, x1, y1)

        draw.rectangle(rect, fill=(255, 255, 255, 255))

        buf = BytesIO()
        mask.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


async def openai_route_visual_mode(user_text: str) -> Tuple[str, str]:
    """
    Возвращает ("POSTER"|"PHOTO", reason)
    """
    raw = (user_text or "").strip()
    if not raw:
        return ("POSTER", "empty_text_default_poster")

    # Быстрый хард-роутинг без вызова модели
    t = raw.lower()
    poster_markers = [
        "афиша", "баннер", "реклама", "реклам", "постер",
        "надпись", "текст на", "добавь текст", "напиши",
        "цена", "₽", "р.", "руб", "поступление", "акция", "скидка", "прайс",
        "для сторис", "для магазина", "промо",
    ]
    photo_markers = [
        "без текста", "без надпис", "без букв", "без цифр",
        "просто фото", "обычная картинка", "сцена", "сюжет", "кадр",
        "сделай картинку", "сделай фото", "нарисуй",
    ]
    if any(m in t for m in photo_markers) and not any(m in t for m in poster_markers):
        return ("PHOTO", "photo_markers")

    if any(m in t for m in poster_markers):
        return ("POSTER", "poster_markers")

    # Если нет явных маркеров — спросим классификатором
    out = await openai_chat_answer(
        user_text=raw,
        system_prompt=VISUAL_ROUTER_SYSTEM_PROMPT,
        image_bytes=None,
        temperature=0.0,
        max_tokens=120,
    )
    try:
        data = json.loads(out)
        mode = str(data.get("mode", "")).strip().upper()
        reason = str(data.get("reason", "")).strip()[:120]
        if mode not in ("POSTER", "PHOTO"):
            mode = "PHOTO"  # безопаснее: не навязывать афишу
        return (mode, reason or "model_router")
    except Exception:
        return ("PHOTO", "router_parse_fail")


async def openai_check_poster_overlay_text(
    image_bytes: bytes,
    headline: str,
    price: str,
) -> Dict[str, Any]:
    """
    Проверка: не добавила ли модель лишние крупные рекламные фразы (вроде "максимум вкуса", "акция", "хит"),
    и не напечатала ли неверные цифры/цену.
    ВАЖНО: игнорируем текст на упаковке товара (бренд/вкус), проверяем только "добавленный" крупный оверлей/заголовки.
    """
    sys = (
        "Ты проверяешь рекламную афишу.\n"
        "Тебе нужно проверить ТОЛЬКО добавленный крупный текст-оверлей (заголовки/плашки/бейджи), "
        "а НЕ текст, который напечатан на упаковке товара.\n\n"
        "Разрешённый оверлей-текст:\n"
        f"• HEADLINE: {headline}\n"
        + (f"• PRICE: {price}\n" if price else "• PRICE: (отсутствует)\n")
        + "\n"
        "Запрещено:\n"
        "• Любые другие слова/фразы/слоганы (например: АКЦИЯ, СКИДКА, ХИТ, НОВИНКА, МАКСИМУМ ВКУСА и т.п.).\n"
        "• Любые дополнительные цифры/цены/₽ кроме разрешённой цены.\n\n"
        "Верни строго JSON:\n"
        "{\"ok\":true|false,\"extra_text\":\"...\",\"notes\":\"...\"}\n"
    )
    out = await openai_chat_answer(
        user_text="Проверь оверлей-текст на афише по правилам и верни JSON.",
        system_prompt=sys,
        image_bytes=image_bytes,
        temperature=0.0,
        max_tokens=220,
    )
    try:
        data = json.loads(out)
        ok = bool(data.get("ok", False))
        extra_text = str(data.get("extra_text", "")).strip()
        notes = str(data.get("notes", "")).strip()
        return {"ok": ok, "extra_text": extra_text, "notes": notes}
    except Exception:
        # если не распарсили — не блокируем, просто считаем ok=true
        return {"ok": True, "extra_text": "", "notes": "parse_fail"}


# ---------------- Webhook handler ----------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/webhook/{secret}")
async def webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        return Response(status_code=403)

    _cleanup_state()
    update = await request.json()

    update_id = update.get("update_id")
    if isinstance(update_id, int):
        if update_id in PROCESSED_UPDATES:
            return {"ok": True}
        PROCESSED_UPDATES[update_id] = _now()

    message = update.get("message") or update.get("edited_message")
    if not message:
        return {"ok": True}

    chat = message.get("chat", {})
    chat_id = int(chat.get("id") or 0)

    from_user = message.get("from") or {}
    user_id = int(from_user.get("id") or 0)

    if not chat_id or not user_id:
        return {"ok": True}

    message_id = int(message.get("message_id") or 0)
    if message_id:
        key = (chat_id, message_id)
        if key in PROCESSED_MESSAGES:
            return {"ok": True}
        PROCESSED_MESSAGES[key] = _now()

    st = _ensure_state(chat_id, user_id)

    # ✅ Telegram: текст может быть в caption
    incoming_text = (message.get("text") or message.get("caption") or "").strip()

    # /start
    if incoming_text.startswith("/start"):
        _set_mode(chat_id, user_id, "chat")
        await tg_send_message(
            chat_id,
            "Привет!\n"
            "Режимы:\n"
            "• «ИИ (чат)» — вопросы/анализ фото/решение задач.\n"
            "• «Фото/Афиши» — делаю афишу ИЛИ обычный фото-эдит (по твоему тексту).\n",
            reply_markup=_main_menu_keyboard(),
        )
        return {"ok": True}

    if incoming_text == "ИИ (чат)":
        _set_mode(chat_id, user_id, "chat")
        await tg_send_message(chat_id, "Ок. Режим «ИИ (чат)».", reply_markup=_main_menu_keyboard())
        return {"ok": True}

    if incoming_text == "Фото/Афиши":
        _set_mode(chat_id, user_id, "poster")
        await tg_send_message(
            chat_id,
            "Режим «Фото/Афиши».\n"
            "1) Пришли фото.\n"
            "2) Потом одним сообщением:\n"
            "   • если хочешь афишу — напиши надпись/цену/стиль (или слово 'афиша')\n"
            "   • если хочешь обычную картинку — просто опиши сцену (или напиши 'без текста').\n",
            reply_markup=_main_menu_keyboard(),
        )
        return {"ok": True}

    if incoming_text == "Помощь":
        await tg_send_message(
            chat_id,
            "• ИИ (чат): фото + подпись 'реши задачу' — решу.\n"
            "• Фото/Афиши: фото → потом текст.\n"
            "  — если просишь надпись/цену/афишу → сделаю афишу\n"
            "  — если описываешь сцену / пишешь 'без текста' → сделаю обычный фото-эдит\n"
            "  — в обычном фото-эдите бот автоматически ограничивает правку маской, чтобы фон не менялся\n",
            reply_markup=_main_menu_keyboard(),
        )
        return {"ok": True}

    # ---------------- Фото (photo) ----------------
    photos = message.get("photo") or []
    if photos:
        largest = photos[-1]
        file_id = largest.get("file_id")
        if not file_id:
            await tg_send_message(chat_id, "Не смог прочитать file_id. Отправь фото ещё раз.", reply_markup=_main_menu_keyboard())
            return {"ok": True}

        try:
            file_path = await tg_get_file_path(file_id)
            img_bytes = await tg_download_file_bytes(file_path)
        except Exception as e:
            await tg_send_message(chat_id, f"Ошибка при загрузке фото: {e}", reply_markup=_main_menu_keyboard())
            return {"ok": True}

        # VISUAL mode
        if st.get("mode") == "poster":
            st["poster"] = {"step": "need_prompt", "photo_bytes": img_bytes}
            st["ts"] = _now()
            await tg_send_message(
                chat_id,
                "Фото получил. Теперь одним сообщением напиши:\n"
                "• для афиши: надпись/цена/стиль (или слово 'афиша')\n"
                "• для обычной картинки: опиши сцену (или 'без текста').",
                reply_markup=_main_menu_keyboard()
            )
            return {"ok": True}

        # CHAT mode
        if _is_math_request(incoming_text) or _infer_intent_from_text(incoming_text) == "math":
            prompt = incoming_text if incoming_text else "Реши задачу с картинки. Дай решение по шагам и строку 'Ответ: ...'."
            answer = await openai_chat_answer(
                user_text=prompt,
                system_prompt=UNICODE_MATH_SYSTEM_PROMPT,
                image_bytes=img_bytes,
                temperature=0.3,
                max_tokens=900,
            )
            await tg_send_message(chat_id, answer, reply_markup=_main_menu_keyboard())
            return {"ok": True}

        await tg_send_message(chat_id, "Фото получил. Анализирую...", reply_markup=_main_menu_keyboard())
        prompt = incoming_text if incoming_text else VISION_DEFAULT_USER_PROMPT
        answer = await openai_chat_answer(
            user_text=prompt,
            system_prompt=VISION_GENERAL_SYSTEM_PROMPT,
            image_bytes=img_bytes,
            temperature=0.4,
            max_tokens=700,
        )
        await tg_send_message(chat_id, answer, reply_markup=_main_menu_keyboard())
        return {"ok": True}

    # ---------------- Фото (document image/*) ----------------
    doc = message.get("document") or {}
    if doc:
        mime = (doc.get("mime_type") or "").lower()
        file_id = doc.get("file_id")
        if file_id and mime.startswith("image/"):
            try:
                file_path = await tg_get_file_path(file_id)
                img_bytes = await tg_download_file_bytes(file_path)
            except Exception as e:
                await tg_send_message(chat_id, f"Ошибка при загрузке фото: {e}", reply_markup=_main_menu_keyboard())
                return {"ok": True}

            if st.get("mode") == "poster":
                st["poster"] = {"step": "need_prompt", "photo_bytes": img_bytes}
                st["ts"] = _now()
                await tg_send_message(
                    chat_id,
                    "Фото получил. Теперь одним сообщением напиши:\n"
                    "• для афиши: надпись/цена/стиль (или слово 'афиша')\n"
                    "• для обычной картинки: опиши сцену (или 'без текста').",
                    reply_markup=_main_menu_keyboard()
                )
                return {"ok": True}

            if _is_math_request(incoming_text) or _infer_intent_from_text(incoming_text) == "math":
                prompt = incoming_text if incoming_text else "Реши задачу с картинки. Дай решение по шагам и строку 'Ответ: ...'."
                answer = await openai_chat_answer(
                    user_text=prompt,
                    system_prompt=UNICODE_MATH_SYSTEM_PROMPT,
                    image_bytes=img_bytes,
                    temperature=0.3,
                    max_tokens=900,
                )
                await tg_send_message(chat_id, answer, reply_markup=_main_menu_keyboard())
                return {"ok": True}

            await tg_send_message(chat_id, "Фото получил. Анализирую...", reply_markup=_main_menu_keyboard())
            prompt = incoming_text if incoming_text else VISION_DEFAULT_USER_PROMPT
            answer = await openai_chat_answer(
                user_text=prompt,
                system_prompt=VISION_GENERAL_SYSTEM_PROMPT,
                image_bytes=img_bytes,
                temperature=0.4,
                max_tokens=700,
            )
            await tg_send_message(chat_id, answer, reply_markup=_main_menu_keyboard())
            return {"ok": True}

    # ---------------- Текст без фото ----------------
    if incoming_text:
        # VISUAL flow (poster mode): после фото -> роутинг POSTER/PHOTO
        if st.get("mode") == "poster":
            poster = st.get("poster") or {}
            step: PosterStep = poster.get("step") or "need_photo"
            photo_bytes = poster.get("photo_bytes")

            if step == "need_photo" or not photo_bytes:
                await tg_send_message(chat_id, "Сначала пришли фото.", reply_markup=_main_menu_keyboard())
                return {"ok": True}

            if step == "need_prompt":
                mode, _reason = await openai_route_visual_mode(incoming_text)

                if mode == "POSTER":
                    await tg_send_message(chat_id, "Делаю афишу на основе твоего фото...")
                    try:
                        spec = await openai_extract_poster_spec(incoming_text)
                        prompt1 = _poster_prompt_from_spec(spec, extra_strict=False)
                        out_bytes = await openai_edit_image(photo_bytes, prompt1, IMG_SIZE_DEFAULT, mask_png_bytes=None)

                        # Проверка на лишний оверлей-текст (одна попытка рефайна)
                        check = await openai_check_poster_overlay_text(
                            image_bytes=out_bytes,
                            headline=(spec.get("headline") or "").strip(),
                            price=(spec.get("price") or "").strip(),
                        )
                        if not bool(check.get("ok", True)):
                            # Вторая попытка: супер-строгий промпт
                            prompt2 = _poster_prompt_from_spec(spec, extra_strict=True)
                            out_bytes = await openai_edit_image(photo_bytes, prompt2, IMG_SIZE_DEFAULT, mask_png_bytes=None)

                        await tg_send_photo_bytes(chat_id, out_bytes, caption="Готово (афиша).")
                    except Exception as e:
                        await tg_send_message(chat_id, f"Не получилось сгенерировать афишу: {e}")

                else:
                    # PHOTO: авто-маска по зоне + санитизация IP-слов
                    safe_text = _sanitize_ip_terms_for_image(incoming_text)

                    strict = _wants_strict_preserve(safe_text)
                    zone = _infer_zone_from_text(safe_text)
                    mask_png = _build_zone_mask_png(photo_bytes, zone)  # может быть None (fallback)
                    prompt = _photo_edit_prompt(safe_text, strict=strict)

                    await tg_send_message(
                        chat_id,
                        f"Делаю обычный фото-эдит (без текста). Зона: {zone}. "
                        + ("Фон максимально сохраняю..." if strict else "...")
                    )
                    try:
                        out_bytes = await openai_edit_image(photo_bytes, prompt, IMG_SIZE_DEFAULT, mask_png_bytes=mask_png)
                        await tg_send_photo_bytes(chat_id, out_bytes, caption="Готово (без текста).")
                    except Exception as e:
                        if _is_moderation_blocked_error(e):
                            await tg_send_message(
                                chat_id,
                                "Запрос отклонён модерацией (часто из-за упоминания известных персонажей/брендов).\n"
                                "Попробуй без имени, например:\n"
                                "«Добавь человека в тёмном костюме в маске, без логотипов, фон не менять.»"
                            )
                        else:
                            await tg_send_message(chat_id, f"Не получилось сгенерировать картинку: {e}")

                # reset
                st["poster"] = {"step": "need_photo", "photo_bytes": None}
                st["ts"] = _now()
                return {"ok": True}

            await tg_send_message(chat_id, "Пришли фото, затем одним сообщением текст.", reply_markup=_main_menu_keyboard())
            return {"ok": True}

        # CHAT: обычный текстовый ответ
        answer = await openai_chat_answer(
            user_text=incoming_text,
            system_prompt=DEFAULT_TEXT_SYSTEM_PROMPT,
            image_bytes=None,
            temperature=0.6,
            max_tokens=700,
        )
        await tg_send_message(chat_id, answer, reply_markup=_main_menu_keyboard())
        return {"ok": True}

    await tg_send_message(chat_id, "Я понимаю текст и фото. Выбери режим в меню снизу.", reply_markup=_main_menu_keyboard())
    return {"ok": True}
