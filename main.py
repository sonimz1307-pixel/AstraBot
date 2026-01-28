import os
import base64
import time
import asyncio
import re
import json
from io import BytesIO
from typing import Optional, Literal, Dict, Any, Tuple, List

import httpx
from fastapi import FastAPI, Request, Response

app = FastAPI()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change_me")


# ---- BytePlus / ModelArk (Seedream) — used ONLY for "Нейро фотосессии" mode ----
ARK_API_KEY = os.getenv("ARK_API_KEY", "").strip()
ARK_BASE_URL = os.getenv("ARK_BASE_URL", "https://ark.ap-southeast.bytepluses.com/api/v3").rstrip("/")
ARK_IMAGE_MODEL = os.getenv("ARK_IMAGE_MODEL", "").strip()  # endpoint id: ep-...
ARK_SIZE_DEFAULT = os.getenv("ARK_SIZE_DEFAULT", "2K").strip()
ARK_TIMEOUT = float(os.getenv("ARK_TIMEOUT", "120"))
ARK_WATERMARK = os.getenv("ARK_WATERMARK", "true").lower() in ("1","true","yes","y","on")

IMG_SIZE_DEFAULT = os.getenv("IMG_SIZE_DEFAULT", "1024x1536")

# Fake progress UI (Telegram): updates caption while generating images
PROGRESS_UI_ENABLED = os.getenv("PROGRESS_UI_ENABLED", "true").lower() in ("1","true","yes","y","on")
PROGRESS_EXPECTED_SECONDS = float(os.getenv("PROGRESS_EXPECTED_SECONDS", "22"))  # how fast % grows
PROGRESS_UPDATE_EVERY = float(os.getenv("PROGRESS_UPDATE_EVERY", "2.0"))
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TELEGRAM_FILE_BASE = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}"

# ---------------- In-memory state ----------------
STATE_TTL_SECONDS = int(os.getenv("STATE_TTL_SECONDS", "1800"))  # 30 минут
STATE: Dict[Tuple[int, int], Dict[str, Any]] = {}

# ---------------- AI chat memory (in-RAM) ----------------
AI_CHAT_HISTORY_MAX = int(os.getenv("AI_CHAT_HISTORY_MAX", "10"))  # last N messages (user+assistant)
AI_CHAT_TTL_SECONDS = int(os.getenv("AI_CHAT_TTL_SECONDS", "7200"))  # 2 hours
AI_CHAT_SUMMARY_MAX_CHARS = int(os.getenv("AI_CHAT_SUMMARY_MAX_CHARS", "800"))
AI_CHAT_SUMMARY_BATCH = int(os.getenv("AI_CHAT_SUMMARY_BATCH", "10"))  # summarize each N trimmed messages


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

    # Cleanup download slots stored in per-user state
    for k, v in list(STATE.items()):
        dl = v.get("dl")
        if isinstance(dl, dict):
            expired_tokens = []
            for tok, meta in dl.items():
                try:
                    ts2 = float((meta or {}).get("ts", 0))
                except Exception:
                    ts2 = 0.0
                if now - ts2 > STATE_TTL_SECONDS:
                    expired_tokens.append(tok)
            for tok in expired_tokens:
                dl.pop(tok, None)

    # Cleanup AI chat memory (only) after TTL
for k, v in list(STATE.items()):
    try:
        ts_ai = float(v.get("ai_ts", 0) or 0)
    except Exception:
        ts_ai = 0.0
    if ts_ai and (now - ts_ai > AI_CHAT_TTL_SECONDS):
        v.pop("ai_hist", None)
        v.pop("ai_pending", None)
        v.pop("ai_summary", None)
        v.pop("ai_ts", None)


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
        STATE[key] = {"mode": "chat", "ts": _now(), "poster": {}, "dl": {}}
    STATE[key]["ts"] = _now()
    return STATE[key]


# ---------------- AI chat memory helpers ----------------

    def _ai_mem_reset(st: Dict[str, Any]):
        st.pop("ai_hist", None)
        st.pop("ai_pending", None)
        st.pop("ai_summary", None)
        st.pop("ai_ts", None)


    def _ai_hist_get(st: Dict[str, Any]) -> List[Dict[str, str]]:
        hist = st.get("ai_hist")
        return hist if isinstance(hist, list) else []


    def _ai_summary_get(st: Dict[str, Any]) -> str:
        s = st.get("ai_summary")
        return s if isinstance(s, str) else ""


    def _ai_pending_get(st: Dict[str, Any]) -> List[Dict[str, str]]:
        p = st.get("ai_pending")
        if isinstance(p, list):
            return p
        st["ai_pending"] = []
        return st["ai_pending"]


    def _ai_hist_add_sync(st: Dict[str, Any], role: str, content: str):
        """Add message to AI chat memory (sync). Keeps last AI_CHAT_HISTORY_MAX messages.
        Overflow messages are moved to ai_pending for later summarization."""
        if "ai_hist" not in st or not isinstance(st.get("ai_hist"), list):
            st["ai_hist"] = []
        st["ai_hist"].append({"role": role, "content": content})
        st["ai_ts"] = _now()

        # Trim to last N, move overflow to pending
        hist = st["ai_hist"]
        if len(hist) > AI_CHAT_HISTORY_MAX:
            overflow = hist[:-AI_CHAT_HISTORY_MAX]
            st["ai_hist"] = hist[-AI_CHAT_HISTORY_MAX:]
            pending = _ai_pending_get(st)
            pending.extend(overflow)
            # Keep pending from growing without bound (hard cap by count)
            if len(pending) > 200:
                pending[:] = pending[-200:]


async def _ai_build_summary_chunk(chunk: List[Dict[str, str]], prev_summary: str) -> str:
    """
    Summarize a chunk of messages and merge into previous summary.
    """
    sys = (
        "Ты сжимаешь историю диалога для Telegram-бота. "
        "Пиши коротко и по делу. "
        "Сохраняй: цель пользователя, важные факты, договоренности, текущий контекст. "
        "Не добавляй новых фактов и не выдумывай."
    )

    lines = []
    for m in chunk:
        r = m.get("role")
        c = m.get("content")
        if r in ("user", "assistant") and isinstance(c, str) and c.strip():
            c2 = c.strip()
            if len(c2) > 600:
                c2 = c2[:600] + "…"
            lines.append(f"{r}: {c2}")

    user = (
        "Обнови краткое резюме диалога.\n"
        "Сохраняй: цель пользователя, важные факты, договоренности, текущий контекст.\n"
        "Не добавляй новых фактов и не выдумывай.\n\n"
        f"Предыдущее резюме:\n{prev_summary or '—'}\n\n"
        "Новые сообщения:\n"
        + "\n".join(lines)
        + "\n\nВерни обновленное краткое резюме (без воды)."
    )

    out = await openai_chat_answer(
        user_text=user,
        system_prompt=sys,
        image_bytes=None,
        temperature=0.2,
        max_tokens=250,
    )
    return (out or "").strip()


async def _ai_maybe_summarize(st: Dict[str, Any]):
    """If pending has enough messages, update ai_summary and clear pending."""
    pending = _ai_pending_get(st)
    if len(pending) < AI_CHAT_SUMMARY_BATCH:
        return

    chunk = pending[:AI_CHAT_SUMMARY_BATCH]
    del pending[:AI_CHAT_SUMMARY_BATCH]

    prev = _ai_summary_get(st)
    new_sum = await _ai_build_summary_chunk(chunk, prev)
    if new_sum:
        st["ai_summary"] = new_sum[:AI_CHAT_SUMMARY_MAX_CHARS]
    st["ai_ts"] = _now()

def _set_mode(chat_id: int, user_id: int, mode: Literal["chat", "poster", "photosession", "t2i", "two_photos"]):
    st = _ensure_state(chat_id, user_id)
    st["mode"] = mode
    st["ts"] = _now()

    if mode == "poster":
        # Визуальный режим: афиша ИЛИ обычный фото-эдит (после фото)
        st["poster"] = {"step": "need_photo", "photo_bytes": None, "light": "bright"}

    elif mode == "photosession":
        # Нейро фотосессии: Seedream/ModelArk endpoint (image-to-image)
        st["photosession"] = {"step": "need_photo", "photo_bytes": None}

    elif mode == "t2i":
        # Text-to-image: Seedream/ModelArk endpoint (text-to-image)
        st["t2i"] = {"step": "need_prompt"}

    elif mode == "two_photos":
        # 2 фото: multi-image (если эндпоинт поддерживает)
        st["two_photos"] = {
            "step": "need_photo_1",
            "photo1_bytes": None,
            "photo1_file_id": None,
            "photo2_bytes": None,
            "photo2_file_id": None,
        }

    else:
        # chat
        st.pop("poster", None)
        st.pop("photosession", None)
        st.pop("t2i", None)
        st.pop("two_photos", None)



