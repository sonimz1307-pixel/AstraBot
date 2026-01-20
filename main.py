import os
import base64
import time
import re
import json
from typing import Optional, Literal, Dict, Any, Tuple

import httpx
from fastapi import FastAPI, Request, Response

app = FastAPI()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change_me")

# Вертикальный формат под сторис/афиши (можно поменять на 1024x1024)
IMG_SIZE_DEFAULT = os.getenv("IMG_SIZE_DEFAULT", "1024x1536")

TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# ---------------- In-memory state ----------------
STATE_TTL_SECONDS = int(os.getenv("STATE_TTL_SECONDS", "1800"))  # 30 минут
STATE: Dict[Tuple[int, int], Dict[str, Any]] = {}

PosterStep = Literal["need_photo", "need_prompt"]

# Anti-duplicate (idempotency)
PROCESSED_TTL_SECONDS = int(os.getenv("PROCESSED_TTL_SECONDS", "1800"))  # 30 минут
# key -> ts
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


async def openai_edit_image_make_poster(
    source_image_bytes: bytes,
    prompt: str,
    size: str,
) -> bytes:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY не задан в переменных окружения.")

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}

    ext, mime = _detect_image_type(source_image_bytes)

    files = {"image": (f"source.{ext}", source_image_bytes, mime)}
    data = {
        "model": "gpt-image-1",
        "prompt": prompt,
        "size": size,
        "n": "1",
    }

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


# ---------------- Poster: parsing helpers ----------------

def _extract_price_any(text: str) -> str:
    t = (text or "").lower()
    m = re.search(r"(цена\s*)?(\d[\d\s]{1,8})(\s*(р|₽))?", t)
    if not m:
        return ""
    price_num = (m.group(2) or "").replace(" ", "").strip()
    if not price_num:
        return ""
    return f"{price_num}₽"


async def openai_extract_poster_spec(user_text: str) -> Dict[str, str]:
    """
    1) Разбираем одно сообщение пользователя на:
       headline (что печатать), style (как оформить), price (если есть).
    2) Возвращаем dict: {"headline": "...", "style": "...", "price": "..."}.
    """
    # Быстрый эвристический fallback:
    # если есть "надпись:" — берём всё после него как headline.
    raw = (user_text or "").strip()
    if not raw:
        return {"headline": "", "style": "", "price": ""}

    price = _extract_price_any(raw)

    low = raw.lower()
    if "надпись" in low:
        # надпись: ....
        m = re.search(r"надпись\s*[:\-]\s*(.+)$", raw, flags=re.IGNORECASE | re.DOTALL)
        if m:
            headline = m.group(1).strip().strip('"“”')
            # стиль — всё до "надпись"
            style_part = re.split(r"надпись\s*[:\-]", raw, flags=re.IGNORECASE)[0].strip()
            return {"headline": headline, "style": style_part, "price": price}

    # GPT-разбор (надежнее семантически)
    sys = (
        "Ты парсер для рекламных афиш.\n"
        "Нужно отделить: (1) текст, который надо НАПЕЧАТАТЬ на афише (headline), "
        "(2) пожелания к стилю/оформлению (style), (3) цену (price).\n\n"
        "Правила:\n"
        "• headline — это короткая надпись/слоган/название товара. "
        "Не включай в headline фразы-инструкции: 'сделай', 'оформи', 'красиво', 'в стиле', 'хочу', 'нужно'.\n"
        "• style — это всё про настроение, стиль, цвета, сезон, фон, 'в стиле эко/неон/премиум' и т.п.\n"
        "• price — только если пользователь явно указал цифры/валюту. Иначе пусто.\n"
        "• Верни СТРОГО JSON без пояснений.\n"
        "Формат: {\"headline\":\"...\",\"style\":\"...\",\"price\":\"...\"}\n"
    )
    user = f"Текст пользователя:\n{raw}"

    out = await openai_chat_answer(
        user_text=user,
        system_prompt=sys,
        image_bytes=None,
        temperature=0.0,
        max_tokens=250,
    )

    try:
        data = json.loads(out)
        headline = str(data.get("headline", "")).strip()
        style = str(data.get("style", "")).strip()
        price2 = str(data.get("price", "")).strip()
        # если GPT не выдал price, оставляем эвристику
        if not price2 and price:
            price2 = price
        return {"headline": headline, "style": style, "price": price2}
    except Exception:
        # fallback: считаем, что весь текст — это стиль, а headline пустой
        return {"headline": "", "style": raw, "price": price}


