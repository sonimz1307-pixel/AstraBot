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

# ---------------- In-memory state (без истории, только текущий режим и черновик афиши) ----------------
# key: (chat_id, user_id) -> {"mode": "chat"|"poster", "ts": float, "poster": {...}}
STATE_TTL_SECONDS = int(os.getenv("STATE_TTL_SECONDS", "1800"))  # 30 минут

STATE: Dict[Tuple[int, int], Dict[str, Any]] = {}

PosterStep = Literal["need_photo", "need_text_price", "need_style", "ready"]


def _now() -> float:
    return time.time()


def _cleanup_state():
    now = _now()
    expired = []
    for k, v in STATE.items():
        ts = float(v.get("ts", 0))
        if now - ts > STATE_TTL_SECONDS:
            expired.append(k)
    for k in expired:
        STATE.pop(k, None)


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
        # сбрасываем черновик афиши
        st["poster"] = {"step": "need_photo", "photo_bytes": None, "title": "", "price": "", "style": "Зима"}


# ---------------- Reply/Inline keyboards ----------------

def _main_menu_keyboard() -> dict:
    # ReplyKeyboardMarkup (нижнее меню)
    return {
        "keyboard": [
            [{"text": "ИИ (чат)"}, {"text": "Фото/Афиши"}],
            [{"text": "Помощь"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False,
        "selective": False,
    }


def _style_inline_keyboard() -> dict:
    # InlineKeyboardMarkup (кнопки стиля)
    return {
        "inline_keyboard": [
            [{"text": "Зима", "callback_data": "poster_style:winter"},
             {"text": "Новый год", "callback_data": "poster_style:newyear"}],
            [{"text": "Минимализм", "callback_data": "poster_style:min"},
             {"text": "Неон", "callback_data": "poster_style:neon"}],
            [{"text": "Отмена", "callback_data": "poster_cancel"}],
        ]
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


async def tg_answer_callback_query(callback_query_id: str, text: Optional[str] = None, show_alert: bool = False):
    if not TELEGRAM_BOT_TOKEN:
        return
    payload = {"callback_query_id": callback_query_id, "show_alert": show_alert}
    if text:
        payload["text"] = text

    async with httpx.AsyncClient(timeout=20) as client:
        await client.post(f"{TELEGRAM_API_BASE}/answerCallbackQuery", json=payload)


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
    "Примеры:\n"
    "x = −2π/3 + 4πk, k ∈ ℤ\n"
    "cos(x/2 + π/3) = 1 ⇒ x/2 + π/3 = 2πk\n\n"
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
    """
    Возвращает (ext, mime).
    Telegram чаще всего присылает JPEG, но бывает PNG/WEBP.
    Чтобы /v1/images/edits не падал, отправляем корректные mime/filename.
    """
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
    """
    Делает афишу на основе исходного фото (image-to-image/edit).
    Использует multipart/form-data: /v1/images/edits
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY не задан в переменных окружения.")

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}

    ext, mime = _detect_image_type(source_image_bytes)

    # endpoint ожидает multipart/form-data
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


# ---------------- Poster helpers ----------------

def _parse_title_price(text: str) -> Tuple[str, str]:
    """
    Простой парсер: ожидаем строку формата:
    Заголовок; Цена
    или
    Заголовок / Цена
    или
    Заголовок, цена 1500
    """
    t = (text or "").strip()
    if not t:
        return "", ""

    # Попытка разделителя ; или /
    for sep in (";", "/"):
        if sep in t:
            left, right = t.split(sep, 1)
            return left.strip(), right.strip()

    # Ищем цену по паттерну 1000, 1000р, 1000₽, цена 1000
    m = re.search(r"(цена\s*)?(\d[\d\s]{1,8})(\s*(р|₽))?", t.lower())
    if m:
        price = m.group(2).replace(" ", "").strip()
        # Заголовок = текст без найденного куска
        title = re.sub(r"(цена\s*)?(\d[\d\s]{1,8})(\s*(р|₽))?", "", t, flags=re.IGNORECASE).strip(" ,.-")
        if price:
            price = f"{price}₽"
        return title.strip(), price.strip()

    # Если не нашли цену — считаем всё заголовком
    return t, ""


def _poster_prompt(title: str, price: str, style: str) -> str:
    # Важно: просим сохранить товар максимально реалистично, а фон/декор/типографику — улучшить.
    # Это best-practice для рекламной афиши.
    title = (title or "").strip()
    price = (price or "").strip()
    style = (style or "Зима").strip()

    # Если нет цены, не заставляем рисовать пустое поле.
    price_line = f"Цена: {price}" if price else ""

    style_directive = {
        "Зима": "Зимний стиль: снег, морозные узоры, прохладные оттенки, но товар остаётся главным.",
        "Новый год": "Новогодний стиль: лёгкие гирлянды/блики, праздничные элементы, снег, ощущение праздника.",
        "Минимализм": "Минимализм: чистый фон, аккуратные акценты, максимум читабельности и воздуха.",
        "Неон": "Неон: яркие неоновые подсветки, современный стиль, контраст, динамика.",
    }.get(style, style)

    # Рекомендуем вертикаль под сторис
    return (
        "Сделай рекламную афишу на основе предоставленного фото товара.\n"
        "Сохрани товар максимально реалистично и узнаваемо (не меняй бренд/упаковку/форму товара).\n"
        "Улучши композицию, свет и фон так, чтобы это выглядело как профессиональная рекламная афиша.\n"
        f"{style_directive}\n"
        "Добавь читаемую типографику и аккуратные декоративные элементы, не перекрывая товар.\n"
        f"Крупный заголовок (сверху или слева): {title if title else 'АКЦИЯ'}\n"
        f"{price_line}\n"
        "Формат: вертикальный, под сторис. Высокое качество.\n"
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

    # 1) callback_query (inline кнопки стиля / отмена)
    callback = update.get("callback_query")
    if callback:
        cb_id = callback.get("id")
        from_user = callback.get("from") or {}
        user_id = int(from_user.get("id") or 0)

        msg = callback.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = int(chat.get("id") or 0)

        data = (callback.get("data") or "").strip()

        if cb_id:
            await tg_answer_callback_query(cb_id)

        if not chat_id or not user_id:
            return {"ok": True}

        st = _ensure_state(chat_id, user_id)

        if data == "poster_cancel":
            _set_mode(chat_id, user_id, "chat")
            await tg_send_message(chat_id, "Ок, отменил. Возвращаюсь в режим «ИИ (чат)».", reply_markup=_main_menu_keyboard())
            return {"ok": True}

        if data.startswith("poster_style:"):
            if st.get("mode") != "poster":
                await tg_send_message(chat_id, "Сначала нажми «Фото/Афиши» и пришли фото товара.", reply_markup=_main_menu_keyboard())
                return {"ok": True}

            poster = st.get("poster") or {}
            if poster.get("step") != "need_style":
                await tg_send_message(chat_id, "Стиль сейчас не требуется. Нажми «Фото/Афиши» и следуй шагам.", reply_markup=_main_menu_keyboard())
                return {"ok": True}

            style_code = data.split(":", 1)[1].strip()
            style = {
                "winter": "Зима",
                "newyear": "Новый год",
                "min": "Минимализм",
                "neon": "Неон",
            }.get(style_code, "Зима")

            poster["style"] = style

            # Проверяем, что всё есть
            photo_bytes = poster.get("photo_bytes")
            title = (poster.get("title") or "").strip()
            price = (poster.get("price") or "").strip()

            if not photo_bytes:
                poster["step"] = "need_photo"
                await tg_send_message(chat_id, "Не вижу фото. Пришли фото товара ещё раз.", reply_markup=_main_menu_keyboard())
                return {"ok": True}

            # Генерация афиши
            await tg_send_message(chat_id, "Делаю афишу на основе твоего фото...")

            prompt = _poster_prompt(title=title, price=price, style=style)
            try:
                out_bytes = await openai_edit_image_make_poster(
                    source_image_bytes=photo_bytes,
                    prompt=prompt,
                    size=IMG_SIZE_DEFAULT,
                )
                await tg_send_photo_bytes(chat_id, out_bytes, caption="Готово. Если нужно — напиши новый текст/цену или пришли другое фото.")
            except Exception as e:
                await tg_send_message(chat_id, f"Не получилось сгенерировать афишу: {e}")

            # Остаёмся в режиме poster и ждём следующее фото/запрос
            st["poster"] = {"step": "need_photo", "photo_bytes": None, "title": "", "price": "", "style": "Зима"}
            st["ts"] = _now()
            return {"ok": True}

        await tg_send_message(chat_id, "Неизвестная кнопка.")
        return {"ok": True}

    # 2) message / edited_message
    message = update.get("message") or update.get("edited_message")
    if not message:
        return {"ok": True}

    chat = message.get("chat", {})
    chat_id = int(chat.get("id") or 0)

    from_user = message.get("from") or {}
    user_id = int(from_user.get("id") or 0)

    if not chat_id or not user_id:
        return {"ok": True}

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

    # Нажатия на reply-кнопки меню
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
            "2) Потом я попрошу текст и цену.\n"
            "3) Выберешь стиль — и я сделаю афишу.",
            reply_markup=_main_menu_keyboard(),
        )
        return {"ok": True}

    if text == "Помощь":
        await tg_send_message(
            chat_id,
            "Как пользоваться:\n"
            "• ИИ (чат): пиши текст — отвечу; пришли фото + вопрос — опишу/попробую определить.\n"
            "• Фото/Афиши: нажми «Фото/Афиши» и пришли фото товара — сделаю рекламную афишу.\n",
            reply_markup=_main_menu_keyboard(),
        )
        return {"ok": True}

    # Фото (как photo)
    photos = message.get("photo") or []
    if photos:
        largest = photos[-1]
        file_id = largest.get("file_id")
        if not file_id:
            await tg_send_message(chat_id, "Фото получил, но не смог прочитать file_id. Попробуй отправить ещё раз.", reply_markup=_main_menu_keyboard())
            return {"ok": True}

        # Режим POSTER: фото = старт афиши
        if st.get("mode") == "poster":
            try:
                file_path = await tg_get_file_path(file_id)
                img_bytes = await tg_download_file_bytes(file_path)
            except Exception as e:
                await tg_send_message(chat_id, f"Ошибка при загрузке фото: {e}", reply_markup=_main_menu_keyboard())
                return {"ok": True}

            poster = st.get("poster") or {}
            poster["photo_bytes"] = img_bytes
            poster["step"] = "need_text_price"
            st["poster"] = poster
            st["ts"] = _now()

            await tg_send_message(
                chat_id,
                "Фото получил.\n"
                "Теперь отправь текст для афиши и цену.\n"
                "Примеры:\n"
                "• NAN ExpertPro; 1500₽\n"
                "• Акция на смесь / 1490₽\n"
                "• Скидка 20%, цена 999\n",
                reply_markup=_main_menu_keyboard(),
            )
            return {"ok": True}

        # Режим CHAT: анализ фото (как раньше)
        await tg_send_message(chat_id, "Фото получил. Анализирую...", reply_markup=_main_menu_keyboard())

        try:
            file_path = await tg_get_file_path(file_id)
            img_bytes = await tg_download_file_bytes(file_path)

            intent = _infer_intent_from_text(text)
            if intent == "math":
                prompt = text if text else "Реши задачу с картинки. Дай решение по шагам и строку 'Ответ: ...'."
                answer = await openai_chat_answer(
                    user_text=prompt,
                    system_prompt=UNICODE_MATH_SYSTEM_PROMPT,
                    image_bytes=img_bytes,
                    temperature=0.3,
                    max_tokens=900,
                )
            else:
                prompt = text if text else VISION_DEFAULT_USER_PROMPT
                answer = await openai_chat_answer(
                    user_text=prompt,
                    system_prompt=VISION_GENERAL_SYSTEM_PROMPT,
                    image_bytes=img_bytes,
                    temperature=0.4,
                    max_tokens=700,
                )
        except Exception as e:
            await tg_send_message(chat_id, f"Ошибка при обработке фото: {e}", reply_markup=_main_menu_keyboard())
            return {"ok": True}

        await tg_send_message(chat_id, answer, reply_markup=_main_menu_keyboard())
        return {"ok": True}

    # Фото (как document, если прислали файлом)
    doc = message.get("document") or {}
    if doc:
        mime = (doc.get("mime_type") or "").lower()
        file_id = doc.get("file_id")
        if file_id and mime.startswith("image/"):
            # Режим POSTER: фото = старт афиши
            if st.get("mode") == "poster":
                try:
                    file_path = await tg_get_file_path(file_id)
                    img_bytes = await tg_download_file_bytes(file_path)
                except Exception as e:
                    await tg_send_message(chat_id, f"Ошибка при загрузке фото: {e}", reply_markup=_main_menu_keyboard())
                    return {"ok": True}

                poster = st.get("poster") or {}
                poster["photo_bytes"] = img_bytes
                poster["step"] = "need_text_price"
                st["poster"] = poster
                st["ts"] = _now()

                await tg_send_message(
                    chat_id,
                    "Фото (файлом) получил.\n"
                    "Теперь отправь текст для афиши и цену.\n"
                    "Примеры:\n"
                    "• NAN ExpertPro; 1500₽\n"
                    "• Акция на смесь / 1490₽\n"
                    "• Скидка 20%, цена 999\n",
                    reply_markup=_main_menu_keyboard(),
                )
                return {"ok": True}

            # Режим CHAT: анализ фото
            await tg_send_message(chat_id, "Фото получил. Анализирую...", reply_markup=_main_menu_keyboard())
            try:
                file_path = await tg_get_file_path(file_id)
                img_bytes = await tg_download_file_bytes(file_path)

                intent = _infer_intent_from_text(text)
                if intent == "math":
                    prompt = text if text else "Реши задачу с картинки. Дай решение по шагам и строку 'Ответ: ...'."
                    answer = await openai_chat_answer(
                        user_text=prompt,
                        system_prompt=UNICODE_MATH_SYSTEM_PROMPT,
                        image_bytes=img_bytes,
                        temperature=0.3,
                        max_tokens=900,
                    )
                else:
                    prompt = text if text else VISION_DEFAULT_USER_PROMPT
                    answer = await openai_chat_answer(
                        user_text=prompt,
                        system_prompt=VISION_GENERAL_SYSTEM_PROMPT,
                        image_bytes=img_bytes,
                        temperature=0.4,
                        max_tokens=700,
                    )
            except Exception as e:
                await tg_send_message(chat_id, f"Ошибка при обработке фото: {e}", reply_markup=_main_menu_keyboard())
                return {"ok": True}

            await tg_send_message(chat_id, answer, reply_markup=_main_menu_keyboard())
            return {"ok": True}

    # Текст без фото
    if text:
        # Режим POSTER: ожидаем текст/цену, затем стиль
        if st.get("mode") == "poster":
            poster = st.get("poster") or {}
            step: PosterStep = poster.get("step") or "need_photo"

            if step == "need_photo":
                await tg_send_message(chat_id, "Сначала пришли фото товара.", reply_markup=_main_menu_keyboard())
                return {"ok": True}

            if step == "need_text_price":
                title, price = _parse_title_price(text)
                poster["title"] = title or "АКЦИЯ"
                poster["price"] = price
                poster["step"] = "need_style"
                st["poster"] = poster
                st["ts"] = _now()

                await tg_send_message(
                    chat_id,
                    f"Принял.\nЗаголовок: {poster['title']}\nЦена: {poster['price'] or '(без цены)'}\n"
                    "Выбери стиль афиши:",
                    reply_markup=_style_inline_keyboard(),
                )
                return {"ok": True}

            if step == "need_style":
                await tg_send_message(chat_id, "Выбери стиль кнопкой ниже.", reply_markup=_style_inline_keyboard())
                return {"ok": True}

            # fallback
            await tg_send_message(chat_id, "Нажми «Фото/Афиши» и следуй шагам.", reply_markup=_main_menu_keyboard())
            return {"ok": True}

        # Режим CHAT: обычный ответ
        # Если пользователь просит “сделай афишу”, направляем в меню
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