# ---------------- Reply keyboard ----------------

def _main_menu_keyboard() -> dict:
    return {
        "keyboard": [
            [{"text": "ИИ (чат)"}, {"text": "Фото/Афиши"}],
            [{"text": "Нейро фотосессии"}, {"text": "2 фото"}],
            [{"text": "Помощь"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False,
        "selective": False,
    }




def _poster_menu_keyboard(light: str = "bright") -> dict:
    """
    Клавиатура для режима «Фото/Афиши».

    Внутри режима НЕ показываем другие режимы (ИИ/2 фото/Нейро фотосессии) и не дублируем «Фото/Афиши».
    Оставляем только элементы, относящиеся к этому экрану:
      - переключатели стиля афиши (Ярко / Кино)
      - вход в Text→Image (по кнопке)
      - «Назад» в главное меню
    """
    # В кнопке «Ярко» можно держать ✅ — обработчик ниже уже удаляет её при нормализации текста.
    return {
        "keyboard": [
            [{"text": "Афиша: Ярко ✅"}, {"text": "Афиша: Кино"}],
            [{"text": "Текст→Картинка"}],
            [{"text": "⬅ Назад"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False,
        "selective": False,
    }

# ---------------- Telegram helpers ----------------

def _dl_keyboard(token: str) -> dict:
    return {"inline_keyboard": [[{"text": "⬇️ Скачать оригинал 2К", "callback_data": f"dl2k:{token}"}]]}


def _dl_init_slot(chat_id: int, user_id: int) -> str:
    """Create a download slot and return token. Bytes will be filled later."""
    st = _ensure_state(chat_id, user_id)
    dl = st.setdefault("dl", {})
    # short token suitable for callback_data
    token = base64.urlsafe_b64encode(os.urandom(9)).decode("ascii").rstrip("=")
    dl[token] = {"ts": _now(), "bytes": None, "ext": "png", "mime": "image/png"}
    return token


def _dl_set_bytes(chat_id: int, user_id: int, token: str, image_bytes: bytes):
    st = _ensure_state(chat_id, user_id)
    dl = st.setdefault("dl", {})
    ext, mime = _detect_image_type(image_bytes)
    dl[token] = {"ts": _now(), "bytes": image_bytes, "ext": ext, "mime": mime}


def _dl_get(chat_id: int, user_id: int, token: str):
    st = _ensure_state(chat_id, user_id)
    dl = st.get("dl") or {}
    meta = dl.get(token)
    if not isinstance(meta, dict):
        return None
    return meta


async def tg_answer_callback_query(callback_query_id: str, text: Optional[str] = None, show_alert: bool = False):
    if not TELEGRAM_BOT_TOKEN:
        return
    payload = {"callback_query_id": callback_query_id, "show_alert": bool(show_alert)}
    if text:
        payload["text"] = text
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(f"{TELEGRAM_API_BASE}/answerCallbackQuery", json=payload)


async def tg_send_document_bytes(
    chat_id: int,
    file_bytes: bytes,
    filename: str = "original_2k.png",
    caption: Optional[str] = None,
    reply_markup: Optional[dict] = None,
):
    """Send as document to avoid Telegram compression."""
    if not TELEGRAM_BOT_TOKEN:
        return

    if not file_bytes:
        raise RuntimeError("Empty file bytes for sendDocument")

    ext, mime = _detect_image_type(file_bytes)
    if not filename.lower().endswith(f".{ext}"):
        filename = f"{os.path.splitext(filename)[0]}.{ext}"

    files = {"document": (filename, file_bytes, mime)}
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)

    async with httpx.AsyncClient(timeout=240) as client:
        r = await client.post(f"{TELEGRAM_API_BASE}/sendDocument", data=data, files=files)

    # Если Telegram вернул ошибку — поднимем исключение (его поймают выше и покажут пользователю)
    try:
        j = r.json()
        if isinstance(j, dict) and not j.get("ok", False):
            raise RuntimeError(f"Telegram sendDocument error: {j}")
    except Exception:
        if r.status_code >= 400:
            raise RuntimeError(f"Telegram sendDocument HTTP {r.status_code}: {r.text[:1200]}")



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


async def tg_send_chat_action(chat_id: int, action: str = "typing"):
    """
    Показывает системный индикатор Telegram (typing/upload_photo/record_video и т.п.).
    Это не "полоска прогресса", но создаёт ощущение активности.
    """
    if not TELEGRAM_BOT_TOKEN:
        return
    payload = {"chat_id": str(chat_id), "action": action}
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(f"{TELEGRAM_API_BASE}/sendChatAction", json=payload)


async def tg_send_photo_bytes_return_message_id(chat_id: int, image_bytes: bytes, caption: Optional[str] = None, reply_markup: Optional[dict] = None) -> Optional[int]:
    """
    sendPhoto, но возвращает message_id (нужен для editMessageCaption/editMessageMedia).
    """
    if not TELEGRAM_BOT_TOKEN:
        return None
    files = {"photo": ("image.png", image_bytes, "image/png")}
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
    async with httpx.AsyncClient(timeout=180) as client:
        r = await client.post(f"{TELEGRAM_API_BASE}/sendPhoto", data=data, files=files)
    try:
        j = r.json()
        if isinstance(j, dict) and j.get("ok") and j.get("result") and j["result"].get("message_id") is not None:
            return int(j["result"]["message_id"])
    except Exception:
        pass
    return None


async def tg_edit_message_caption(chat_id: int, message_id: int, caption: str):
    if not TELEGRAM_BOT_TOKEN:
        return
    payload = {"chat_id": str(chat_id), "message_id": int(message_id), "caption": caption}
    async with httpx.AsyncClient(timeout=20) as client:
        await client.post(f"{TELEGRAM_API_BASE}/editMessageCaption", json=payload)


async def tg_edit_message_media_photo(chat_id: int, message_id: int, image_bytes: bytes, caption: Optional[str] = None, reply_markup: Optional[dict] = None):
    """
    Заменяет фото в существующем сообщении (эффект: был силуэт/превью → стало финальное изображение).
    """
    if not TELEGRAM_BOT_TOKEN:
        return
    media = {"type": "photo", "media": "attach://photo"}
    if caption:
        media["caption"] = caption

    files = {"photo": ("image.png", image_bytes, "image/png")}
    data = {"chat_id": str(chat_id), "message_id": str(message_id), "media": json.dumps(media, ensure_ascii=False)}
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)

    async with httpx.AsyncClient(timeout=180) as client:
        await client.post(f"{TELEGRAM_API_BASE}/editMessageMedia", data=data, files=files)


def _make_blur_placeholder(source_image_bytes: Optional[bytes], size_hint: Tuple[int, int] = (768, 1152)) -> bytes:
    """
    Быстро генерит 'силуэт/превью' (пикселизация + блюр + затемнение), чтобы отправить как placeholder.
    Если исходника нет (T2I), рисуем нейтральный фон.
    """
    from PIL import Image, ImageDraw, ImageFilter  # type: ignore

    W, H = size_hint
    try:
        if source_image_bytes:
            img = Image.open(BytesIO(source_image_bytes)).convert("RGB")
            # подгоняем под вертикаль
            img = img.resize((W, H), Image.LANCZOS)
            # пикселизация
            small = img.resize((max(32, W // 24), max(32, H // 24)), Image.BILINEAR)
            img = small.resize((W, H), Image.NEAREST)
        else:
            img = Image.new("RGB", (W, H), (40, 40, 40))
        img = img.filter(ImageFilter.GaussianBlur(radius=6))

        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(overlay)
        d.rectangle((0, 0, W, H), fill=(0, 0, 0, 90))
        # лёгкая "плашка" внизу
        d.rounded_rectangle((int(W*0.07), int(H*0.83), int(W*0.93), int(H*0.93)), radius=22, fill=(0, 0, 0, 110))
        img = Image.alpha_composite(img.convert("RGBA"), overlay)

        bio = BytesIO()
        img.save(bio, format="PNG")
        return bio.getvalue()
    except Exception:
        # fallback: пустая картинка
        img = Image.new("RGB", (W, H), (40, 40, 40))
        bio = BytesIO()
        img.save(bio, format="PNG")
        return bio.getvalue()


async def _progress_caption_updater(chat_id: int, message_id: int, base_text: str, stop: asyncio.Event):
    """
    Фейковый прогресс: обновляем caption каждые N секунд до 99%.
    """
    if not PROGRESS_UI_ENABLED:
        return

    start = _now()
    last_sent = -1

    while not stop.is_set():
        elapsed = _now() - start
        pct = int(min(99, max(1, (elapsed / max(1.0, PROGRESS_EXPECTED_SECONDS)) * 100)))

        # лёгкая "шкала" из кружков
        filled = max(0, min(5, int(round(pct / 20))))
        bar = "🟢" * filled + "⚪" * (5 - filled)

        if pct != last_sent:
            last_sent = pct
            try:
                await tg_edit_message_caption(chat_id, message_id, f"{base_text}\n{bar} ({pct}%)")
            except Exception:
                # если Telegram не даёт слишком часто — просто молча продолжаем
                pass

        await asyncio.sleep(PROGRESS_UPDATE_EVERY)

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


async def http_download_bytes(url: str, timeout: float = 180) -> bytes:
    async with httpx.AsyncClient(timeout=timeout) as client:
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
    history: Optional[List[Dict[str, str]]] = None,
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
    msgs = [{"role": "system", "content": system_prompt}]
    if history:
        for m in history:
            if (
                isinstance(m, dict)
                and m.get("role") in ("system", "user", "assistant")
                and isinstance(m.get("content"), str)
            ):
                msgs.append({"role": m["role"], "content": m["content"]})
    msgs.append({"role": "user", "content": user_text})

    payload = {
        "model": "gpt-4o-mini",
        "messages": msgs,
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




def _normalize_ark_size(size: str) -> str:
    """
    Seedream/ModelArk в консоли часто использует "2K"/"4K".
    Если у тебя размер вида "1024x1536" — конвертируем в ARK_SIZE_DEFAULT.
    """
    s = (size or "").strip()
    if not s:
        return ARK_SIZE_DEFAULT
    if "x" in s.lower():
        return ARK_SIZE_DEFAULT
    return s


async def ark_edit_image(
    source_image_bytes: bytes,
    prompt: str,
    size: str = "1024x1024",
    mask_png_bytes: Optional[bytes] = None,
    *,
    source_image_url: Optional[str] = None,
    source_image_urls: Optional[List[str]] = None,
) -> bytes:
    """Image-to-image via ModelArk (Seedream) using /images/generations.

    IMPORTANT:
    - ModelArk V3 uses /images/generations for both text-to-image and image-to-image.
    - For image-to-image, pass a publicly reachable image URL (recommended) via `source_image_url`.
      We generate such a URL using Telegram File API in the caller.
    - If `source_image_url` is not provided, we fall back to multipart upload to /images/generations.
      (Some deployments may accept it; if your account only supports URL input, provide `source_image_url`.)
    """

    url = f"{ARK_BASE_URL.rstrip('/')}/images/generations"
    headers = {"Authorization": f"Bearer {ARK_API_KEY}"}

    # If we have URL(s), prefer JSON payload (most compatible)
    img_list: Optional[List[str]] = None
    if source_image_urls and isinstance(source_image_urls, list) and len(source_image_urls) > 0:
        img_list = [u for u in source_image_urls if u]
    elif source_image_url:
        img_list = [source_image_url]

    if img_list:
        payload = {
            "model": ARK_IMAGE_MODEL,
            "prompt": prompt,
            "response_format": "url",
            "size": size,
            # ModelArk expects list for multi-image fusion; single image works too
            "image": img_list,
            "sequential_image_generation": "disabled",
            "stream": False,
            "watermark": bool(ARK_WATERMARK),
        }
        async with httpx.AsyncClient(timeout=ARK_TIMEOUT) as client:
            resp = await client.post(url, headers={**headers, "Content-Type": "application/json"}, json=payload)
            if resp.status_code >= 400:
                raise RuntimeError(f"ModelArk Images Generations ({resp.status_code}): {resp.text}")
            j = resp.json()
    else:
        # Fallback: try multipart (works on some setups)
        files = {
            "image": ("image.jpg", source_image_bytes, "image/jpeg"),
        }
        data = {
            "model": ARK_IMAGE_MODEL,
            "prompt": prompt,
            "response_format": "url",
            "size": size,
            "sequential_image_generation": "disabled",
            "stream": "false",
            "watermark": "true" if ARK_WATERMARK else "false",
        }
        async with httpx.AsyncClient(timeout=ARK_TIMEOUT) as client:
            resp = await client.post(url, headers=headers, data=data, files=files)
            if resp.status_code >= 400:
                raise RuntimeError(f"ModelArk Images Generations ({resp.status_code}): {resp.text}")
            j = resp.json()

    # Expected OpenAI-compatible schema: {data: [{url: ...}]}
    data_arr = j.get("data") or []
    if not data_arr:
        raise RuntimeError(f"ModelArk empty response: {j}")
    img_url = data_arr[0].get("url") or data_arr[0].get("b64_json")
    if not img_url:
        raise RuntimeError(f"ModelArk missing url in response: {j}")

    if data_arr[0].get("b64_json"):
        import base64
        return base64.b64decode(data_arr[0]["b64_json"])

    # Download the resulting image from the returned URL
    async with httpx.AsyncClient(timeout=ARK_TIMEOUT) as client:
        r2 = await client.get(img_url)
        if r2.status_code >= 400:
            raise RuntimeError(f"ModelArk result download ({r2.status_code}): {r2.text}")
        return r2.content



async def ark_text_to_image(prompt: str, size: str = "2K") -> bytes:
    """Text-to-image via ModelArk (Seedream) using /images/generations."""
    url = f"{ARK_BASE_URL.rstrip('/')}/images/generations"
    headers = {"Authorization": f"Bearer {ARK_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": ARK_IMAGE_MODEL,
        "prompt": prompt,
        "response_format": "url",
        "size": size,
        "sequential_image_generation": "disabled",
        "stream": False,
        "watermark": bool(ARK_WATERMARK),
    }
    async with httpx.AsyncClient(timeout=ARK_TIMEOUT) as client:
        resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code >= 400:
            raise RuntimeError(f"ModelArk Images Generations ({resp.status_code}): {resp.text}")
        j = resp.json()

    data_arr = j.get("data") or []
    if not data_arr:
        raise RuntimeError(f"ModelArk empty response: {j}")
    if data_arr[0].get("b64_json"):
        import base64
        return base64.b64decode(data_arr[0]["b64_json"])
    img_url = data_arr[0].get("url")
    if not img_url:
        raise RuntimeError(f"ModelArk missing url in response: {j}")

    async with httpx.AsyncClient(timeout=ARK_TIMEOUT) as client:
        r2 = await client.get(img_url)
        if r2.status_code >= 400:
            raise RuntimeError(f"ModelArk result download ({r2.status_code}): {r2.text}")
        return r2.content


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
        return {"headline": "", "style": "", "price": "", "simple_text": False, "short_headline": True}

    price = _extract_price_any(raw)
    simple_text = _wants_simple_text(raw)

    low = raw.lower()
    if "надпись" in low:
        m = re.search(r"надпись\s*[:\-]\s*(.+)$", raw, flags=re.IGNORECASE | re.DOTALL)
        if m:
            headline = m.group(1).strip().strip('"“”')
            style_part = re.split(r"надпись\s*[:\-]", raw, flags=re.IGNORECASE)[0].strip()
            is_short = (len(headline.split()) <= 3) if headline else True
            return {"headline": headline, "style": style_part, "price": price, "simple_text": simple_text, "short_headline": is_short}

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

        is_short = (len(headline.split()) <= 3) if headline else True
        return {"headline": headline, "style": style, "price": price2, "simple_text": simple_text, "short_headline": is_short}
    except Exception:
        is_short = True
        return {"headline": "", "style": raw, "price": price, "simple_text": simple_text, "short_headline": is_short}


# ---------------- Poster: ART DIRECTOR prompt (Variant A) ----------------

ART_DIRECTOR_NEGATIVE = (
    "plain font, simple typography, flat text, cheap poster, wordart, basic letters, "
    "low contrast, boring design, stock typography, watermark, random slogan, extra text"
)

def _poster_prompt_art_director(spec: Dict[str, Any], light: str = "bright") -> str:
    """
    VARIANT A: просим модель САМОЙ нарисовать типографику как объект сцены.
    Никакого overlay Pillow. Цель — дизайнерский результат, а не стабильный шрифт.

    Важно: мы всё равно жёстко запрещаем любые дополнительные слова/фразы.
    """
    headline = (spec.get("headline") or "").strip() or " "
    style = (spec.get("style") or "").strip()
    price = (spec.get("price") or "").strip()

    # Фото может быть любым: не только цветы. Поэтому стиль используем как описание сцены/настроения.
    # Если пользователь дал просто "сделай красиво" — задаём универсальный арт-директорский каркас.
    scene = style if style else "Premium, clean, modern, cinematic atmosphere that matches the provided photo."

    # Lighting preset
    light = (light or "bright").strip().lower()
    if light not in ("bright", "cinema"):
        light = "bright"

    if light == "bright":
        lighting = (
            "LIGHTING & EXPOSURE (CRITICAL):\n"
            "Bright high-key lighting. Daylight or studio soft light.\n"
            "High exposure, airy and fresh look. Clean highlights.\n"
            "No dark mood, no low-key lighting, no gloomy atmosphere.\n"
            "Vivid but natural colors, fresh palette, no muddy/brown tones.\n"
        )
        opener = "Create a bright high-end vertical poster based on the provided photo."
    else:
        lighting = (
            "LIGHTING & EXPOSURE (CRITICAL):\n"
            "Cinematic contrast lighting. Controlled shadows, depth and atmosphere.\n"
            "Still keep legibility and premium clarity.\n"
        )
        opener = "Create a cinematic vertical poster based on the provided photo."


    # Price: печатаем только если пользователь указал цену
    price_block = ""
    if price:
        price_block = (
            f'\nSecondary text (price): "{price}".\n'
            "Price must be written exactly as provided, once, and must be perfectly legible.\n"
        )

    return (
        "You are an art director and typographic designer.\n"
        "Create a cinematic vertical poster based on the provided photo.\n\n"
        "CRITICAL TEXT RULES:\n"
        f'1) Main headline text must be EXACTLY: "{headline}".\n'
        + ("2) Also include ONLY the price text below.\n" if price else "2) Do NOT include any price.\n")
        + "3) Absolutely NO other words, slogans, subtitles, labels, badges, watermarks, brand phrases.\n"
          "   Forbidden examples: SALE, DISCOUNT, NEW, HIT, PROMO, opening soon (unless it IS the headline), etc.\n"
          "4) Text must be perfectly legible, not distorted, no missing letters, keep original language and spelling.\n\n"
        "TYPOGRAPHY (MANDATORY):\n"
        "• The headline is NOT a plain font.\n"
        "• The headline is custom artistic lettering made of materials that match the scene (organic petals, glass, metal, neon, paper, fabric, light, smoke, etc.).\n"
        "• Volumetric, detailed, professional poster-design quality.\n"
        "• Integrated into the environment with natural lighting, shadows, depth.\n\n"
        "PHOTO PRESERVATION:\n"
        "• Keep the main subject from the photo realistic and recognizable.\n"
        "• Do not change branding/shape/colors of the subject.\n"
        "• Improve lighting/composition/background atmosphere only.\n\n"
        f"SCENE / MOOD:\n{scene}\n\n"
        + lighting + "\n"
        f"{price_block}\n"
        f"Negative prompt: {ART_DIRECTOR_NEGATIVE}\n\n"
        "Output: one high-quality vertical poster for stories.\n"
    )


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
    short_headline = bool(spec.get("short_headline", False))

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

    headline_boost = (
    """
УСИЛЕНИЕ КОРОТКОГО ЗАГОЛОВКА:
• HEADLINE состоит из 1–3 слов — это нормально.
• Сделай типографику максимально выразительной, как в дорогих брендовых постерах:
  — очень крупный кегль и сильная иерархия
  — артистичное размещение и баланс композиции
  — аккуратный кернинг/трекинг, чистые края, ощущение премиум
  — допускается перенос слов на 2–3 строки (без изменения букв/регистра)
• НЕ добавляй новых слов. Работай ТОЛЬКО дизайном.
"""
    if (short_headline and not simple_text)
    else ""
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
        f"{headline_boost}"
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



# ---------------- Poster: background-only prompt + deterministic WOW text overlay ----------------

def _poster_background_prompt_from_spec(spec: Dict[str, Any]) -> str:
    """
    Генерируем ТОЛЬКО фон/композицию афиши без печати текста.
    Текст будем накладывать сами (Pillow), чтобы он всегда был 'ВАУ' и без искажений.
    """
    style = (spec.get("style") or "").strip()
    price = (spec.get("price") or "").strip()

    # ВАЖНО: не печатать никаких букв/цифр, включая цену — цену тоже наложим сами.
    return (
        "Сделай профессиональную рекламную афишу/промо-баннер на основе предоставленного фото.\n\n"
        "КРИТИЧЕСКОЕ ПРАВИЛО: НИКАКОГО ТЕКСТА.\n"
        "• Запрещены любые буквы, слова, цифры, цены, символы валют, слоганы, водяные знаки, логотипы.\n"
        "• НЕ печатай даже HEADLINE и цену.\n\n"
        "КОМПОЗИЦИЯ:\n"
        "• Товар/объект (то, что на фото) должен остаться максимально реалистичным и узнаваемым.\n"
        "• Разрешено улучшить композицию, свет, фон, добавить атмосферные элементы по стилю (НЕ текстом).\n"
        "• Оставь чистое свободное место под заголовок в верхней части кадра (примерно верхние 25–30%).\n"
        "• Если нужно — затемни/размой фон в верхней зоне, чтобы на нём хорошо читался будущий заголовок.\n\n"
        "СТИЛЬ/АТМОСФЕРА (НЕ ПЕЧАТАТЬ КАК ТЕКСТ, только оформление):\n"
        f"{style if style else 'Премиум, чисто, современно, без перегруза.'}\n\n"
        "ФОРМАТ: вертикальный, под сторис, высокое качество.\n"
    )


def _split_headline_lines(headline: str) -> str:
    """
    Делим короткий заголовок на 2 строки максимально красиво.
    Возвращает текст с \n если нужно.
    """
    h = (headline or "").strip()
    if not h:
        return " "
    words = h.split()
    if len(words) <= 1:
        return h
    if len(words) == 2:
        return words[0] + "\n" + words[1]
    # 3+ слов — балансируем примерно пополам
    mid = len(words) // 2
    return " ".join(words[:mid]) + "\n" + " ".join(words[mid:])


def _load_font(prefer_serif: bool, size: int):
    """
    Пытаемся загрузить системные шрифты (обычно доступны на Render/Linux).
    """
    try:
        from PIL import ImageFont  # type: ignore
    except Exception:
        return None

    candidates = []
    if prefer_serif:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSerifBold.ttf",
        ]
    else:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ]

    for p in candidates:
        try:
            return ImageFont.truetype(p, size=size)
        except Exception:
            continue

    # fallback pillow default
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def _draw_text_with_effects(img_rgba, text: str, y_top: int, premium: bool = True):
    """
    Рисуем заголовок 'ВАУ' (эмбосс/фольга/свечение) детерминированно.
    """
    try:
        from PIL import Image, ImageDraw, ImageFilter  # type: ignore
    except Exception:
        return img_rgba  # без Pillow не сможем

    W, H = img_rgba.size
    # базовые параметры
    headline = _split_headline_lines(text)
    # крупный кегль под сторис
    base_size = int(W * (0.18 if "\n" in headline else 0.20))
    font = _load_font(prefer_serif=True, size=base_size) if premium else _load_font(prefer_serif=False, size=base_size)
    if font is None:
        return img_rgba

    # измерение текста
    dummy = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(dummy)
    bbox = d.multiline_textbbox((0, 0), headline, font=font, align="center", spacing=int(base_size * 0.10))
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]

    # если слишком широко — уменьшаем
    while tw > int(W * 0.92) and base_size > 20:
        base_size = int(base_size * 0.92)
        font = _load_font(prefer_serif=True, size=base_size) if premium else _load_font(prefer_serif=False, size=base_size)
        bbox = d.multiline_textbbox((0, 0), headline, font=font, align="center", spacing=int(base_size * 0.10))
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]

    x = (W - tw) // 2
    y = max(10, y_top)

    # слой с маской текста
    text_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    td = ImageDraw.Draw(text_layer)

    spacing = int(base_size * 0.10)
    # Glow
    if premium:
        glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        gd.multiline_text((x, y), headline, font=font, fill=(255, 244, 220, 255), align="center", spacing=spacing)
        glow = glow.filter(ImageFilter.GaussianBlur(radius=int(base_size * 0.12)))
        img_rgba = Image.alpha_composite(img_rgba, glow)

    # Shadow
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.multiline_text((x + int(base_size * 0.05), y + int(base_size * 0.05)), headline, font=font, fill=(0, 0, 0, 140), align="center", spacing=spacing)
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=int(base_size * 0.06)))
    img_rgba = Image.alpha_composite(img_rgba, shadow)

    # Outline (stroke)
    stroke_w = max(2, int(base_size * 0.04)) if premium else 1
    for dx in (-stroke_w, 0, stroke_w):
        for dy in (-stroke_w, 0, stroke_w):
            if dx == 0 and dy == 0:
                continue
            td.multiline_text((x + dx, y + dy), headline, font=font, fill=(90, 60, 20, 180) if premium else (0, 0, 0, 180), align="center", spacing=spacing)

    # Fill: "foil" gradient using mask
    mask = Image.new("L", (W, H), 0)
    md = ImageDraw.Draw(mask)
    md.multiline_text((x, y), headline, font=font, fill=255, align="center", spacing=spacing)

    grad = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    # вертикальный градиент (золото/перламутр)
    topc = (255, 248, 230, 255)
    midc = (235, 204, 140, 255)
    botc = (255, 246, 220, 255)
    for yy in range(y, min(H, y + th + 4)):
        if th <= 1:
            t = 0.5
        else:
            t = (yy - y) / float(th)
        if t < 0.5:
            # top -> mid
            tt = t / 0.5
            c = (
                int(topc[0] + (midc[0] - topc[0]) * tt),
                int(topc[1] + (midc[1] - topc[1]) * tt),
                int(topc[2] + (midc[2] - topc[2]) * tt),
                255,
            )
        else:
            # mid -> bot
            tt = (t - 0.5) / 0.5
            c = (
                int(midc[0] + (botc[0] - midc[0]) * tt),
                int(midc[1] + (botc[1] - midc[1]) * tt),
                int(midc[2] + (botc[2] - midc[2]) * tt),
                255,
            )
        ImageDraw.Draw(grad).line([(0, yy), (W, yy)], fill=c)

    fill_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    fill_layer.paste(grad, (0, 0), mask=mask)

    # лёгкий эмбосс/блик
    if premium:
        emb = fill_layer.filter(ImageFilter.EMBOSS)
        emb = emb.filter(ImageFilter.GaussianBlur(radius=int(base_size * 0.02)))
        fill_layer = Image.alpha_composite(fill_layer, emb)

    img_rgba = Image.alpha_composite(img_rgba, fill_layer)
    return img_rgba


def overlay_poster_text(image_bytes: bytes, headline: str, price: str, simple_text: bool) -> bytes:
    """
    Накладываем текст детерминированно поверх готового фона.
    """
    try:
        from PIL import Image  # type: ignore
    except Exception:
        # Pillow нет — вернём как есть
        return image_bytes

    from io import BytesIO
    im = Image.open(BytesIO(image_bytes)).convert("RGBA")
    W, H = im.size

    premium = not bool(simple_text)

    # Заголовок в верхней зоне
    top_zone = int(H * 0.06)
    im = _draw_text_with_effects(im, headline, y_top=top_zone, premium=premium)

    # Цена (если есть) — внизу, но аккуратно
    if price:
        # простая цена тоже может быть премиум-стикером
        price_text = str(price).strip()
        # рисуем чуть ниже букета (нижняя треть)
        y_price = int(H * 0.80)
        im = _draw_text_with_effects(im, price_text, y_top=y_price, premium=premium)

    out = BytesIO()
    im.convert("RGB").save(out, format="PNG")
    return out.getvalue()

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



async def openai_check_poster_typography_quality(image_bytes: bytes) -> Dict[str, Any]:
    """
    Проверка качества типографики оверлей-заголовка:
    избегаем "обычного шрифта" и добиваемся премиум-леттеринга/брендового вида.
    """
    sys = (
        "Ты арт-директор и оцениваешь ТОЛЬКО добавленный крупный заголовок/типографику на афише (не текст на упаковке).\n"
        "Оцени: выглядит ли заголовок как премиум-дизайн (кастомный леттеринг/фольгирование/тиснение/иерархия), "
        "или как простой стандартный шрифт.\n\n"
        "Верни строго JSON без текста вокруг:\n"
        "{\"wow\":1-10,\"plain\":true|false,\"notes\":\"коротко\"}\n\n"
        "Правила:\n"
        "• plain=true, если заголовок выглядит как обычная печатная надпись без дизайнерского характера.\n"
        "• wow>=8 — это реально 'вау', как брендовый постер.\n"
        "• Учитывай только оверлей-текст, игнорируй текст на товаре/упаковке."
    )
    out = await openai_chat_answer(
        user_text="Оцени типографику заголовка и верни JSON.",
        system_prompt=sys,
        image_bytes=image_bytes,
        temperature=0.0,
        max_tokens=180,
    )
    try:
        data = json.loads(out)
        wow = int(data.get("wow", 0))
        plain = bool(data.get("plain", False))
        notes = str(data.get("notes", "")).strip()[:200]
        if wow < 1:
            wow = 1
        if wow > 10:
            wow = 10
        return {"wow": wow, "plain": plain, "notes": notes}
    except Exception:
        return {"wow": 7, "plain": False, "notes": "parse_fail"}




# ---------------- 2-photo prompt (ModelArk) ----------------

def _two_photos_prompt(user_task: str) -> str:
    """
    Multi-image instruction wrapper.
    Image 1 = BASE, Image 2 = REFERENCE.
    User describes what to do in plain text.
    """
    task = (user_task or "").strip()
    return (
        "MULTI-IMAGE EDIT (2 references).\n"
        "Image 1 = BASE: keep composition, pose, body, scene, camera angle.\n"
        "Image 2 = REFERENCE: use as identity/style reference ONLY as requested by the user.\n\n"
        "CRITICAL RULES:\n"
        "• Follow the user's instruction exactly.\n"
        "• Do NOT add any text, words, numbers, prices, watermarks.\n"
        "• If user asks to replace face/identity: keep body/scene from Image 1 and transfer identity from Image 2.\n"
        "• If user asks to keep identity from Image 1: do not change the person's face.\n"
        "• Do not change age, gender, ethnicity unless user explicitly asks.\n"
        "• Keep realism, correct anatomy, consistent lighting and shadows.\n\n"
        f"USER TASK:\n{task}\n"
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

    # --- Inline button callbacks (e.g., download 2K) ---
    callback_query = update.get("callback_query")
    if callback_query:
        cq_id = callback_query.get("id") or ""
        from_user = callback_query.get("from") or {}
        user_id = int(from_user.get("id") or 0)
        msg = callback_query.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = int(chat.get("id") or 0)
        data = (callback_query.get("data") or "").strip()

        if cq_id:
            # instantly stop Telegram spinner
            try:
                await tg_answer_callback_query(str(cq_id))
            except Exception:
                pass

        if chat_id and user_id and data.startswith("dl2k:"):
            token = data.split(":", 1)[1].strip()
            meta = _dl_get(chat_id, user_id, token)
            if not meta:
                await tg_answer_callback_query(str(cq_id), text="Ссылка устарела. Сгенерируй заново.", show_alert=True)
                return {"ok": True}

            b = meta.get("bytes")
            if not b:
                await tg_answer_callback_query(str(cq_id), text="Оригинал ещё генерируется…", show_alert=False)
                return {"ok": True}

            try:
                await tg_send_document_bytes(chat_id, b, filename=f"original_2k.{meta.get('ext','png')}", caption="⬇️ Оригинал 2К (без сжатия)")
            except Exception:
                await tg_send_message(chat_id, "Не смог отправить оригинал файлом. Попробуй ещё раз.")
        return {"ok": True}

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


    if incoming_text in ("⬅ Назад", "Назад"):
        # Возврат в главное меню из любого режима
        _set_mode(chat_id, user_id, "chat")
        await tg_send_message(chat_id, "Главное меню.", reply_markup=_main_menu_keyboard())
        return {"ok": True}


    if incoming_text == "ИИ (чат)":
        _set_mode(chat_id, user_id, "chat")
        await tg_send_message(chat_id, "Ок. Режим «ИИ (чат)».", reply_markup=_main_menu_keyboard())
        return {"ok": True}


    if incoming_text == "Нейро фотосессии":
        _set_mode(chat_id, user_id, "photosession")
        await tg_send_message(
            chat_id,
            "Режим «Нейро фотосессии».\n"
            "1) Пришли фото.\n"
            "2) Потом одним сообщением напиши задачу: локация/стиль/одежда/детали.\n"
            "Я постараюсь сохранить человека максимально 1к1 и сделать фото как профессиональную фотосессию.",
            reply_markup=_main_menu_keyboard(),
        )
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
            reply_markup=_poster_menu_keyboard((st.get("poster") or {}).get("light", "bright")),
        )
        return {"ok": True}


    if incoming_text == "2 фото":
        _set_mode(chat_id, user_id, "two_photos")
        await tg_send_message(
            chat_id,
            "Режим «2 фото».\n"
            "1) Пришли Фото 1 — это ОСНОВА (поза/тело/фон).\n"
            "2) Потом Пришли Фото 2 — это ИСТОЧНИК (лицо/стиль/одежда — что скажешь).\n"
            "3) Потом одним сообщением напиши, что сделать из этих двух фото.\n\n"
            "Команда для сброса: /reset",
            reply_markup=_main_menu_keyboard(),
        )
        return {"ok": True}

    if incoming_text == "Текст→Картинка":
        # Text-to-image mode (no input photo required)
        _set_mode(chat_id, user_id, "t2i")
        st["t2i"] = {"step": "need_prompt"}
        await tg_send_message(
            chat_id,
            "Ок. Режим «Текст→Картинка» (без фото).\n"
            "Напиши одним сообщением, что нужно сгенерировать.\n"
            "Пример: «Яркая афиша открытия цветочного магазина, лепестки в воздухе, крупный заголовок»",
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
            "  — в обычном фото-эдите бот автоматически ограничивает правку маской, чтобы фон не менялся\n"
            "• Нейро фотосессии: фото → потом задача\n"
            "• Текст→Картинка: без фото, просто описание\n"
            "• 2 фото: фото1 → фото2 → потом текст, что сделать\n"
            "• /reset — сбросить текущий режим\n",
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



        
        # TWO PHOTOS mode
        if st.get("mode") == "two_photos":
            tp = st.get("two_photos") or {}
            step = (tp.get("step") or "need_photo_1")

            if step == "need_photo_1":
                st["two_photos"] = {
                    "step": "need_photo_2",
                    "photo1_bytes": img_bytes,
                    "photo1_file_id": file_id,
                    "photo2_bytes": None,
                    "photo2_file_id": None,
                }
                st["ts"] = _now()
                await tg_send_message(
                    chat_id,
                    "Фото 1 получил. Теперь пришли Фото 2 (источник: лицо/стиль/одежда).",
                    reply_markup=_main_menu_keyboard(),
                )
                return {"ok": True}

            if step == "need_photo_2":
                tp["photo2_bytes"] = img_bytes
                tp["photo2_file_id"] = file_id
                tp["step"] = "need_prompt"
                st["two_photos"] = tp
                st["ts"] = _now()
                await tg_send_message(
                    chat_id,
                    "Фото 2 получил. Теперь одним сообщением напиши, что сделать из этих двух фото.\n"
                    "Пример: «Возьми позу и фон с фото 1, а лицо с фото 2. Реалистично, без текста».",
                    reply_markup=_main_menu_keyboard(),
                )
                return {"ok": True}

            if step == "need_prompt":
                await tg_send_message(
                    chat_id,
                    "Я уже получил 2 фото. Теперь пришли ТЕКСТОМ, что нужно сделать (или /reset).",
                    reply_markup=_main_menu_keyboard(),
                )
                return {"ok": True}

# PHOTOSESSION mode (Seedream/ModelArk)
        if st.get("mode") == "photosession":
            st["photosession"] = {"step": "need_prompt", "photo_bytes": img_bytes, "photo_file_id": file_id}
            st["ts"] = _now()
            await tg_send_message(
                chat_id,
                "Фото получил. Теперь напиши задачу для фотосессии:\n"
                "• где находится человек (место/фон)\n"
                "• стиль/настроение\n"
                "• можно указать одежду/аксессуары\n",
                reply_markup=_main_menu_keyboard(),
            )
            return {"ok": True}

        # VISUAL mode
        if st.get("mode") == "poster":
            # Выбор света для афиши (работает в любом шаге режима «Фото/Афиши»)
            t = incoming_text.strip()
            t_norm = t.replace("✅", "").strip().lower()
            if t_norm in ("афиша: ярко", "ярко"):
                st.setdefault("poster", {})
                st["poster"]["light"] = "bright"
                st["ts"] = _now()
                await tg_send_message(chat_id, "Ок. Для афиш включен режим света: Ярко.", reply_markup=_poster_menu_keyboard("bright"))
                return {"ok": True}
            if t_norm in ("афиша: кино", "кино"):
                st.setdefault("poster", {})
                st["poster"]["light"] = "cinema"
                st["ts"] = _now()
                await tg_send_message(chat_id, "Ок. Для афиш включен режим света: Кино.", reply_markup=_poster_menu_keyboard("cinema"))
                return {"ok": True}

            st["poster"] = {"step": "need_prompt", "photo_bytes": img_bytes, "light": (st.get("poster") or {}).get("light", "bright")}
            st["ts"] = _now()
            await tg_send_message(
                chat_id,
                "Фото получил. Теперь одним сообщением напиши:\n"
                "• для афиши: надпись/цена/стиль (или слово 'афиша')\n"
                "• для обычной картинки: опиши сцену (или 'без текста').",
                reply_markup=_poster_menu_keyboard((st.get("poster") or {}).get("light", "bright"))
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

            # TWO PHOTOS mode
            if st.get("mode") == "two_photos":
                tp = st.get("two_photos") or {}
                step = (tp.get("step") or "need_photo_1")

                if step == "need_photo_1":
                    st["two_photos"] = {
                        "step": "need_photo_2",
                        "photo1_bytes": img_bytes,
                        "photo1_file_id": file_id,
                        "photo2_bytes": None,
                        "photo2_file_id": None,
                    }
                    st["ts"] = _now()
                    await tg_send_message(chat_id, "Фото 1 получил. Теперь пришли Фото 2.", reply_markup=_main_menu_keyboard())
                    return {"ok": True}

                if step == "need_photo_2":
                    tp["photo2_bytes"] = img_bytes
                    tp["photo2_file_id"] = file_id
                    tp["step"] = "need_prompt"
                    st["two_photos"] = tp
                    st["ts"] = _now()
                    await tg_send_message(chat_id, "Фото 2 получил. Теперь напиши текстом, что сделать.", reply_markup=_main_menu_keyboard())
                    return {"ok": True}

                if step == "need_prompt":
                    await tg_send_message(chat_id, "Я уже получил 2 фото. Пришли текстом задачу (или /reset).", reply_markup=_main_menu_keyboard())
                    return {"ok": True}

            if st.get("mode") == "photosession":
                st["photosession"] = {"step": "need_prompt", "photo_bytes": img_bytes, "photo_file_id": file_id}
                st["ts"] = _now()
                await tg_send_message(
                    chat_id,
                    "Фото получил. Теперь напиши задачу для фотосессии:\n"
                    "• где находится человек (место/фон)\n"
                    "• стиль/настроение\n"
                    "• можно указать одежду/аксессуары\n",
                    reply_markup=_main_menu_keyboard(),
                )
                return {"ok": True}

            if st.get("mode") == "poster":
                st["poster"] = {"step": "need_prompt", "photo_bytes": img_bytes, "light": (st.get("poster") or {}).get("light", "bright")}
                st["ts"] = _now()
                await tg_send_message(
                    chat_id,
                    "Фото получил. Теперь одним сообщением напиши:\n"
                    "• для афиши: надпись/цена/стиль (или слово 'афиша')\n"
                    "• для обычной картинки: опиши сцену (или 'без текста').",
                    reply_markup=_poster_menu_keyboard((st.get("poster") or {}).get("light", "bright"))
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

        # TWO PHOTOS: после 2 фото — пользователь пишет инструкцию
        if st.get("mode") == "two_photos":
            tp = st.get("two_photos") or {}
            step = (tp.get("step") or "need_photo_1")
            if step != "need_prompt":
                await tg_send_message(chat_id, "В режиме «2 фото» сначала пришли 2 фото подряд.", reply_markup=_main_menu_keyboard())
                return {"ok": True}

            photo1_file_id = tp.get("photo1_file_id")
            photo2_file_id = tp.get("photo2_file_id")
            if not photo1_file_id or not photo2_file_id:
                await tg_send_message(chat_id, "Не вижу оба фото. Пришли 2 фото заново (или /reset).", reply_markup=_main_menu_keyboard())
                return {"ok": True}

            user_task = incoming_text.strip()
            if not user_task:
                await tg_send_message(chat_id, "Напиши текстом, что сделать из этих 2 фото.", reply_markup=_main_menu_keyboard())
                return {"ok": True}

            await tg_send_message(chat_id, "Делаю генерацию по 2 фото…", reply_markup=_main_menu_keyboard())
            try:
                file_path1 = await tg_get_file_path(photo1_file_id)
                file_path2 = await tg_get_file_path(photo2_file_id)
                url1 = f"{TELEGRAM_FILE_BASE}/{file_path1}"
                url2 = f"{TELEGRAM_FILE_BASE}/{file_path2}"

                prompt = _two_photos_prompt(user_task)

                # Placeholder + fake progress
                placeholder = _make_blur_placeholder(tp.get("photo1_bytes") or b"")
                token = _dl_init_slot(chat_id, user_id)
                msg_id = await tg_send_photo_bytes_return_message_id(chat_id, placeholder, caption="Генерация по 2 фото…", reply_markup=_dl_keyboard(token))
                stop = asyncio.Event()
                prog_task = None
                if msg_id is not None:
                    prog_task = asyncio.create_task(_progress_caption_updater(chat_id, msg_id, "Генерация по 2 фото…", stop))
                else:
                    await tg_send_chat_action(chat_id, "upload_photo")

                _sent_via_edit = False

                out_bytes = await ark_edit_image(
                    source_image_bytes=tp.get("photo1_bytes") or b"",
                    prompt=prompt,
                    size=ARK_SIZE_DEFAULT,
                    mask_png_bytes=None,
                    source_image_urls=[url1, url2],
                )

                _dl_set_bytes(chat_id, user_id, token, out_bytes)

                _dl_set_bytes(chat_id, user_id, token, out_bytes)

                stop.set()
                if prog_task:
                    try:
                        await prog_task
                    except Exception:
                        pass

                if msg_id is not None:
                    try:
                        await tg_edit_message_media_photo(chat_id, msg_id, out_bytes, caption="Готово (2 фото).", reply_markup=_dl_keyboard(token))
                        _sent_via_edit = True
                    except Exception:
                        pass

                if not _sent_via_edit:
                    await tg_send_photo_bytes(chat_id, out_bytes, caption="Готово (2 фото).")
            except Exception as e:
                stop.set()
                if prog_task:
                    try:
                        await prog_task
                    except Exception:
                        pass
                await tg_send_message(
                    chat_id,
                    f"Ошибка 2 фото: {e}\n"
                    "Если ошибка про 'image' / 'invalid' — возможно твой endpoint не поддерживает 2 изображения.\n"
                    "Тогда нужен endpoint с multi-image или другой провайдер.",
                    reply_markup=_main_menu_keyboard(),
                )
            finally:
                # Сбрасываем режим, чтобы можно было сразу начать заново
                _set_mode(chat_id, user_id, "two_photos")
                st["ts"] = _now()

            return {"ok": True}

        # T2I flow: генерация Seedream по одному тексту (без входного фото)
        if st.get("mode") == "t2i":
            t2i = st.get("t2i") or {}
            step = (t2i.get("step") or "need_prompt")
            if step != "need_prompt":
                st["t2i"] = {"step": "need_prompt"}

            user_prompt = incoming_text.strip()
            if not user_prompt:
                await tg_send_message(chat_id, "Напиши описание для генерации (без фото).", reply_markup=_main_menu_keyboard())
                return {"ok": True}

            # Placeholder + fake progress
            placeholder = _make_blur_placeholder(None)
            token = _dl_init_slot(chat_id, user_id)
            msg_id = await tg_send_photo_bytes_return_message_id(chat_id, placeholder, caption="Генерация изображения…", reply_markup=_dl_keyboard(token))
            stop = asyncio.Event()
            prog_task = None
            if msg_id is not None:
                prog_task = asyncio.create_task(_progress_caption_updater(chat_id, msg_id, "Генерация изображения…", stop))
            else:
                await tg_send_chat_action(chat_id, "upload_photo")

            try:
                img_bytes = await ark_text_to_image(prompt=user_prompt, size=ARK_SIZE_DEFAULT)

                _dl_set_bytes(chat_id, user_id, token, img_bytes)

                stop.set()
                if prog_task:
                    try:
                        await prog_task
                    except Exception:
                        pass

                if msg_id is not None:
                    try:
                        await tg_edit_message_media_photo(chat_id, msg_id, img_bytes, caption="Готово.", reply_markup=_dl_keyboard(token))
                    except Exception:
                        await tg_send_photo_bytes(chat_id, img_bytes, caption="Готово.")
                else:
                    await tg_send_photo_bytes(chat_id, img_bytes, caption="Готово.")

            except Exception as e:
                stop.set()
                if prog_task:
                    try:
                        await prog_task
                    except Exception:
                        pass
                await tg_send_message(chat_id, f"Ошибка T2I: {e}", reply_markup=_main_menu_keyboard())
            finally:
                # остаёмся в режиме t2i, чтобы можно было генерировать дальше без повторного выбора
                st["t2i"] = {"step": "need_prompt"}
                st["ts"] = _now()
            return {"ok": True}


        # PHOTOSESSION flow: после фото -> генерация Seedream
        if st.get("mode") == "photosession":
            ps = st.get("photosession") or {}
            step: PosterStep = ps.get("step") or "need_photo"
            photo_bytes = ps.get("photo_bytes")

            if step == "need_photo" or not photo_bytes:
                await tg_send_message(chat_id, "Пришли фото для режима «Нейро фотосессии».", reply_markup=_main_menu_keyboard())
                return {"ok": True}

            # step == need_prompt
            user_task = incoming_text.strip()

            # Усиленный промпт: максимум похожести + фотосессия
            prompt = (
                "Neural photoshoot. Preserve the person's identity and facial features as close as possible to the original photo. "
                "Do not change facial structure. Keep the same person. "
                "High-quality professional photoshoot look: realistic, detailed, natural skin, sharp focus, good lighting, "
                "cinematic but realistic, no artifacts.\n"
                f"Task: {user_task}"
            )

            # Placeholder + fake progress (только для генерации изображений)
            placeholder = _make_blur_placeholder(photo_bytes)
            token = _dl_init_slot(chat_id, user_id)
            msg_id = await tg_send_photo_bytes_return_message_id(chat_id, placeholder, caption="Генерация фотосессии…", reply_markup=_dl_keyboard(token))
            stop = asyncio.Event()
            prog_task = None
            if msg_id is not None:
                prog_task = asyncio.create_task(_progress_caption_updater(chat_id, msg_id, "Генерация фотосессии…", stop))
            else:
                await tg_send_chat_action(chat_id, "upload_photo")

            _sent_via_edit = False
            try:
                photo_file_id = ps.get("photo_file_id")
                source_url = None
                if photo_file_id:
                    file_path = await tg_get_file_path(photo_file_id)
                    source_url = f"{TELEGRAM_FILE_BASE}/{file_path}"

                out_bytes = await ark_edit_image(
                    source_image_bytes=photo_bytes,
                    prompt=prompt,
                    size=ARK_SIZE_DEFAULT,
                    mask_png_bytes=None,
                    source_image_url=source_url,
                )

                _dl_set_bytes(chat_id, user_id, token, out_bytes)

                stop.set()
                if prog_task:
                    try:
                        await prog_task
                    except Exception:
                        pass

                if msg_id is not None:
                    try:
                        await tg_edit_message_media_photo(chat_id, msg_id, out_bytes, caption="Готово.", reply_markup=_dl_keyboard(token))
                        _sent_via_edit = True
                    except Exception:
                        pass

            except Exception as e:
                stop.set()
                if prog_task:
                    try:
                        await prog_task
                    except Exception:
                        pass
                await tg_send_message(chat_id, f"Ошибка нейро-фотосессии: {e}", reply_markup=_main_menu_keyboard())
                # остаёмся в режиме, чтобы пользователь мог попробовать ещё раз
                st["photosession"] = {"step": "need_photo", "photo_bytes": None}
                st["ts"] = _now()
                return {"ok": True}

            if not _sent_via_edit:
                await tg_send_photo_bytes(chat_id, out_bytes, caption="Готово. Если нужно ещё — пришли новое фото.")
            st["photosession"] = {"step": "need_photo", "photo_bytes": None}
            st["ts"] = _now()
            return {"ok": True}
        # VISUAL flow (poster mode): после фото -> роутинг POSTER/PHOTO
        if st.get("mode") == "poster":
            poster = st.get("poster") or {}
            step: PosterStep = poster.get("step") or "need_photo"
            photo_bytes = poster.get("photo_bytes")

            if step == "need_photo" or not photo_bytes:
                await tg_send_message(chat_id, "Сначала пришли фото.", reply_markup=_main_menu_keyboard())
                return {"ok": True}

            if step == "need_prompt":
                # Перехват кнопок выбора света, чтобы они НЕ воспринимались как промпт генерации
                btn = incoming_text.strip().replace("✅", "").strip().lower()
                if btn.startswith("афиша:") or btn in ("ярко", "кино"):
                    st.setdefault("poster", {})
                    if ("ярко" in btn) or (btn == "ярко"):
                        st["poster"]["light"] = "bright"
                        await tg_send_message(
                            chat_id,
                            "Ок. Режим света для афиш: Ярко. Теперь напиши текст для афиши одним сообщением.",
                            reply_markup=_poster_menu_keyboard("bright"),
                        )
                        return {"ok": True}
                    if ("кино" in btn) or (btn == "кино"):
                        st["poster"]["light"] = "cinema"
                        await tg_send_message(
                            chat_id,
                            "Ок. Режим света для афиш: Кино. Теперь напиши текст для афиши одним сообщением.",
                            reply_markup=_poster_menu_keyboard("cinema"),
                        )
                        return {"ok": True}

                mode, _reason = await openai_route_visual_mode(incoming_text)

                if mode == "POSTER":
                    # Placeholder + fake progress (только для генерации изображений)
                    placeholder = _make_blur_placeholder(photo_bytes)
                    token = _dl_init_slot(chat_id, user_id)
                    msg_id = await tg_send_photo_bytes_return_message_id(chat_id, placeholder, caption="Генерация афиши…", reply_markup=_dl_keyboard(token))
                    stop = asyncio.Event()
                    prog_task = None
                    if msg_id is not None:
                        prog_task = asyncio.create_task(_progress_caption_updater(chat_id, msg_id, "Генерация афиши…", stop))
                    else:
                        await tg_send_chat_action(chat_id, "upload_photo")

                    try:
                        spec = await openai_extract_poster_spec(incoming_text)
                        poster_prompt = _poster_prompt_art_director(spec, light=(poster.get("light") or "bright"))
                        out_bytes = await openai_edit_image(
                            photo_bytes,
                            poster_prompt,
                            IMG_SIZE_DEFAULT,
                            mask_png_bytes=None,
                        )

                        _dl_set_bytes(chat_id, user_id, token, out_bytes)

                        stop.set()
                        if prog_task:
                            try:
                                await prog_task
                            except Exception:
                                pass

                        if msg_id is not None:
                            try:
                                await tg_edit_message_media_photo(chat_id, msg_id, out_bytes, caption="Готово (афиша).", reply_markup=_dl_keyboard(token))
                            except Exception:
                                await tg_send_photo_bytes(chat_id, out_bytes, caption="Готово (афиша).")
                        else:
                            await tg_send_photo_bytes(chat_id, out_bytes, caption="Готово (афиша).")

                    except Exception as e:
                        stop.set()
                        if prog_task:
                            try:
                                await prog_task
                            except Exception:
                                pass
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
                    placeholder = _make_blur_placeholder(photo_bytes)
                    token = _dl_init_slot(chat_id, user_id)
                    msg_id = await tg_send_photo_bytes_return_message_id(chat_id, placeholder, caption="Генерация изображения…", reply_markup=_dl_keyboard(token))
                    stop = asyncio.Event()
                    prog_task = None
                    if msg_id is not None:
                        prog_task = asyncio.create_task(_progress_caption_updater(chat_id, msg_id, "Генерация изображения…", stop))
                    else:
                        await tg_send_chat_action(chat_id, "upload_photo")

                    try:
                        out_bytes = await openai_edit_image(photo_bytes, prompt, IMG_SIZE_DEFAULT, mask_png_bytes=mask_png)

                        _dl_set_bytes(chat_id, user_id, token, out_bytes)

                        stop.set()
                        if prog_task:
                            try:
                                await prog_task
                            except Exception:
                                pass

                        if msg_id is not None:
                            try:
                                await tg_edit_message_media_photo(chat_id, msg_id, out_bytes, caption="Готово (без текста).", reply_markup=_dl_keyboard(token))
                            except Exception:
                                await tg_send_photo_bytes(chat_id, out_bytes, caption="Готово (без текста).")
                        else:
                            await tg_send_photo_bytes(chat_id, out_bytes, caption="Готово (без текста).")

                    except Exception as e:
                        stop.set()
                        if prog_task:
                            try:
                                await prog_task
                            except Exception:
                                pass
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
                st["poster"] = {"step": "need_photo", "photo_bytes": None, "light": "bright"}
                st["ts"] = _now()
                return {"ok": True}

            await tg_send_message(chat_id, "Пришли фото, затем одним сообщением текст.", reply_markup=_main_menu_keyboard())
            return {"ok": True}

        # CHAT: обычный текстовый ответ (AI dialog memory: summary + last 10 messages)
if st.get("mode") == "chat":
    summary = _ai_summary_get(st)
    hist = _ai_hist_get(st)

    history_for_model: List[Dict[str, str]] = []
    if summary:
        history_for_model.append({"role": "system", "content": f"Краткое резюме диалога (для контекста):
{summary}"})
    # append last messages
    for m in hist:
        if isinstance(m, dict) and m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str):
            history_for_model.append({"role": m["role"], "content": m["content"]})

    answer = await openai_chat_answer(
        user_text=incoming_text,
        system_prompt=DEFAULT_TEXT_SYSTEM_PROMPT,
        image_bytes=None,
        temperature=0.6,
        max_tokens=700,
        history=history_for_model,
    )

    # store only AI chat dialog (not posters/photos/etc.)
    _ai_hist_add_sync(st, "user", incoming_text)
    _ai_hist_add_sync(st, "assistant", answer)
    await _ai_maybe_summarize(st)

    await tg_send_message(chat_id, answer, reply_markup=_main_menu_keyboard())
    return {"ok": True}

# fallback (should not happen): treat as chat without memory
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