def _poster_prompt_from_spec(spec: Dict[str, str]) -> str:
    """
    Собираем промпт для gpt-image-1.
    ВАЖНО: печатаем только headline и (опционально) price.
    """
    headline = (spec.get("headline") or "").strip()
    style = (spec.get("style") or "").strip()
    price = (spec.get("price") or "").strip()

    # если headline пустой — используем минимальный безопасный вариант:
    # печатать только слово из товара (если юзер вообще ничего не дал)
    if not headline:
        headline = " "

    # Правила по цене/цифрам
    if price:
        price_rule = f"Цена разрешена пользователем: {price}. Добавь цену ОДИН раз, крупно, без изменения цифр."
        digits_rule = "Цифры разрешены ТОЛЬКО в цене и только как указано пользователем."
    else:
        price_rule = "Пользователь НЕ указал цену. Запрещено добавлять любые цены, валюту и любые цифры."
        digits_rule = "Запрещено добавлять любые цифры."

    return (
        "Сделай профессиональную рекламную афишу / промо-баннер на основе предоставленного фото.\n\n"

        "КРИТИЧЕСКИ ВАЖНО — СОХРАНЕНИЕ ЛЮДЕЙ (ЕСЛИ ОНИ ЕСТЬ НА ФОТО):\n"
        "• Сохранить всех людей на фото 1:1.\n"
        "• НЕ изменять лица, черты лица, возраст, мимику, форму головы, причёски.\n"
        "• НЕ менять одежду людей.\n"
        "• НЕ добавлять новых людей и персонажей.\n"
        "• Только фотореализм, как исходная фотография.\n"
        "• Разрешено менять ТОЛЬКО: фон, атмосферу, свет, декор, цветокор и добавить текст.\n\n"

        "ТОВАР (если есть) должен остаться максимально реалистичным и узнаваемым:\n"
        "• НЕ менять бренд, упаковку, форму, цвета товара, логотипы.\n\n"

        "ТЕКСТ НА АФИШЕ (СТРОГОЕ ПРАВИЛО):\n"
        "• На афише печатай ТОЛЬКО разрешённый headline (и цену, если разрешена).\n"
        "• Запрещено добавлять слова от себя: 'АКЦИЯ', 'СКИДКА', 'ХИТ', 'НОВИНКА' и любые другие.\n"
        f"• {digits_rule}\n"
        f"• {price_rule}\n"
        "• НЕ печатай фразы-инструкции и стиль (например: 'сделай красиво', 'в стиле эко').\n"
        "• НЕ меняй написание букв в headline: без подмены слов, без добавления слов.\n\n"

        "ТИПОГРАФИКА (КАК В ДОРОГИХ ПРОМО-АФИШАХ):\n"
        "• Заголовок сделай максимально выразительным: крупный, жирный, рекламный.\n"
        "• Псевдо-3D/эмбосс (легкий), аккуратно, без искажений букв.\n"
        "• Контрастная обводка + мягкая тень.\n"
        "• Лёгкое свечение допустимо, но аккуратно.\n"
        "• Можно использовать ленту/плашку под заголовок.\n"
        "• Текст должен читаться мгновенно на смартфоне.\n"
        "• НЕ делай плоский 'обычный' шрифт.\n\n"

        "РАЗМЕСТИ ТЕКСТ:\n"
        f"HEADLINE (печатать): {headline}\n"
        + (f"PRICE (печатать): {price}\n" if price else "PRICE: (не печатать)\n") +
        "\n"

        "СТИЛЬ/АТМОСФЕРА (НЕ ПЕЧАТАТЬ КАК ТЕКСТ, ТОЛЬКО ДЛЯ ОФОРМЛЕНИЯ):\n"
        f"{style if style else 'Сделай визуально красиво, современно, чисто, без перегруза.'}\n\n"

        "КОМПОЗИЦИЯ:\n"
        "• Главный объект (товар/люди) — в центре внимания.\n"
        "• Текст не перекрывает ключевые элементы.\n"
        "• Свет и цвет как в рекламной или студийной съёмке.\n\n"

        "ФОРМАТ:\n"
        "• Вертикальный, под сторис.\n"
        "• Высокое качество.\n"
    )


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

    # idempotency by update_id
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

    # idempotency by message_id (per chat)
    message_id = int(message.get("message_id") or 0)
    if message_id:
        key = (chat_id, message_id)
        if key in PROCESSED_MESSAGES:
            return {"ok": True}
        PROCESSED_MESSAGES[key] = _now()

    st = _ensure_state(chat_id, user_id)
    text = (message.get("text") or "").strip()

    # /start
    if text.startswith("/start"):
        _set_mode(chat_id, user_id, "chat")
        await tg_send_message(
            chat_id,
            "Привет!\n"
            "У меня есть два режима:\n"
            "1) «ИИ (чат)» — отвечаю на вопросы, могу анализировать фото и решать задачи.\n"
            "2) «Фото/Афиши» — делаю рекламную афишу НА ОСНОВЕ твоего фото.\n\n"
            "Нажми кнопку внизу, чтобы выбрать режим.",
            reply_markup=_main_menu_keyboard(),
        )
        return {"ok": True}

    if text == "ИИ (чат)":
        _set_mode(chat_id, user_id, "chat")
        await tg_send_message(
            chat_id,
            "Ок. Режим «ИИ (чат)». Пиши вопрос или пришли фото + запрос.",
            reply_markup=_main_menu_keyboard(),
        )
        return {"ok": True}

    if text == "Фото/Афиши":
        _set_mode(chat_id, user_id, "poster")
        await tg_send_message(
            chat_id,
            "Режим «Фото/Афиши».\n"
            "1) Пришли фото товара.\n"
            "2) Потом одним сообщением напиши: надпись/слоган + (цена, если нужна) + стиль.\n"
            "Пример:\n"
            "Сделай красиво в стиле эко. Надпись: КАПЕРСЫ — БОМБА, ЧЕСТНО ГОВОРЯ. Цена 299₽\n",
            reply_markup=_main_menu_keyboard(),
        )
        return {"ok": True}

    if text == "Помощь":
        await tg_send_message(
            chat_id,
            "Как пользоваться:\n"
            "• ИИ (чат): пиши текст — отвечу; пришли фото + вопрос — опишу/определю или решу задачу.\n"
            "• Фото/Афиши: нажми «Фото/Афиши», пришли фото, затем одним сообщением надпись/цену/стиль.\n",
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

        # POSTER mode: сохраняем фото и ждём один текст
        if st.get("mode") == "poster":
            try:
                file_path = await tg_get_file_path(file_id)
                img_bytes = await tg_download_file_bytes(file_path)
            except Exception as e:
                await tg_send_message(chat_id, f"Ошибка при загрузке фото: {e}", reply_markup=_main_menu_keyboard())
                return {"ok": True}

            st["poster"] = {"step": "need_prompt", "photo_bytes": img_bytes}
            st["ts"] = _now()

            await tg_send_message(
                chat_id,
                "Фото получил. Теперь одним сообщением напиши надпись/цену/стиль.",
                reply_markup=_main_menu_keyboard(),
            )
            return {"ok": True}

        # CHAT mode: решаем математику без "анализирую" (если это запрос на математику)
        try:
            file_path = await tg_get_file_path(file_id)
            img_bytes = await tg_download_file_bytes(file_path)
        except Exception as e:
            await tg_send_message(chat_id, f"Ошибка при загрузке фото: {e}", reply_markup=_main_menu_keyboard())
            return {"ok": True}

        if _is_math_request(text) or _infer_intent_from_text(text) == "math":
            prompt = text if text else "Реши задачу с картинки. Дай решение по шагам и строку 'Ответ: ...'."
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
        prompt = text if text else VISION_DEFAULT_USER_PROMPT
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
            if st.get("mode") == "poster":
                try:
                    file_path = await tg_get_file_path(file_id)
                    img_bytes = await tg_download_file_bytes(file_path)
                except Exception as e:
                    await tg_send_message(chat_id, f"Ошибка при загрузке фото: {e}", reply_markup=_main_menu_keyboard())
                    return {"ok": True}

                st["poster"] = {"step": "need_prompt", "photo_bytes": img_bytes}
                st["ts"] = _now()

                await tg_send_message(
                    chat_id,
                    "Фото получил. Теперь одним сообщением напиши надпись/цену/стиль.",
                    reply_markup=_main_menu_keyboard(),
                )
                return {"ok": True}

            # CHAT mode
            try:
                file_path = await tg_get_file_path(file_id)
                img_bytes = await tg_download_file_bytes(file_path)
            except Exception as e:
                await tg_send_message(chat_id, f"Ошибка при загрузке фото: {e}", reply_markup=_main_menu_keyboard())
                return {"ok": True}

            if _is_math_request(text) or _infer_intent_from_text(text) == "math":
                prompt = text if text else "Реши задачу с картинки. Дай решение по шагам и строку 'Ответ: ...'."
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
            prompt = text if text else VISION_DEFAULT_USER_PROMPT
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
    if text:
        # POSTER: ждём текст после фото -> сначала извлекаем spec -> потом генерим
        if st.get("mode") == "poster":
            poster = st.get("poster") or {}
            step: PosterStep = poster.get("step") or "need_photo"
            photo_bytes = poster.get("photo_bytes")

            if step == "need_photo" or not photo_bytes:
                await tg_send_message(chat_id, "Сначала пришли фото товара.", reply_markup=_main_menu_keyboard())
                return {"ok": True}

            if step == "need_prompt":
                await tg_send_message(chat_id, "Делаю афишу на основе твоего фото...")

                try:
                    spec = await openai_extract_poster_spec(text)
                    prompt = _poster_prompt_from_spec(spec)

                    out_bytes = await openai_edit_image_make_poster(
                        source_image_bytes=photo_bytes,
                        prompt=prompt,
                        size=IMG_SIZE_DEFAULT,
                    )
                    await tg_send_photo_bytes(
                        chat_id,
                        out_bytes,
                        caption="Готово. Если нужно — пришли новое фото или снова напиши надпись/цену/стиль.",
                    )
                except Exception as e:
                    await tg_send_message(chat_id, f"Не получилось сгенерировать афишу: {e}")

                # Сброс — ждём следующее фото
                st["poster"] = {"step": "need_photo", "photo_bytes": None}
                st["ts"] = _now()
                return {"ok": True}

            await tg_send_message(chat_id, "Пришли фото товара, затем одним сообщением надпись/цену/стиль.", reply_markup=_main_menu_keyboard())
            return {"ok": True}

        # CHAT: обычный ответ
        low = text.lower()
        if any(w in low for w in ["афиш", "баннер", "постер", "сделай красиво", "сделай дизайн", "реклам"]):
            await tg_send_message(
                chat_id,
                "Чтобы сделать афишу из фото товара, нажми «Фото/Афиши» и пришли фото.",
                reply_markup=_main_menu_keyboard(),
            )
            return {"ok": True}

        answer = await openai_chat_answer(
            user_text=text,
            system_prompt=DEFAULT_TEXT_SYSTEM_PROMPT,
            image_bytes=None,
            temperature=0.6,
            max_tokens=700,
        )
        await tg_send_message(chat_id, answer, reply_markup=_main_menu_keyboard())
        return {"ok": True}

    await tg_send_message(chat_id, "Я понимаю текст и фото. Выбери режим в меню снизу.", reply_markup=_main_menu_keyboard())
    return {"ok": True}
