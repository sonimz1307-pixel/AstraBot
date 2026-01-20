import os
import base64
import time
import re
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

# Anti-duplicate updates (idempotency)
PROCESSED_TTL_SECONDS = int(os.getenv("PROCESSED_TTL_SECONDS", "1800"))  # 30 минут
# key: (chat_id, message_id) -> ts
PROCESSED: Dict[Tuple[int, int], float] = {}

PosterStep = Literal["need_photo", "need_prompt"]


def _now() -> float:
    return time.time()


def _cleanup_state():
    now = _now()

    # state cleanup
    expired = []
    for k, v in STATE.items():
        ts = float(v.get("ts", 0))
        if now - ts > STATE_TTL_SECONDS:
            expired.append(k)
    for k in expired:
        STATE.pop(k, None)

    # processed cleanup
    expired_p = []
    for k, ts in PROCESSED.items():
        if now - float(ts) > PROCESSED_TTL_SECONDS:
            expired_p.append(k)
    for k in expired_p:
        PROCESSED.pop(k, None)


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
    "Если нужна математика — пиши формулы обычным текстом, можно с символом π."
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

    files = {
        "image": (f"source.{ext}", source_image_bytes, mime),
    }
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


# ---------------- Intent (для режима ИИ) ----------------

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


# ---------------- Poster helpers ----------------

def _extract_price_any(text: str) -> str:
    t = (text or "").lower()
    m = re.search(r"(цена\s*)?(\d[\d\s]{1,8})(\s*(р|₽))?", t)
    if not m:
        return ""
    price_num = (m.group(2) or "").replace(" ", "").strip()
    if not price_num:
        return ""
    return f"{price_num}₽"


def _clean_text_without_price(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"(цена\s*)?(\d[\d\s]{1,8})(\s*(р|₽))?", "", text, flags=re.IGNORECASE).strip(" \n,.-")


def _poster_prompt_from_user_text(user_text: str) -> str:
    """
    ВАЖНО: больше никаких "АКЦИЯ" и выдуманных цен.
    Используем только то, что дал пользователь.
    """
    ut = (user_text or "").strip()
    if not ut:
        ut = "Сделай красивую рекламную афишу. Текст должен быть читабельным."

    price = _extract_price_any(ut)
    cleaned = _clean_text_without_price(ut)

    # Текст, который разрешено наносить
    allowed_text = ut.strip()

    # Явные правила, чтобы модель не придумывала "АКЦИЯ" и не рисовала цену из головы
    price_rule = (
        f"Цена указана пользователем: {price}. Добавь её один раз, крупно.\n"
        if price
        else "Пользователь НЕ указал цену. Запрещено добавлять любые цены, цифры и валюту.\n"
    )

    return (
        "Сделай профессиональную рекламную афишу / промо-баннер на основе предоставленного фото.\n\n"

        "КРИТИЧЕСКИ ВАЖНО — СОХРАНЕНИЕ ЛЮДЕЙ (ЕСЛИ ОНИ ЕСТЬ НА ФОТО):\n"
        "• Сохранить всех людей на фото 1:1.\n"
        "• НЕ изменять лица, черты лица, возраст, мимику, форму головы, причёски.\n"
        "• НЕ менять одежду людей.\n"
        "• НЕ добавлять новых людей и персонажей.\n"
        "• Только фотореализм, как исходная фотография.\n"
        "• Разрешено менять ТОЛЬКО: фон, атмосферу, свет, декор, цветокор и добавить текст.\n\n"

        "ТОВАР (если есть) должен остаться максимально реалистичным и узнаваемым: "
        "НЕ менять бренд, упаковку, форму, цвета товара, логотипы.\n\n"

        "ТЕКСТ НА АФИШЕ (СТРОГОЕ ПРАВИЛО):\n"
        "• Используй ТОЛЬКО текст, который дал пользователь.\n"
        "• Запрещено добавлять слова от себя (например: 'АКЦИЯ', 'СКИДКА', 'ХИТ', 'НОВИНКА'), "
        "если пользователь это не написал.\n"
        "• Запрещено добавлять любые цифры, если пользователь их не написал.\n"
        "• Запрещено выдумывать цену.\n"
        + price_rule +
        "• Не дублируй один и тот же текст много раз.\n\n"

        "ТИПОГРАФИКА:\n"
        "• Сделай текст дорогим и читабельным на смартфоне.\n"
        "• Чёткая иерархия: главный текст → второстепенный.\n"
        "• Можно использовать плашки/ленты/тени/обводку, но без перегруза.\n"
        "• Текст не перекрывает ключевые элементы товара.\n\n"

        "ТЕКСТ ПОЛЬЗОВАТЕЛЯ (ИСПОЛЬЗУЙ ЕГО ДОСЛОВНО):\n"
        f"{allowed_text}\n\n"

        "КОМПОЗИЦИЯ:\n"
        "• Главный объект (товар/люди) — в центре внимания.\n"
        "• Стиль и атмосфера соответствуют запросу пользователя (эко/неон/премиум и т.д.).\n\n"

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

    message = update.get("message") or update.get("edited_message")
    if not message:
        return {"ok": True}

    chat = message.get("chat", {})
    chat_id = int(chat.get("id") or 0)

    from_user = message.get("from") or {}
    user_id = int(from_user.get("id") or 0)

    if not chat_id or not user_id:
        return {"ok": True}

    # Idempotency: ignore duplicates by message_id per chat
    message_id = int(message.get("message_id") or 0)
    if message_id:
        key = (chat_id, message_id)
        if key in PROCESSED:
            return {"ok": True}
        PROCESSED[key] = _now()

    st = _ensure_state(chat_id, user_id)

    text = (message.get("text") or "").strip()

    # /start
    if text.startswith("/start"):
        _set_mode(chat_id, user_id, "chat")
        await tg_send_message(
            chat_id,
            "Привет!\n"
            "У меня есть два режима:\n"
            "1) «ИИ (чат)» — отвечаю на вопросы, могу анализировать фото (машина/цветок/товар) и решать задачи.\n"
            "2) «Фото/Афиши» — делаю рекламную афишу НА ОСНОВЕ твоего фото товара.\n\n"
            "Нажми кнопку внизу, чтобы выбрать режим.",
            reply_markup=_main_menu_keyboard(),
        )
        return {"ok": True}

    if text == "ИИ (чат)":
        _set_mode(chat_id, user_id, "chat")
        await tg_send_message(chat_id, "Ок. Режим «ИИ (чат)». Пиши вопрос или пришли фото для анализа.", reply_markup=_main_menu_keyboard())
        return {"ok": True}

    if text == "Фото/Афиши":
        _set_mode(chat_id, user_id, "poster")
        await tg_send_message(
            chat_id,
            "Режим «Фото/Афиши».\n"
            "1) Пришли фото товара.\n"
            "2) Потом одним сообщением напиши ТЕКСТ для афиши + цену (если надо) + любые пожелания к стилю.\n"
            "Примеры:\n"
            "• Каперсы, 299₽, стиль эко, красиво\n"
            "• Pasito III в наличии, 1500₽, премиум, черный фон, золото\n",
            reply_markup=_main_menu_keyboard(),
        )
        return {"ok": True}

    if text == "Помощь":
        await tg_send_message(
            chat_id,
            "Как пользоваться:\n"
            "• ИИ (чат): пиши текст — отвечу; пришли фото + вопрос — опишу/попробую определить или решу задачу.\n"
            "• Фото/Афиши: нажми «Фото/Афиши», пришли фото товара, затем одним сообщением напиши текст/цену/стиль.\n",
            reply_markup=_main_menu_keyboard(),
        )
        return {"ok": True}

    # ---------------- Фото (photo) ----------------
    photos = message.get("photo") or []
    if photos:
        largest = photos[-1]
        file_id = largest.get("file_id")
        if not file_id:
            await tg_send_message(chat_id, "Фото получил, но не смог прочитать file_id. Попробуй отправить ещё раз.", reply_markup=_main_menu_keyboard())
            return {"ok": True}

        # POSTER
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
                "Фото получил.\n"
                "Теперь одним сообщением напиши текст для афиши + цену (если нужна) + стиль.",
                reply_markup=_main_menu_keyboard(),
            )
            return {"ok": True}

        # CHAT
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

    # ---------------- Фото (document) ----------------
    doc = message.get("document") or {}
    if doc:
        mime = (doc.get("mime_type") or "").lower()
        file_id = doc.get("file_id")
        if file_id and mime.startswith("image/"):
            # POSTER
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
                    "Фото получил.\n"
                    "Теперь одним сообщением напиши текст для афиши + цену (если нужна) + стиль.",
                    reply_markup=_main_menu_keyboard(),
                )
                return {"ok": True}

            # CHAT
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
        # POSTER: ждём ТЗ после фото
        if st.get("mode") == "poster":
            poster = st.get("poster") or {}
            step: PosterStep = poster.get("step") or "need_photo"
            photo_bytes = poster.get("photo_bytes")

            if step == "need_photo" or not photo_bytes:
                await tg_send_message(chat_id, "Сначала пришли фото товара.", reply_markup=_main_menu_keyboard())
                return {"ok": True}

            if step == "need_prompt":
                await tg_send_message(chat_id, "Делаю афишу на основе твоего фото...")

                prompt = _poster_prompt_from_user_text(text)
                try:
                    out_bytes = await openai_edit_image_make_poster(
                        source_image_bytes=photo_bytes,
                        prompt=prompt,
                        size=IMG_SIZE_DEFAULT,
                    )
                    await tg_send_photo_bytes(
                        chat_id,
                        out_bytes,
                        caption="Готово. Если нужно — пришли новое фото или снова напиши текст/стиль.",
                    )
                except Exception as e:
                    await tg_send_message(chat_id, f"Не получилось сгенерировать афишу: {e}")

                st["poster"] = {"step": "need_photo", "photo_bytes": None}
                st["ts"] = _now()
                return {"ok": True}

            await tg_send_message(chat_id, "Пришли фото товара, затем одним сообщением текст/цену/стиль.", reply_markup=_main_menu_keyboard())
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
