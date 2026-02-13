import os
import base64
import time
import asyncio
import re
import json
import hashlib
import hmac
import logging
from io import BytesIO
from typing import Optional, Literal, Dict, Any, Tuple, List

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from db_supabase import track_user_activity, get_basic_stats, supabase as sb
from kling_flow import run_motion_control_from_bytes, run_image_to_video_from_bytes
from veo_flow import run_veo_text_to_video, run_veo_image_to_video
from veo_billing import calc_veo_charge, format_veo_charge_line
from billing_db import ensure_user_row, get_balance, add_tokens
from nano_banana import run_nano_banana
from yookassa_flow import create_yookassa_payment

app = FastAPI()
# --- static files (/static/...) ---
app.mount("/static", StaticFiles(directory="static"), name="static")

APP_VERSION = "v7-suno-callback-dedup-fix"
try:
    UVICORN_LOGGER.info("BOOT: main.py %s loaded", APP_VERSION)
except Exception:
    pass


# ---- logging (ensure INFO shows up in Render/Uvicorn logs) ----
# Uvicorn config usually wires handlers for 'uvicorn.*' loggers.
# Using uvicorn.error makes sure our logs are visible even if root logger is WARNING.
UVICORN_LOGGER = logging.getLogger('uvicorn.error')
if UVICORN_LOGGER.level > logging.INFO:
    UVICORN_LOGGER.setLevel(logging.INFO)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# предотвращаем дубли callback'ов от SunoAPI (иногда приходит несколько POST подряд)
_SUNOAPI_CB_DEDUP: dict[str, float] = {}
_SUNOAPI_CB_DEDUP_TTL_SEC = 600.0

# уведомления по стадиям (чтобы не спамить пользователя)
_SUNOAPI_TASK_NOTIFIED: dict[str, set[str]] = {}
_SUNOAPI_TASK_NOTIFIED_TTL_SEC = 3600.0
_SUNOAPI_TASK_NOTIFIED_TS: dict[str, float] = {}


# ---------------- Per-user busy lock (prevents double-charging on concurrent updates) ----------------
# Telegram can deliver multiple updates while a long generation is running (ASGI concurrency).
# We keep a lightweight in-memory "busy" flag per user with TTL to avoid duplicate launches.
_USER_BUSY: dict[int, dict[str, Any]] = {}
_USER_BUSY_TTL_SEC_DEFAULT = 20 * 60  # 20 minutes


def _busy_is_active(user_id: int) -> bool:
    try:
        uid = int(user_id)
    except Exception:
        return False
    rec = _USER_BUSY.get(uid)
    if not rec:
        return False
    until = float(rec.get("until_ts") or 0.0)
    if until and until > time.time():
        return True
    # expired -> cleanup
    _USER_BUSY.pop(uid, None)
    return False


def _busy_kind(user_id: int) -> str:
    try:
        uid = int(user_id)
    except Exception:
        return ""
    rec = _USER_BUSY.get(uid) or {}
    return str(rec.get("kind") or "").strip()


def _busy_start(user_id: int, kind: str, ttl_sec: int = _USER_BUSY_TTL_SEC_DEFAULT) -> None:
    try:
        uid = int(user_id)
    except Exception:
        return
    _USER_BUSY[uid] = {
        "kind": str(kind or "task"),
        "started_ts": time.time(),
        "until_ts": time.time() + max(60, int(ttl_sec)),
    }


def _busy_end(user_id: int) -> None:
    try:
        uid = int(user_id)
    except Exception:
        return
    _USER_BUSY.pop(uid, None)


def _normalize_btn_text(t: str) -> str:
    t = (t or "").strip()
    # remove leading emojis/checkmarks/spaces
    t = t.replace("✅", "").replace("☑️", "").replace("✔️", "").strip()
    return t


def _is_nav_or_menu_text(t: str) -> bool:
    """
    True if the incoming text looks like a navigation/menu command (not a prompt).
    We use it to prevent accidental launches when the user taps buttons during VEO flow.
    """
    s = _normalize_btn_text(t).lower()
    if not s:
        return False
    # common nav / menu buttons
    nav = {
        "наз", "назад", "в меню", "главное меню", "меню", "start", "/start",
        "помощь", "help", "/help",
        "сброс", "reset", "/reset", "отмена", "cancel", "/cancel",
        "ии", "ии чат", "чат", "ai chat", "chatgpt", "gpt",
        "профиль", "баланс", "тарифы", "оплата", "пополнить",
        "фото", "картинка", "изображение", "видео", "видео будущего", "музыка",
    }
    if s in nav:
        return True
    # buttons with emojis often include these words
    markers = [
        "назад", "меню", "помощ", "ии", "чат", "баланс", "пополн", "тариф", "оплат",
        "фото", "видео", "музы", "сгенер", "генерац",
    ]
    return any(m in s for m in markers)



# ---------------- SunoAPI callback (required by SunoAPI.org) ----------------

def _deep_pick_str(val) -> str:
    if not val:
        return ""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, list):
        for x in val:
            u = _deep_pick_str(x)
            if u:
                return u
    if isinstance(val, dict):
        for k in ("url","audio_url","audioUrl","song_url","songUrl","mp3","mp3_url","file","file_url","fileUrl"):
            v = val.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for v in val.values():
            u = _deep_pick_str(v)
            if u:
                return u
    return ""


def _is_http_url(s: str) -> bool:
    try:
        s = (s or "").strip()
    except Exception:
        return False
    return bool(s) and (s.startswith("http://") or s.startswith("https://"))

def _first_http_url(*candidates: str) -> str:
    for c in candidates:
        if isinstance(c, str):
            cc = c.strip()
            if _is_http_url(cc):
                return cc
    return ""

def _suno_sig(uid: int, chat_id: int) -> str:
    secret = (WEBHOOK_SECRET or "change_me").encode("utf-8")
    msg = f"{int(uid)}:{int(chat_id)}".encode("utf-8")
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()

def _build_suno_callback_url(user_id: int, chat_id: int) -> str:
    base = (PUBLIC_BASE_URL or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("PUBLIC_BASE_URL is not set (needed for SunoAPI callBackUrl)")
    sig = _suno_sig(int(user_id), int(chat_id))
    return f"{base}/api/suno/callback?uid={int(user_id)}&chat={int(chat_id)}&sig={sig}"

def _suno_extract_audio_url(payload: dict) -> str:
    """Best-effort extraction of an audio URL from various callback payload shapes.
    IMPORTANT: returns ONLY http(s) URLs (prevents cases like "MP3: text")."""
    if not isinstance(payload, dict):
        return ""

    def pick(val) -> str:
        if isinstance(val, str):
            s = val.strip()
            return s if _is_http_url(s) else ""
        return ""

    # 1) top-level keys
    for k in ("audio_url", "audioUrl", "song_url", "songUrl", "mp3_url", "mp3", "file_url", "fileUrl", "url"):
        u = pick(payload.get(k))
        if u:
            return u

    data = payload.get("data")
    if isinstance(data, dict):
        # 2) data-level keys
        for k in ("audio_url", "audioUrl", "song_url", "songUrl", "mp3_url", "mp3", "file_url", "fileUrl", "url"):
            u = pick(data.get(k))
            if u:
                return u

        # 2.5) SunoAPI callback typical shape: payload.data.data[*].audio_url
        inner = data.get("data")
        if isinstance(inner, list):
            for item in inner:
                if isinstance(item, dict):
                    for kk in ("audio_url", "audioUrl", "stream_audio_url", "streamAudioUrl", "source_audio_url", "sourceAudioUrl",
                               "song_url", "songUrl", "mp3_url", "mp3", "file_url", "fileUrl", "url"):
                        u = pick(item.get(kk))
                        if u:
                            return u

        # 3) SunoAPI.org record-info shape: data.response.data[*].audio_url
        # 3) SunoAPI.org record-info shape: data.response.data[*].audio_url
        resp = data.get("response")
        if isinstance(resp, dict):
            resp_data = resp.get("data")
            if isinstance(resp_data, list):
                for item in resp_data:
                    if isinstance(item, dict):
                        for kk in ("audio_url", "audioUrl", "song_url", "songUrl", "mp3_url", "mp3", "file_url", "fileUrl", "url"):
                            u = pick(item.get(kk))
                            if u:
                                return u
                        deep = _deep_pick_str(item)
                        if deep and _is_http_url(deep):
                            return deep

        # 4) generic deep-pick from typical fields
        out = data.get("output") or data.get("outputs") or data.get("result")
        deep = _deep_pick_str(out)
        if deep and _is_http_url(deep):
            return deep

        # 5) last resort: deep-pick everything in data
        deep2 = _deep_pick_str(data)
        if deep2 and _is_http_url(deep2):
            return deep2

    out2 = payload.get("output") or payload.get("outputs") or payload.get("result")
    deep3 = _deep_pick_str(out2)
    if deep3 and _is_http_url(deep3):
        return deep3

    return ""


@app.post("/api/suno/callback")
async def sunoapi_callback(request: Request):
    qp = dict(request.query_params)
    try:
        uid = int(qp.get("uid", "0"))
        chat_id = int(qp.get("chat", "0"))
    except Exception:
        return Response(status_code=400)

    sig = (qp.get("sig") or "").strip().lower()
    expected_sig = _suno_sig(uid, chat_id) if uid and chat_id else ""
    if not uid or not chat_id or sig != expected_sig:
        # важно: если callback не проходит проверку, мы раньше молча возвращали 403, и ты не видел почему MP3 не прилетает
        try:
            UVICORN_LOGGER.warning("SUNOAPI CALLBACK REJECTED: uid=%s chat=%s sig=%s expected=%s qp=%s", uid, chat_id, sig, expected_sig, qp)
        except Exception:
            pass
        return Response(status_code=403)

    # payload может приходить несколько раз подряд — сразу делаем лог, но дальше защитимся от дублей
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    # расширенное логирование: сырой payload + ключевые поля (нужно, чтобы видеть этап callbackType)
    try:
        UVICORN_LOGGER.info("SUNOAPI CALLBACK RAW: %s", json.dumps(payload, ensure_ascii=False)[:6000])
    except Exception:
        try:
            UVICORN_LOGGER.info("SUNOAPI CALLBACK RAW(fallback): %s", str(payload)[:6000])
        except Exception:
            pass
    
    # аккуратно распарсим ключевые поля (не меняем поведение)
    try:
        cb = payload.get("data") if isinstance(payload, dict) else {}
        cb_type = ""
        if isinstance(cb, dict):
            cb_type = (cb.get("callbackType") or cb.get("callback_type") or cb.get("type") or "").strip()
        inner = cb.get("data") if isinstance(cb, dict) else None
    
        inner_kind = type(inner).__name__
        inner_len = len(inner) if isinstance(inner, list) else (len(inner.keys()) if isinstance(inner, dict) else 0)
    
        first_keys = []
        first_audio = ""
        if isinstance(inner, list) and inner:
            if isinstance(inner[0], dict):
                first_keys = list(inner[0].keys())[:30]
                first_audio = (inner[0].get("audio_url") or inner[0].get("audioUrl") or inner[0].get("url") or "")
    
        UVICORN_LOGGER.info(
            "SUNOAPI CALLBACK PARSED: code=%s task_id=%s callbackType=%s inner=%s len=%s first_keys=%s first_audio=%s",
            payload.get("code"),
            (cb.get("taskId") or cb.get("task_id") or cb.get("id") or ""),
            cb_type,
            inner_kind,
            inner_len,
            first_keys,
            (first_audio or "")[:300],
        )
    except Exception:
        pass

    # ----- дедупликация -----
    # ВАЖНО: SunoAPI может присылать несколько callback'ов с одним и тем же task_id:
    #   1) callbackType=text (без MP3)
    #   2) callbackType=complete (иногда тоже без MP3)
    #   3) следующий complete уже с audio_url
    # Поэтому НЕЛЬЗЯ дедупить просто по task_id — иначе мы «съедим» финальный callback с MP3.
    now_ts = time.time()
    # чистим старые ключи
    try:
        for k, ts in list(_SUNOAPI_CB_DEDUP.items()):
            if now_ts - ts > _SUNOAPI_CB_DEDUP_TTL_SEC:
                _SUNOAPI_CB_DEDUP.pop(k, None)
    except Exception:
        pass

    # task_id будет разобран ниже (после определения callbackType и списка треков)
# ----- определяем стадию callback (callbackType) -----
    cb = payload.get("data") if isinstance(payload, dict) else {}
    cb_type = ""
    task_id = ""
    if isinstance(cb, dict):
        cb_type = (cb.get("callbackType") or cb.get("callback_type") or cb.get("type") or "").strip().lower()
        task_id = (cb.get("task_id") or cb.get("taskId") or cb.get("id") or "").strip()

    # чистим старые уведомления
    try:
        for k, ts in list(_SUNOAPI_TASK_NOTIFIED_TS.items()):
            if (now_ts - ts) > _SUNOAPI_TASK_NOTIFIED_TTL_SEC:
                _SUNOAPI_TASK_NOTIFIED_TS.pop(k, None)
                _SUNOAPI_TASK_NOTIFIED.pop(k, None)
    except Exception:
        pass

    # хелпер: уведомить 1 раз на стадию
    def _notify_once(stage: str) -> bool:
        if not task_id:
            return False
        st = _SUNOAPI_TASK_NOTIFIED.get(task_id)
        if st is None:
            st = set()
            _SUNOAPI_TASK_NOTIFIED[task_id] = st
        if stage in st:
            return False
        st.add(stage)
        _SUNOAPI_TASK_NOTIFIED_TS[task_id] = now_ts
        return True

    # ----- достаем треки СТРОГО из реального callback SunoAPI: payload["data"]["data"] (list) -----
    tracks = []
    try:
        inner_items = cb.get("data") if isinstance(cb, dict) else None
        if isinstance(inner_items, list):
            for it in inner_items:
                if isinstance(it, dict):
                    tracks.append(it)
    except Exception:
        tracks = []

    # если почему-то не нашли (редкие нестандартные формы) — пробуем общий парсер
    if not tracks:
        try:
            tracks = _sunoapi_extract_tracks(payload)
        except Exception:
            tracks = []

    # ----- если это промежуточный callback (например, text), не ругаемся на отсутствие MP3 -----
    # По факту у тебя приходит сначала callbackType=text с пустым audio_url, потом callbackType=complete с mp3.
    if cb_type and cb_type not in ("complete", "success", "succeed", "finished", "done"):
        # сообщим один раз, что ждём MP3 (чтобы пользователь понимал, что всё ок)
        if cb_type in ("text", "lyrics"):
            if _notify_once("text"):
                try:
                    await tg_send_message(chat_id, "✅ SunoAPI: текст готов. Жду callback с MP3 — как только будет трек, отправлю сюда.")
                except Exception:
                    pass
        else:
            if _notify_once(cb_type):
                try:
                    await tg_send_message(chat_id, "⏳ SunoAPI: генерация в процессе. Жду финальный callback с MP3…")
                except Exception:
                    pass

        return {"ok": True}

    # ----- дедупликация отправки MP3 -----
    # Дедупаем ТОЛЬКО по фактическим audio_url (а не по task_id),
    # чтобы не «съедать» поздний callback, где MP3 появляется после первого complete.
    audio_fps: list[str] = []
    try:
        for it in (tracks[:2] if isinstance(tracks, list) else []):
            if isinstance(it, dict):
                u = _first_http_url(
                    it.get("audio_url"), it.get("audioUrl"),
                    it.get("stream_audio_url"), it.get("streamAudioUrl"),
                    it.get("source_audio_url"), it.get("sourceAudioUrl"),
                    it.get("source_stream_audio_url"), it.get("sourceStreamAudioUrl"),
                    it.get("song_url"), it.get("songUrl"),
                    it.get("mp3_url"), it.get("mp3"),
                    it.get("file_url"), it.get("fileUrl"),
                    it.get("url"),
                )
                if u:
                    audio_fps.append(u)
    except Exception:
        audio_fps = []

    if task_id and audio_fps:
        dedup_key2 = f"{task_id}:" + "|".join(audio_fps)
        ts0 = _SUNOAPI_CB_DEDUP.get(dedup_key2)
        if ts0 and (now_ts - ts0) < _SUNOAPI_CB_DEDUP_TTL_SEC:
            return {"ok": True}
        _SUNOAPI_CB_DEDUP[dedup_key2] = now_ts


    if tracks:
        try:
            await tg_send_message(chat_id, "✅ SunoAPI: музыка готова — отправляю треки…")
        except Exception:
            pass

        # отправляем максимум 2 трека
        for i, item in enumerate(tracks[:2], start=1):
            if not isinstance(item, dict):
                continue
            audio_url = _first_http_url(
                item.get("audio_url"), item.get("audioUrl"), item.get("song_url"), item.get("songUrl"),
                item.get("mp3_url"), item.get("mp3"), item.get("file_url"), item.get("fileUrl"), item.get("url")
            )
            image_url = (item.get("image_url") or item.get("imageUrl") or item.get("cover") or item.get("cover_url") or "").strip()
            title = (item.get("title") or "").strip()

            caption = f"🎵 Трек #{i}" + (f" — {title}" if title else "")
            if audio_url:
                try:
                    await tg_send_audio_from_url(chat_id, audio_url, caption=caption, reply_markup=_main_menu_for(uid) if i == 1 else None)
                except Exception as e:
                    try:
                        await tg_send_message(chat_id, f"{caption}\n🎧 MP3: {audio_url}\n(не смог отправить файлом: {e})", reply_markup=_main_menu_for(uid) if i == 1 else None)
                    except Exception:
                        pass
            else:
                try:
                    await tg_send_message(chat_id, f"⚠️ SunoAPI: трек #{i} без audio_url в callback. Проверь логи.", reply_markup=_main_menu_for(uid) if i == 1 else None)
                except Exception:
                    pass

        return {"ok": True}

    # ----- fallback: достаем хотя бы одну ссылку на MP3 -----
    audio_url = _suno_extract_audio_url(payload)
    if audio_url:
        try:
            await tg_send_message(chat_id, "✅ SunoAPI: трек готов, отправляю…")
            await tg_send_audio_from_url(chat_id, audio_url, caption="🎵 Трек (SunoAPI)", reply_markup=_main_menu_for(uid))
        except Exception as e:
            try:
                await tg_send_message(chat_id, f"✅ SunoAPI: трек готов.\n🎧 MP3: {audio_url}\n(не смог отправить файлом: {e})", reply_markup=_main_menu_for(uid))
            except Exception:
                pass
    else:
        try:
            await tg_send_message(chat_id, f"⚠️ SunoAPI: callbackType={cb_type or '?'} task={task_id or '?'} — MP3/ссылку извлечь не удалось. Проверь логи callback.", reply_markup=_main_menu_for(uid))
        except Exception:
            pass

    return {"ok": True}


# --- basic health endpoints (reduce noisy 404 in logs) ---
@app.get("/")
async def root_ok():
    return {"ok": True}

@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)

from fastapi.responses import HTMLResponse

@app.get("/webapp/kling", response_class=HTMLResponse)
async def webapp_kling():
    with open(os.path.join(BASE_DIR, "webapp_kling.html"), "r", encoding="utf-8") as f:
        return f.read()


@app.get("/webapp/music", response_class=HTMLResponse)
async def webapp_music():
    with open(os.path.join(BASE_DIR, "webapp_music.html"), "r", encoding="utf-8") as f:
        return f.read()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
WEBAPP_KLING_URL = os.getenv("WEBAPP_KLING_URL", "https://astrabot-tchj.onrender.com/webapp/kling")
WEBAPP_MUSIC_URL = os.getenv("WEBAPP_MUSIC_URL", "https://astrabot-tchj.onrender.com/webapp/music")
# --- YooKassa (cards/SBP) ---
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "").strip()
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "").strip()
# Return URL after payment on YooKassa hosted page (can be any page; Telegram will still show the success in browser)
YOOKASSA_RETURN_URL = os.getenv("YOOKASSA_RETURN_URL", WEBAPP_MUSIC_URL).strip()
# Optional: explicit webhook URL (if empty, you can set it in YooKassa cabinet; if set, it will be passed to create-payment)
YOOKASSA_WEBHOOK_URL = os.getenv("YOOKASSA_WEBHOOK_URL", "").strip()
# Optional: simple protection for /yookassa/webhook (send as header 'Authorization: Bearer <token>' or 'X-Webhook-Token')
YOOKASSA_WEBHOOK_TOKEN = os.getenv("YOOKASSA_WEBHOOK_TOKEN", "").strip()

def _yookassa_enabled() -> bool:
    return bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY)
PIAPI_API_KEY = os.getenv("PIAPI_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change_me")

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
SUNOAPI_ENABLED = os.getenv("SUNOAPI_ENABLED", "true").lower() in ("1","true","yes","y","on")
SUNOAPI_MODEL_DEFAULT = os.getenv("SUNOAPI_MODEL_DEFAULT", "V4_5ALL")
ADMIN_IDS = set(
    int(x.strip())
    for x in (os.getenv("ADMIN_IDS", "")).split(",")
    if x.strip().isdigit()
)

def _sunoapi_extract_tracks(payload: dict) -> List[dict]:
    """Extract list of track dicts from SunoAPI callback payload.

    Typical SunoAPI callback (as in your Render logs) looks like:
      { "code":200, "data": { "callbackType":"complete", "task_id":"...", "data":[{...track...},{...}] } }

    But sometimes it can be nested like:
      payload["data"]["response"]["data"]  or  payload["data"]["response"]["data"]["data"]

    Returns a list of dicts (each dict is one track item). Empty list means "no tracks found".
    """
    if not isinstance(payload, dict):
        return []

    data = payload.get("data")
    if not isinstance(data, dict):
        return []

    # 1) common: data.data is list of tracks
    inner = data.get("data")
    if isinstance(inner, list):
        return [x for x in inner if isinstance(x, dict)]

    # 2) sometimes: data.response.data is list/dict
    resp = data.get("response")
    if isinstance(resp, dict):
        rdata = resp.get("data")
        if isinstance(rdata, list):
            return [x for x in rdata if isinstance(x, dict)]
        if isinstance(rdata, dict):
            inner2 = rdata.get("data")
            if isinstance(inner2, list):
                return [x for x in inner2 if isinstance(x, dict)]

    # 3) fallback: scan payload for first list of dicts containing audio_url-like keys
    AUDIO_KEYS = ("audio_url","audioUrl","stream_audio_url","streamAudioUrl","source_audio_url","sourceAudioUrl","mp3_url","mp3","url","file_url","fileUrl")

    def _scan(obj):
        if isinstance(obj, dict):
            # If this dict itself looks like a track item, return it
            if any(k in obj for k in AUDIO_KEYS):
                return [obj]
            for v in obj.values():
                res = _scan(v)
                if res:
                    return res
        elif isinstance(obj, list):
            if obj and all(isinstance(x, dict) for x in obj):
                if any(any(k in x for k in AUDIO_KEYS) for x in obj):
                    return obj
            for v in obj:
                res = _scan(v)
                if res:
                    return res
        return []

    found = _scan(payload)
    if isinstance(found, list):
        return [x for x in found if isinstance(x, dict)]
    return []


def _is_admin(user_id: int) -> bool:
    return int(user_id) in ADMIN_IDS


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


# ---------------- Supabase: user state (bot_user_state) ----------------
# Uses shared client from db_supabase.py (service key).
def sb_get_user_state(user_id: int):
    """
    Returns (state, payload_dict) or ("idle", None) if not set / Supabase disabled.
    """
    if sb is None:
        return ("idle", None)
    try:
        r = sb.table("bot_user_state").select("state,payload").eq("telegram_user_id", int(user_id)).limit(1).execute()
        if r.data:
            row = r.data[0] or {}
            return (str(row.get("state") or "idle"), row.get("payload"))
    except Exception:
        pass
    return ("idle", None)


def sb_set_user_state(user_id: int, state: str, payload: dict | None = None):
    if sb is None:
        return
    try:
        sb.table("bot_user_state").upsert(
            {
                "telegram_user_id": int(user_id),
                "state": str(state or "idle"),
                "payload": payload,
            },
            on_conflict="telegram_user_id",
        ).execute()
    except Exception:
        pass


def sb_clear_user_state(user_id: int):
    if sb is None:
        return
    try:
        sb.table("bot_user_state").upsert(
            {
                "telegram_user_id": int(user_id),
                "state": "idle",
                "payload": None,
            },
            on_conflict="telegram_user_id",
        ).execute()
    except Exception:
        pass


# ---------------- Supabase: user email for YooKassa receipts (bot_user_contacts) ----------------
# Таблица: bot_user_contacts(telegram_user_id bigint PK, email text, updated_at timestamptz default now()).
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$", re.IGNORECASE)

def sb_get_user_email(user_id: int) -> str:
    if sb is None:
        return ""
    try:
        r = sb.table("bot_user_contacts").select("email").eq("telegram_user_id", int(user_id)).limit(1).execute()
        if r.data:
            em = str((r.data[0] or {}).get("email") or "").strip()
            return em
    except Exception:
        pass
    return ""

def sb_set_user_email(user_id: int, email: str) -> bool:
    email = (email or "").strip().lower()
    if not email or not _EMAIL_RE.match(email):
        return False
    if sb is None:
        return False
    try:
        sb.table("bot_user_contacts").upsert(
            {"telegram_user_id": int(user_id), "email": email},
            on_conflict="telegram_user_id",
        ).execute()
        return True
    except Exception:
        return False

# ---------------- Stars top-up (XTR) ----------------
# Токены — внутренняя логика.
# Режим (STD/PRO) выбирается ПОЗЖЕ в WebApp и влияет ТОЛЬКО на расход токенов при генерации.
# Оплата: Stars (XTR)

# Пакеты (Вариант A) — продаём токены. Никаких STD/PRO на этапе оплаты.
# Пример UX:
#  💎 18 токенов — 99⭐ (≈ 180 ₽)
#  🔥 36 токенов — 199⭐ (≈ 360 ₽)
#  🚀 72 токенов — 399⭐ (≈ 720 ₽)
#  ⭐ 160 токенов — 799⭐ (≈ 1450 ₽)
#  👑 303 токенов — 1499⭐ (≈ 2700 ₽)
#
# Примечание:
# ⭐ Stars — временный способ оплаты
# ₽ — расчётная стоимость в рублях
# Токены — внутренняя единица сервиса
TOPUP_PACKS = [
    {"tokens": 18,  "stars": 99},
    {"tokens": 36,  "stars": 199},
    {"tokens": 72,  "stars": 399},
    {"tokens": 160, "stars": 799},
    {"tokens": 303, "stars": 1499},
]

def _find_pack_by_tokens(tokens: int) -> Optional[Dict[str, int]]:
    try:
        t = int(tokens)
    except Exception:
        return None
    for p in TOPUP_PACKS:
        if int(p.get("tokens", 0)) == t:
            return p
    return None

def _topup_balance_inline_kb() -> dict:
    return {"inline_keyboard": [[{"text": "➕ Пополнить ⭐", "callback_data": "topup:menu"}]]}

def _topup_packs_kb() -> dict:
    # 2 кнопки в ряд
    btns = []
    for p in TOPUP_PACKS:
        tokens = int(p["tokens"])
        stars = int(p["stars"])
        btns.append({
            "text": f"≈{int(round(stars * 1.82))}₽ • {tokens} токенов • {stars}⭐",
            "callback_data": f"topup:pack:{tokens}"
        })

    def chunk(items, n=2):
        return [items[i:i+n] for i in range(0, len(items), n)]

    kb = [
        [{"text": "Выбери пакет токенов:", "callback_data": "noop"}],
    ]
    kb += chunk(btns, 2)
    return {"inline_keyboard": kb}

async def tg_send_stars_invoice(chat_id: int, title: str, description: str, payload: str, stars: int):
    """Send Stars invoice (currency XTR)."""
    body = {
        "chat_id": str(chat_id),
        "title": title,
        "description": description,
        "payload": payload,
        "currency": "XTR",
        "prices": [{"label": title, "amount": int(stars)}],
        # For Telegram Stars provider_token must be empty string
        "provider_token": "",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(f"{TELEGRAM_API_BASE}/sendInvoice", json=body)
        try:
            j = r.json()
        except Exception:
            j = {}
        if not isinstance(j, dict) or not j.get("ok"):
            raise RuntimeError(f"sendInvoice failed: {r.status_code} {r.text[:800]}")

# --- YooKassa helpers (payments in RUB: cards + SBP on hosted checkout) ---
_YK_PROCESSED: Dict[str, float] = {}  # payment_id -> ts

def _yk_cleanup_processed(now_ts: Optional[float] = None, ttl_seconds: int = 7 * 24 * 3600) -> None:
    now = float(now_ts or time.time())
    dead = [pid for pid, ts in _YK_PROCESSED.items() if (now - float(ts)) > ttl_seconds]
    for pid in dead:
        _YK_PROCESSED.pop(pid, None)

async def yookassa_create_payment(*, amount_rub: int, description: str, user_id: int, tokens: int) -> Dict[str, Any]:
    """
    Creates a YooKassa payment with redirect confirmation.
    User will choose a payment method on YooKassa page (cards / SBP if enabled in your shop).
    """
    if amount_rub <= 0:
        raise ValueError("amount_rub must be > 0")

    # Idempotence-Key protects against double create on retries
    idem_key = hashlib.sha256(f"{user_id}:{tokens}:{amount_rub}:{time.time_ns()}".encode("utf-8")).hexdigest()

    body: Dict[str, Any] = {
        "amount": {"value": f"{amount_rub:.2f}", "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": YOOKASSA_RETURN_URL or WEBAPP_MUSIC_URL},
        "capture": True,
        "description": description[:128],
        "metadata": {"user_id": str(user_id), "tokens": str(tokens), "amount_rub": str(amount_rub)},
    }
    if YOOKASSA_WEBHOOK_URL:
        body["notification_url"] = YOOKASSA_WEBHOOK_URL

    auth = (YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)
    headers = {"Idempotence-Key": idem_key}

    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.post("https://api.yookassa.ru/v3/payments", json=body, auth=auth, headers=headers)
        try:
            j = r.json()
        except Exception:
            j = {}
        if r.status_code >= 300:
            raise RuntimeError(f"YooKassa create payment failed: {r.status_code} {r.text[:800]}")
        if not isinstance(j, dict):
            raise RuntimeError("YooKassa create payment: bad JSON")
        return j

def _yk_extract_confirmation_url(payment_json: Dict[str, Any]) -> str:
    conf = (payment_json.get("confirmation") or {}) if isinstance(payment_json, dict) else {}
    url = (conf.get("confirmation_url") or "").strip()
    return url




# ---------------- In-memory state ----------------
STATE_TTL_SECONDS = int(os.getenv("STATE_TTL_SECONDS", "1800"))  # 30 минут
STATE: Dict[Tuple[int, int], Dict[str, Any]] = {}

# ---------------- AI chat memory (in-RAM, only for mode=chat) ----------------
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

    # Cleanup expired per-user state
    expired_state = []
    for k, v in list(STATE.items()):
        try:
            ts = float(v.get("ts", 0) or 0)
        except Exception:
            ts = 0.0
        if now - ts > STATE_TTL_SECONDS:
            expired_state.append(k)
    for k in expired_state:
        STATE.pop(k, None)

    # Cleanup download slots stored in per-user state
    for _k, _v in list(STATE.items()):
        dl = _v.get("dl")
        if isinstance(dl, dict):
            expired_tokens = []
            for tok, meta in dl.items():
                try:
                    ts2 = float((meta or {}).get("ts", 0) or 0)
                except Exception:
                    ts2 = 0.0
                if now - ts2 > STATE_TTL_SECONDS:
                    expired_tokens.append(tok)
            for tok in expired_tokens:
                dl.pop(tok, None)

    # Cleanup AI chat memory after TTL (only stored for mode=chat)
    for _k, _v in list(STATE.items()):
        try:
            ts_ai = float(_v.get("ai_ts", 0) or 0)
        except Exception:
            ts_ai = 0.0
        if ts_ai and (now - ts_ai > AI_CHAT_TTL_SECONDS):
            _v.pop("ai_hist", None)
            _v.pop("ai_pending", None)
            _v.pop("ai_summary", None)
            _v.pop("ai_ts", None)

    # Anti-duplicate caches
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


def _ai_hist_add(st: Dict[str, Any], role: str, content: str):
    """Add message to AI chat memory. Keeps last AI_CHAT_HISTORY_MAX messages.
    Overflow messages are moved to ai_pending for later summarization."""
    if role not in ("user", "assistant"):
        return
    if not isinstance(content, str) or not content.strip():
        return

    if "ai_hist" not in st or not isinstance(st.get("ai_hist"), list):
        st["ai_hist"] = []
    st["ai_hist"].append({"role": role, "content": content.strip()})
    st["ai_ts"] = _now()

    hist = st["ai_hist"]
    if len(hist) > AI_CHAT_HISTORY_MAX:
        overflow = hist[:-AI_CHAT_HISTORY_MAX]
        st["ai_hist"] = hist[-AI_CHAT_HISTORY_MAX:]
        pending = _ai_pending_get(st)
        pending.extend(overflow)
        # hard cap to avoid RAM bloat
        if len(pending) > 200:
            pending[:] = pending[-200:]


async def _ai_build_summary_chunk(chunk: List[Dict[str, str]], prev_summary: str) -> str:
    """Summarize a chunk of messages and merge into previous summary."""
    sys = (
        "Ты сжимаешь историю диалога для Telegram-бота. Пиши коротко и по делу. "
        "Сохраняй: цель пользователя, важные факты, договоренности, текущий контекст. "
        "Не добавляй новых фактов и не выдумывай."
    )

    lines: List[str] = []
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
    """If pending has enough messages, update ai_summary and trim pending."""
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


def _set_mode(chat_id: int, user_id: int, mode: Literal["chat", "poster", "photosession", "t2i", "two_photos", "nano_banana", "kling_mc", "kling_i2v", "suno_music", "veo_t2v", "veo_i2v"]):
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


    elif mode == "nano_banana":
        # Nano Banana (Replicate): image editing
        st["nano_banana"] = {"step": "need_photo", "photo_bytes": None}

    elif mode == "veo_t2v":
        st["veo_t2v"] = {"step": "need_prompt"}

    elif mode == "veo_i2v":
        st["veo_i2v"] = {
            "step": "need_image",
            "image_bytes": None,
            "last_frame_bytes": None,
            "reference_images_bytes": [],
            "prompt": None,
        }


    else:
        # chat
        st.pop("poster", None)
        st.pop("photosession", None)
        st.pop("t2i", None)
        st.pop("two_photos", None)
        st.pop("nano_banana", None)
        st.pop("veo_t2v", None)
        st.pop("veo_i2v", None)



# ---------------- Reply keyboard ----------------


# ---------------- PiAPI (Suno) helpers ----------------
PIAPI_BASE_URL = "https://api.piapi.ai"

async def piapi_create_task(payload: dict) -> dict:
    """
    POST /api/v1/task
    Returns full JSON response as dict.
    """
    if not PIAPI_API_KEY:
        raise RuntimeError("PIAPI_API_KEY is empty. Set it in Render env vars.")
    url = f"{PIAPI_BASE_URL}/api/v1/task"
    headers = {"X-API-Key": PIAPI_API_KEY, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        return r.json()

async def piapi_get_task(task_id: str) -> dict:
    """
    GET /api/v1/task/{task_id}
    """
    if not PIAPI_API_KEY:
        raise RuntimeError("PIAPI_API_KEY is empty. Set it in Render env vars.")
    url = f"{PIAPI_BASE_URL}/api/v1/task/{task_id}"
    headers = {"X-API-Key": PIAPI_API_KEY}
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(url, headers=headers)
        r.raise_for_status()
        return r.json()

async def piapi_poll_task(task_id: str, *, timeout_sec: int = 240, sleep_sec: float = 2.0) -> dict:
    """
    Simple polling loop:
    - checks status every sleep_sec seconds
    - stops on Completed / Failed
    - raises on timeout
    """
    t0 = time.time()
    last = None
    while True:
        last = await piapi_get_task(task_id)
        status = ((last.get("data") or {}).get("status") or "").lower()
        if status in ("completed", "failed"):
            return last
        if time.time() - t0 > timeout_sec:
            raise TimeoutError(f"PiAPI task timeout after {timeout_sec}s (task_id={task_id}, status={status})")
        await asyncio.sleep(sleep_sec)

# ---------------- SunoAPI.org (alternative Suno aggregator) ----------------
SUNOAPI_API_KEY = os.getenv("SUNOAPI_API_KEY", "").strip()
SUNOAPI_BASE_URL = os.getenv("SUNOAPI_BASE_URL", "https://api.sunoapi.org/api/v1").rstrip("/")
# Normalize if user set SUNOAPI_BASE_URL without /api/v1
if SUNOAPI_BASE_URL.rstrip("/") == "https://api.sunoapi.org":
    SUNOAPI_BASE_URL = "https://api.sunoapi.org/api/v1"
SUNOAPI_CALLBACK_URL = os.getenv("SUNOAPI_CALLBACK_URL", "").strip()  # optional; if empty we'll just poll
SUNOAPI_POLL_TIMEOUT_SEC = int(os.getenv("SUNOAPI_POLL_TIMEOUT_SEC", "600"))

async def sunoapi_generate_task(*, prompt: str, custom_mode: bool, instrumental: bool, model: str,
                               user_id: int, chat_id: int,
                               title: str = "", style: str = "") -> str:
    """Create generation task on SunoAPI.org and return taskId."""
    if not SUNOAPI_API_KEY:
        raise RuntimeError("SUNOAPI_API_KEY is empty. Set it in Render env vars.")
    url = f"{SUNOAPI_BASE_URL}/generate"
    payload = {
        "prompt": prompt,
        "customMode": bool(custom_mode),
        "instrumental": bool(instrumental),
        "model": model,
    }
    # In customMode, docs require title+style; we pass if provided
    if title:
        payload["title"] = title
    if style:
        payload["style"] = style
    # SunoAPI requires callBackUrl. Prefer explicit env override; otherwise build dynamic callback URL.
    payload["callBackUrl"] = SUNOAPI_CALLBACK_URL or _build_suno_callback_url(int(user_id), int(chat_id))
    headers = {"Authorization": f"Bearer {SUNOAPI_API_KEY}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        js = r.json()
    if js.get("code") != 200:
        raise RuntimeError(f"SunoAPI generate failed: {js}")
    task_id = (((js.get("data") or {}) .get("taskId")) or "").strip()
    if not task_id:
        raise RuntimeError(f"SunoAPI did not return taskId: {js}")
    return task_id

async def sunoapi_get_task(task_id: str) -> dict:
    if not SUNOAPI_API_KEY:
        raise RuntimeError("SUNOAPI_API_KEY is empty. Set it in Render env vars.")
    url = f"{SUNOAPI_BASE_URL}/generate/record-info"
    headers = {"Authorization": f"Bearer {SUNOAPI_API_KEY}"}
    params = {"taskId": task_id}
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(url, headers=headers, params=params)
        r.raise_for_status()
        return r.json()

async def sunoapi_poll_task(task_id: str, *, timeout_sec: int | None = None, sleep_sec: float = 2.0) -> dict:
    """Poll SunoAPI record-info until SUCCESS/FAILED."""
    if timeout_sec is None:
        timeout_sec = SUNOAPI_POLL_TIMEOUT_SEC
    t0 = time.time()
    last = None
    while True:
        last = await sunoapi_get_task(task_id)
        data = last.get("data") or {}
        status = str(data.get("status") or "").upper().strip()
        if status in ("SUCCESS", "FAILED", "ERROR"):
            return last
        if time.time() - t0 > timeout_sec:
            raise TimeoutError(f"SunoAPI task timeout after {timeout_sec}s (taskId={task_id}, status={status})")
        await asyncio.sleep(sleep_sec)

def _sunoapi_extract_tracks(task_json: dict) -> list[dict]:
    """Normalize SunoAPI task response to list of tracks with audio_url/image_url/title/duration."""
    data = (task_json.get("data") or {})
    resp = (data.get("response") or {})
    resp_data = (resp.get("data") or [])
    if isinstance(resp_data, list):
        return [x for x in resp_data if isinstance(x, dict)]
    return []

def _main_menu_keyboard(is_admin: bool = False) -> dict:
    rows = [
        [{"text": "ИИ (чат)"}, {"text": "Фото будущего"}],
        [
            {"text": "🎬 Видео будущего", "web_app": {"url": WEBAPP_KLING_URL}},
            {"text": "🎵 Музыка будущего", "web_app": {"url": WEBAPP_MUSIC_URL}},
        ],
        [{"text": "💰 Баланс"}, {"text": "Помощь"}],
    ]
    if is_admin:
        rows.append([{"text": "📊 Статистика"}])

    return {
        "keyboard": rows,
        "resize_keyboard": True,
        "one_time_keyboard": False,
        "selective": False,
    }


def _main_menu_for(user_id: int) -> dict:
    return _main_menu_keyboard(_is_admin(user_id))


def _help_menu_for(user_id: int) -> dict:
    """Главное меню + экстренная кнопка сброса генерации (показываем ТОЛЬКО в «Помощь»)."""
    base = _main_menu_keyboard(_is_admin(user_id))
    # defensive copy
    rows = [list(r) for r in (base.get("keyboard") or [])]
    rows.append([{"text": "🔄 Сбросить генерацию"}])
    base2 = dict(base)
    base2["keyboard"] = rows
    return base2


def _photo_future_menu_keyboard() -> dict:
    """Подменю «Фото будущего» (объединяет фото-режимы в одну кнопку на главном экране)."""
    return {
        "keyboard": [
            [{"text": "Фото/Афиши"}, {"text": "Нейро фотосессии"}],
            [{"text": "2 фото"}, {"text": "🍌 Nano Banana"}],
            [{"text": "⬅ Назад"}],
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


async def tg_send_photo_bytes(
    chat_id: int,
    image_bytes: bytes,
    caption: Optional[str] = None,
    reply_markup: Optional[dict] = None,
):
    if not TELEGRAM_BOT_TOKEN:
        return
    files = {"photo": ("image.png", image_bytes, "image/png")}
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)

    async with httpx.AsyncClient(timeout=180) as client:
        await client.post(f"{TELEGRAM_API_BASE}/sendPhoto", data=data, files=files)



async def tg_send_audio_bytes(
    chat_id: int,
    audio_bytes: bytes,
    filename: str = "track.mp3",
    caption: Optional[str] = None,
    reply_markup: Optional[dict] = None,
):
    """Отправляет MP3 как audio-сообщение (с плеером)."""
    if not TELEGRAM_BOT_TOKEN:
        return
    files = {"audio": (filename, audio_bytes, "audio/mpeg")}
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    async with httpx.AsyncClient(timeout=180) as client:
        await client.post(f"{TELEGRAM_API_BASE}/sendAudio", data=data, files=files)


async def tg_send_document_bytes(
    chat_id: int,
    file_bytes: bytes,
    filename: str,
    mime: str = "application/octet-stream",
    caption: Optional[str] = None,
    reply_markup: Optional[dict] = None,
):
    """Фолбэк: отправка как document, если sendAudio не подходит."""
    if not TELEGRAM_BOT_TOKEN:
        return
    files = {"document": (filename, file_bytes, mime)}
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    async with httpx.AsyncClient(timeout=180) as client:
        await client.post(f"{TELEGRAM_API_BASE}/sendDocument", data=data, files=files)


async def tg_send_audio_from_url(
    chat_id: int,
    url: str,
    caption: Optional[str] = None,
    reply_markup: Optional[dict] = None,
):
    """Скачивает MP3 по ссылке и отправляет файлом. Если слишком большой — шлёт ссылку."""
    try:
        content = await http_download_bytes(url, timeout=180)
        # лимит Bot API на загрузку файлов обычно 50MB; оставим запас
        if len(content) > 48 * 1024 * 1024:
            await tg_send_message(chat_id, f"🎧 MP3: {url}", reply_markup=reply_markup)
            return
        try:
            await tg_send_audio_bytes(chat_id, content, filename="track.mp3", caption=caption, reply_markup=reply_markup)
        except Exception:
            # иногда Telegram может отвергнуть как audio — отправим как документ
            await tg_send_document_bytes(chat_id, content, filename="track.mp3", mime="audio/mpeg", caption=caption, reply_markup=reply_markup)
    except Exception:
        await tg_send_message(chat_id, f"🎧 MP3: {url}", reply_markup=reply_markup)


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


async def tg_send_photo_bytes_return_message_id(
    chat_id: int,
    image_bytes: bytes,
    caption: Optional[str] = None,
    reply_markup: Optional[dict] = None,
) -> Optional[int]:
    """
    sendPhoto, но возвращает message_id (нужен для editMessageCaption/editMessageMedia).
    """
    if not TELEGRAM_BOT_TOKEN:
        return None
    files = {"photo": ("image.png", image_bytes, "image/png")}
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)

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


def tg_build_file_url(file_path: str) -> str:
    """Public URL for Telegram file (for services that can fetch by URL)."""
    return f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"


async def tg_send_video_url(chat_id: int, video_url: str, caption: str | None = None, reply_markup: dict | None = None):
    """Send video by URL (Telegram will fetch). Falls back to text with link if fails."""
    if not TELEGRAM_BOT_TOKEN:
        return
    payload = {"chat_id": chat_id, "video": video_url}
    if caption:
        payload["caption"] = caption
    if reply_markup:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{TELEGRAM_API_BASE}/sendVideo", json=payload)
    if r.status_code >= 400:
        await tg_send_message(chat_id, f"✅ Готово! Видео: {video_url}", reply_markup=reply_markup)


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

    # IMAGE + TEXT (Vision)
    if image_bytes is not None:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        user_content: List[Dict[str, Any]] = []
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
        # TEXT ONLY (Chat) + optional history
        msgs: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
        if history:
            for m in history:
                if (
                    isinstance(m, dict)
                    and m.get("role") in ("system", "user", "assistant")
                    and isinstance(m.get("content"), str)
                    and m["content"].strip()
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



@app.post("/sunoapi/callback")
async def sunoapi_callback(req: Request):
    """Optional callback endpoint for SunoAPI.org.
    If you set SUNOAPI_CALLBACK_URL to point here, SunoAPI will POST results.
    We keep this endpoint lightweight; the bot currently uses polling by default.
    """
    try:
        payload = await req.json()
    except Exception:
        payload = {}
    # For safety, you can protect it with a secret query param if you want.
    # Example: https://your-server/sunoapi/callback?secret=...
    secret = (req.query_params.get("secret") or "").strip()
    expected = os.getenv("SUNOAPI_CALLBACK_SECRET", "").strip()
    if expected and secret != expected:
        return Response("forbidden", status_code=403)
    print("SunoAPI callback received:", payload if isinstance(payload, dict) else str(type(payload)))
    return {"ok": True}

@app.post("/api/yookassa/webhook")
@app.post("/yookassa/webhook")
async def yookassa_webhook(request: Request):
    """
    YooKassa payment notifications.
    Expected event: payment.succeeded (we also accept generic objects with status=succeeded).
    """
    # Optional simple token check (recommended)
    if YOOKASSA_WEBHOOK_TOKEN:
        auth = (request.headers.get("authorization") or "").strip()
        token = (request.headers.get("x-webhook-token") or "").strip()
        ok = False
        if auth.lower().startswith("bearer "):
            ok = auth.split(" ", 1)[1].strip() == YOOKASSA_WEBHOOK_TOKEN
        if token and token == YOOKASSA_WEBHOOK_TOKEN:
            ok = True
        if not ok:
            return Response(status_code=401, content="unauthorized")

    try:
        payload = await request.json()
    except Exception:
        payload = {}

    event = (payload.get("event") or payload.get("type") or "").strip()
    obj = payload.get("object") if isinstance(payload, dict) else None
    if not isinstance(obj, dict):
        return {"ok": True}

    status = (obj.get("status") or "").strip()
    payment_id = (obj.get("id") or "").strip()

    # We process only succeeded payments
    if status != "succeeded" and event != "payment.succeeded":
        return {"ok": True}

    if not payment_id:
        return {"ok": True}

    _yk_cleanup_processed()
    if payment_id in _YK_PROCESSED:
        return {"ok": True}
    _YK_PROCESSED[payment_id] = time.time()

    md = obj.get("metadata") or {}
    try:
        uid = int(md.get("user_id") or 0)
        tokens = int(md.get("tokens") or 0)
    except Exception:
        uid = 0
        tokens = 0

    # If metadata missing, do nothing (to avoid giving tokens to wrong user)
    if uid <= 0 or tokens <= 0:
        if ADMIN_IDS:
            try:
                admin_id = next(iter(ADMIN_IDS))
                await tg_send_message(admin_id, f"⚠️ YooKassa webhook: no metadata user_id/tokens. payment_id={payment_id} payload={json.dumps(payload)[:1500]}")
            except Exception:
                pass
        return {"ok": True}

    try:
        ensure_user_row(uid)
        add_tokens(
            uid,
            tokens,
            reason="yookassa_topup",
            meta={"payment_id": payment_id, "event": event, "status": status, "metadata": md},
        )
        bal = int(get_balance(uid) or 0)
        # Notify user
        await tg_send_message(uid, f"✅ Оплата ЮKassa прошла!\\nНачислено: +{tokens} токенов\\nБаланс: {bal}", reply_markup=_help_menu_for(uid))
    except Exception as e:
        if ADMIN_IDS:
            try:
                admin_id = next(iter(ADMIN_IDS))
                await tg_send_message(admin_id, f"❌ YooKassa начисление упало: {e}\\nuser={uid} payment_id={payment_id}")
            except Exception:
                pass

    return {"ok": True}

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
        # 📊 Supabase: user + DAU tracking
        track_user_activity(from_user)
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

        # --- Balance topup (Stars) ---
        if chat_id and user_id and data.startswith("topup:"):
            # topup:menu
            if data == "topup:menu":
                await tg_send_message(
                    chat_id,
                    "💳 Пополнение баланса сервиса — выбери пакет:",
                    reply_markup=_topup_packs_kb(),
                )
                return {"ok": True}

            # topup:pack:<tokens>
            parts = data.split(":")
            if len(parts) >= 3 and parts[1] == "pack":
                try:
                    tokens = int(parts[2])
                except Exception:
                    tokens = 0

                pack = _find_pack_by_tokens(tokens)
                if not pack:
                    await tg_send_message(chat_id, "Пакет не найден. Нажми «Баланс» → «Пополнить» ещё раз.")
                    return {"ok": True}

                stars = int(pack["stars"])
                amount_rub = int(round(stars * 1.82))
                title = f"Пополнение баланса: {tokens} токенов"
                description = f"{tokens} токенов • {amount_rub}₽ (оплата картой/СБП)"

                if _yookassa_enabled():
                    try:
                        # Для сервиса «Чеки от ЮKassa» часто обязателен email покупателя.
                        email = sb_get_user_email(user_id)
                        if not email:
                            # Запоминаем выбранный пакет и просим email (переживает рестарт Render)
                            sb_set_user_state(user_id, "yk_wait_email", {"tokens": int(tokens), "amount_rub": int(amount_rub), "title": title})
                            await tg_send_message(
                                chat_id,
                                "📧 Для оплаты мне нужен email для чека.\n"
                                "Пришли email одним сообщением (пример: name@gmail.com).\n\n"
                                "После этого я сразу пришлю кнопку оплаты.",
                                reply_markup=_help_menu_for(user_id),
                            )
                            return {"ok": True}

                        payment_id, url = await create_yookassa_payment(
                            amount_rub=amount_rub,
                            description=title,
                            user_id=user_id,
                            tokens=tokens,
                            customer_email=email,
                        )
                        if not url:
                            raise RuntimeError("no confirmation_url from YooKassa")
                        await tg_send_message(
                            chat_id,
                            f"💳 Оплата через ЮKassa\nСумма: {amount_rub}₽\nПакет: {tokens} токенов\n\nНажми кнопку ниже, чтобы оплатить (карта / СБП):",
                            reply_markup={"inline_keyboard": [[{"text": f"Оплатить {amount_rub}₽", "url": url}]]},
                        )
                    except Exception as e:
                        await tg_send_message(chat_id, f"Не смог создать платёж ЮKassa: {e}\nПопробуй ещё раз.", reply_markup=_topup_packs_kb())
                    return {"ok": True}

                # fallback (Telegram Stars)
                payload = f"stars_topup:{tokens}:{user_id}"
                await tg_send_stars_invoice(chat_id, title, f"{tokens} токенов • {stars}⭐ (≈{amount_rub}₽)", payload, stars)
                return {"ok": True}

            # Unknown topup callback: ignore silently
            return {"ok": True}

# --- Stars: pre-checkout (must answer within ~10 seconds) ---
    pre = update.get("pre_checkout_query")
    if pre:
        cq_id = pre.get("id")
        if cq_id:
            async with httpx.AsyncClient(timeout=15) as client:
                await client.post(
                    f"{TELEGRAM_API_BASE}/answerPreCheckoutQuery",
                    json={"pre_checkout_query_id": str(cq_id), "ok": True},
                )
        return {"ok": True}


    message = update.get("message") or update.get("edited_message")
    if not message:
        return {"ok": True}

    chat = message.get("chat", {})
    chat_id = int(chat.get("id") or 0)

    from_user = message.get("from") or {}
    user_id = int(from_user.get("id") or 0)
    # 📊 Supabase: user + DAU tracking (для любых сообщений/режимов)
    track_user_activity(from_user)


    if not chat_id or not user_id:
        return {"ok": True}


    # --- Stars: successful payment ---
    sp = (message.get("successful_payment") or {})
    if sp:
        payload = (sp.get("invoice_payload") or "").strip()
        currency = (sp.get("currency") or "").strip()
        total_amount = sp.get("total_amount")
        tg_charge_id = (sp.get("telegram_payment_charge_id") or "").strip()

        # Debug to logs (Render)
        print("STARS_SUCCESSFUL_PAYMENT:", {"currency": currency, "payload": payload, "total_amount": total_amount, "charge_id": tg_charge_id})

        if currency != "XTR":
            await tg_send_message(chat_id, f"Оплата получена, но валюта не XTR: {currency}", reply_markup=_main_menu_for(user_id))
            return {"ok": True}

        if not payload.startswith("stars_topup:"):
            if ADMIN_IDS:
                try:
                    admin_id = next(iter(ADMIN_IDS))
                    await tg_send_message(admin_id, f"⚠️ Stars payment payload не распознан: {payload} (user {user_id})")
                except Exception:
                    pass
            await tg_send_message(chat_id, "Оплата прошла, но я не понял платёж. Напиши админу.", reply_markup=_main_menu_for(user_id))
            return {"ok": True}

        # Supported payload:
        # stars_topup:<tokens>:<user_id>
        parts = payload.split(":")
        try:
            tokens = int(parts[1]) if len(parts) >= 3 else 0
            uid_pay = int(parts[2]) if len(parts) >= 3 else 0
        except Exception:
            tokens = 0
            uid_pay = 0

        if tokens <= 0 or uid_pay <= 0:
            if ADMIN_IDS:
                try:
                    admin_id = next(iter(ADMIN_IDS))
                    await tg_send_message(admin_id, f"⚠️ Stars payload parse failed: {payload} (user {user_id})")
                except Exception:
                    pass
            await tg_send_message(chat_id, "Оплата прошла, но я не смог обработать платёж. Напиши админу.", reply_markup=_main_menu_for(user_id))
            return {"ok": True}

        if uid_pay != user_id:
            await tg_send_message(chat_id, "Оплата прошла, но user_id не совпал. Напиши админу.", reply_markup=_main_menu_for(user_id))
            return {"ok": True}

        try:
            ensure_user_row(user_id)
            add_tokens(
                user_id,
                tokens,
                reason="stars_topup",
                meta={"tokens": tokens, "currency": "XTR", "payload": payload, "charge_id": tg_charge_id},
            )
            bal = int(get_balance(user_id) or 0)

            await tg_send_message(
                chat_id,
                f"✅ Оплата прошла!\nНачислено: +{tokens} токенов\nБаланс: {bal}",
                reply_markup=_help_menu_for(user_id),
            )
        except Exception as e:
            if ADMIN_IDS:
                try:
                    admin_id = next(iter(ADMIN_IDS))
                    await tg_send_message(admin_id, f"❌ Stars начисление упало: {e}\nuser={user_id} payload={payload}")
                except Exception:
                    pass
            await tg_send_message(chat_id, f"Оплата прошла, но не смог начислить токены: {e}", reply_markup=_main_menu_for(user_id))
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

    # Execution guard: while a long generation is running, ignore accidental navigation/button texts
    # so they do not get interpreted as prompts and start a second generation.
    if _busy_is_active(int(user_id)) and _is_nav_or_menu_text(incoming_text):
        kind = _busy_kind(int(user_id)) or "генерация"
        await tg_send_message(
            chat_id,
            f"⏳ Сейчас выполняется: {kind}. Я не запускаю новую генерацию от кнопок/навигации. Дождись завершения (или /reset).",
            reply_markup=_main_menu_for(user_id),
        )
        return {"ok": True}


    # ----- Supabase state resume (Music Future) -----
    # Если бот перезапустился, режим "ожидаем текст для музыки" берём из Supabase.
    if incoming_text and not (incoming_text.startswith("/") or incoming_text in ("⬅ Назад", "Назад")):
        sb_state, sb_payload = sb_get_user_state(user_id)
        if sb_state == "music_wait_text" and isinstance(sb_payload, dict) and sb_payload:
            st["music_settings"] = sb_payload
            _set_mode(chat_id, user_id, "suno_music")

    # ----- Supabase state resume (YooKassa: wait for email) -----
    # Если пользователь выбрал пакет и у нас нет email для чека — просим email и продолжаем оплату после ввода.
    if incoming_text and not (incoming_text.startswith("/") or incoming_text in ("⬅ Назад", "Назад")):
        sb_state, sb_payload = sb_get_user_state(user_id)
        if sb_state == "yk_wait_email" and isinstance(sb_payload, dict):
            email = (incoming_text or "").strip().lower()
            if sb_set_user_email(user_id, email):
                # очищаем state и создаём платёж сразу
                try:
                    tokens = int(sb_payload.get("tokens") or 0)
                    amount_rub = int(sb_payload.get("amount_rub") or 0)
                    title = str(sb_payload.get("title") or f"Пополнение баланса: {tokens} токенов")
                    sb_clear_user_state(user_id)

                    payment_id, url = await create_yookassa_payment(
                        amount_rub=amount_rub,
                        description=title,
                        user_id=user_id,
                        tokens=tokens,
                        customer_email=email,
                    )
                    await tg_send_message(
                        chat_id,
                        f"💳 Оплата через ЮKassa\nСумма: {amount_rub}₽\nПакет: {tokens} токенов\n\nНажми кнопку ниже, чтобы оплатить (карта / СБП):",
                        reply_markup={"inline_keyboard": [[{"text": f"Оплатить {amount_rub}₽", "url": url}]]},
                    )
                except Exception as e:
                    await tg_send_message(chat_id, f"Не смог создать платёж ЮKassa: {e}\nПопробуй ещё раз: «Баланс» → «Пополнить».", reply_markup=_help_menu_for(user_id))
                return {"ok": True}
            else:
                await tg_send_message(chat_id, "Не похоже на email 😅\nПришли корректный email одним сообщением (пример: name@gmail.com).")
                return {"ok": True}



    # ----- WebApp data (Kling settings) -----
    web_app_data = message.get("web_app_data") or {}
    if isinstance(web_app_data, dict) and web_app_data.get("data"):
        raw = web_app_data.get("data")
        try:
            payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception:
            payload = {"raw": raw}

        # ----- WebApp data (Music settings) -----
        # Поддерживаем разные версии WebApp payload:
        # 1) legacy: {"flow":"music","task_type":"music","music_mode":"prompt|custom", ...}
        # 2) v3 simple: {"feature":"music_future","model":"suno","mode":"idea|lyrics", ...}
        flow_raw = (payload.get("flow") or "").lower().strip()
        task_type_raw = (payload.get("task_type") or "").lower().strip()
        feature_raw = (payload.get("feature") or "").lower().strip()
        model_raw = (payload.get("model") or "").lower().strip()
        provider_raw = (payload.get("provider") or "").lower().strip()
        # 🔒 Жёсткий маркер: если WebApp прислал music_settings — это точно музыка
        if str(payload.get("type") or "").lower().strip() == "music_settings":
            feature_raw = "music_future"
            flow_raw = "music"
            task_type_raw = "music"

        ai_raw = (payload.get("ai") or payload.get("ai_provider") or payload.get("aiProvider") or "").lower().strip()

        is_music = (
            flow_raw == "music"
            or task_type_raw == "music"
            or feature_raw in ("music_future", "music")
            or (model_raw == "suno" and (provider_raw in ("piapi", "") or True))
        )

        if is_music:
            # нормализуем режим
            raw_mode = (payload.get("music_mode") or payload.get("mode") or "prompt")
            raw_mode = str(raw_mode).lower().strip()

            # поддержка: idea->prompt, lyrics->custom
            if raw_mode in ("idea", "prompt", "description", "prompt_mode", "gpt"):
                music_mode = "prompt"
            elif raw_mode in ("lyrics", "custom", "lyric", "text"):
                music_mode = "custom"
            else:
                music_mode = "prompt"

            mv = str(payload.get("mv") or "chirp-crow").strip()
            title = str(payload.get("title") or "").strip()
            tags = str(payload.get("tags") or "").strip()
            make_instrumental = bool(payload.get("make_instrumental"))

            gpt_desc = str(payload.get("gpt_description_prompt") or payload.get("description") or "").strip()
            lyrics_text = str(payload.get("prompt") or payload.get("lyrics") or "").strip()

            service_mode = str(payload.get("service_mode") or "public").strip()
            language = str(payload.get("language") or "").strip()

            # выбор провайдера/сервера для Suno: "piapi" или "sunoapi"
            provider_choice = str(
                payload.get("provider")
                or payload.get("server")
                or payload.get("service")
                or payload.get("api")
                or payload.get("ai_provider")
                or payload.get("aiProvider")
                or provider_raw
                or ""
            ).lower().strip()
            if provider_choice in ("suno-api", "suno_api", "suno api"):
                provider_choice = "sunoapi"
            if provider_choice not in ("piapi", "sunoapi"):
                provider_choice = os.getenv("MUSIC_PROVIDER_DEFAULT", "piapi").lower().strip() or "piapi"
                if provider_choice not in ("piapi", "sunoapi"):
                    provider_choice = "piapi"

            # сохраняем настройки музыки
            st["music_settings"] = {
                "mv": mv,
                "music_mode": music_mode,
                "title": title,
                "tags": tags,
                "make_instrumental": make_instrumental,
                "gpt_description_prompt": gpt_desc,
                "prompt": lyrics_text,
                "service_mode": service_mode,
                "language": language,
                "ai": (ai_raw or "suno"),
                "provider": provider_choice,
            }
            st["ts"] = _now()
            _set_mode(chat_id, user_id, "suno_music")

            # если промпт/лирика уже пришли из WebApp — можно стартовать сразу
            settings = st["music_settings"]
            have_desc = bool(settings.get("gpt_description_prompt"))
            have_lyrics = bool(settings.get("prompt"))

            if not (have_desc or have_lyrics):
                # сохраняем ожидание текста в Supabase (переживает рестарт Render)
                sb_set_user_state(user_id, "music_wait_text", settings)
                await tg_send_message(
                    chat_id,
                    """✅ Настройки музыки сохранены.

Теперь пришли текстом:
• в режиме «Идея» — короткое описание песни (жанр/вайб/тема)
• в режиме «Текст» — текст/лирику с пометками [Verse]/[Chorus]

После этого я отправлю задачу в AI музыки (Suno/Udio) через выбранный провайдер (PiAPI/SunoAPI).""",
                    reply_markup=_help_menu_for(user_id),
                )
                return {"ok": True}

            # сформировать payload для PiAPI и запустить
            input_block = {
                "mv": settings["mv"],
                "title": settings["title"],
                "tags": settings["tags"],
                "make_instrumental": settings["make_instrumental"],
            }
            if settings["music_mode"] == "prompt":
                input_block["gpt_description_prompt"] = settings["gpt_description_prompt"]
            else:
                input_block["prompt"] = settings["prompt"]

            ai_choice = str((settings.get("ai") or "suno")).lower().strip()
            if ai_choice not in ("suno", "udio"):
                ai_choice = "suno"

            if ai_choice == "udio":
                # PiAPI Udio-like (music-u): используем ТОЛЬКО идею (gpt_description_prompt).
                # Не доверяем music_mode: в стейте мог остаться "custom" от прошлых запусков,
                # а у Udio текста песни в WebApp нет -> иначе улетит пустота и PiAPI часто отвечает 500.
                udio_prompt = (
                    (settings.get("gpt_description_prompt") or "").strip()
                    or (settings.get("prompt") or "").strip()
                )
                if not udio_prompt:
                    udio_prompt = "Modern atmospheric music with emotional melody"

                payload_api = {
                    "model": "music-u",
                    "task_type": "generate_music",
                    "input": {
                        "gpt_description_prompt": udio_prompt,
                        "lyrics_type": "instrumental" if settings.get("make_instrumental") else "generate",
                    },
                    # ВАЖНО: для music-u лучше явно ставить public, иначе PiAPI часто возвращает только file_id,
                    # и тогда нужно отдельное скачивание файла.
                    "config": {"service_mode": (settings.get("service_mode") or "public")},
                }
            else:
                payload_api = {
                    "model": "suno",
                    "task_type": "music",
                    "input": input_block,
                    "config": {"service_mode": settings["service_mode"]},
                }

            # генерация стартует — ожидание текста больше не нужно
            sb_clear_user_state(user_id)

            def _clear_music_ctx():
                # Полный сброс музыкального контекста, чтобы не было автоповтора на любой текст/кнопку.
                try:
                    st.pop("music_settings", None)
                except Exception:
                    pass
                try:
                    _set_mode(chat_id, user_id, "chat")
                except Exception:
                    pass
                try:
                    sb_clear_user_state(user_id)
                except Exception:
                    pass

            await tg_send_message(chat_id, "⏳ Запускаю генерацию музыки…")
            try:
                # provider selection:
                # - "piapi" (default): current path
                # - "sunoapi": alternative Suno aggregator (docs.sunoapi.org)
                provider = str(settings.get("provider") or settings.get("api") or settings.get("ai_provider") or settings.get("aiProvider") or "").lower().strip()
                if not provider:
                    provider = os.getenv("MUSIC_PROVIDER_DEFAULT", "piapi").lower().strip()

                # Udio is only available through PiAPI in this build
                if ai_choice == "udio":
                    provider = "piapi"

                async def _run_piapi():
                    created_local = await piapi_create_task(payload_api)
                    task_id_local = ((created_local.get("data") or {}).get("task_id")) or ""
                    if not task_id_local:
                        raise RuntimeError(f"PiAPI did not return task_id: {created_local}")
                    done_local = await piapi_poll_task(task_id_local, timeout_sec=300, sleep_sec=2.0)
                    return ("piapi", done_local)

                async def _run_sunoapi():
                    # Map our settings -> SunoAPI.org request
                    prompt_text = (settings.get("gpt_description_prompt") or "").strip() if settings.get("music_mode") == "prompt" else (settings.get("prompt") or "").strip()
                    if not prompt_text:
                        prompt_text = "A modern catchy song with clear structure and strong hook"
                    mv_local = str(settings.get("mv") or "").lower().strip()
                    model_enum = "V4_5ALL"
                    if "v5" in mv_local:
                        model_enum = "V5"
                    elif "v4_5" in mv_local or "v4.5" in mv_local or "v4-5" in mv_local:
                        model_enum = "V4_5ALL"
                    elif "v4" in mv_local:
                        model_enum = "V4"
                    custom_mode = bool(settings.get("music_mode") != "prompt")
                    instrumental = bool(settings.get("make_instrumental"))
                    title_local = (settings.get("title") or "").strip()
                    style_local = (settings.get("tags") or "").strip()
                    task_id_local = await sunoapi_generate_task(
                        prompt=prompt_text,
                        custom_mode=custom_mode,
                        instrumental=instrumental,
                        model=model_enum,
                        user_id=user_id,
                        chat_id=chat_id,
                        title=title_local,
                        style=style_local,
                    )
                    done_local = await sunoapi_poll_task(task_id_local, timeout_sec=SUNOAPI_POLL_TIMEOUT_SEC, sleep_sec=2.0)
                    return ("sunoapi", done_local)

                # provider может быть: 'sunoapi', 'piapi', 'auto'
                provider_norm = provider if provider in ("piapi", "sunoapi", "auto") else "auto"
                default_primary = os.getenv("MUSIC_PROVIDER_DEFAULT", "piapi").lower().strip()
                if default_primary not in ("piapi", "sunoapi"):
                    default_primary = "piapi"
                primary = (default_primary if provider_norm == "auto" else provider_norm)
                secondary = ("sunoapi" if primary == "piapi" else "piapi")

                try:
                    if primary == "sunoapi":
                        source, done = await _run_sunoapi()
                    else:
                        source, done = await _run_piapi()
                except Exception as e_primary:
                    # fallback допускаем ТОЛЬКО в режиме auto
                    if provider_norm != "auto":
                        await tg_send_message(chat_id, f"❌ Провайдер {primary} вернул ошибку: {e_primary}")
                        _clear_music_ctx()
                        return {"ok": True}

                    can_fallback = (secondary == "sunoapi" and bool(SUNOAPI_API_KEY)) or (secondary == "piapi" and bool(PIAPI_API_KEY))
                    if not can_fallback:
                        await tg_send_message(chat_id, f"❌ Провайдер {primary} упал, а запасной {secondary} недоступен: {e_primary}")
                        _clear_music_ctx()
                        return {"ok": True}

                    await tg_send_message(chat_id, f"⚠️ Основной провайдер ({primary}) упал: {e_primary}\nПробую запасной ({secondary})…")
                    if secondary == "sunoapi":
                        source, done = await _run_sunoapi()
                    else:
                        source, done = await _run_piapi()

# Normalize result for both providers
                if source == "sunoapi":
                    data = done.get("data") or {}
                    status = str(data.get("status") or "").upper().strip()
                    if status not in ("SUCCESS",):
                        await tg_send_message(
                            chat_id,
                            f"❌ Музыка не сгенерировалась (SunoAPI).\nСтатус: {status}\n{done.get('msg') or 'unknown error'}\n\n"
                            "Генерация остановлена. Нажмите «Музыка будущего», чтобы попробовать снова."
                        )
                        _clear_music_ctx()
                        return {"ok": True}

                    out = _sunoapi_extract_tracks(done)
                    if not out:
                        await tg_send_message(chat_id, "⏳ SunoAPI: задача завершена, но ссылки на треки ещё не пришли. Жду callback — как только будет MP3, отправлю сюда.")
                        _clear_music_ctx()
                        return {"ok": True}
                else:
                    data = done.get("data") or {}
                    status = (data.get("status") or "")
                    if str(status).lower() != "completed":
                        err = (data.get("error") or {}).get("message") or "unknown error"
                        await tg_send_message(chat_id, f"❌ Музыка не сгенерировалась.\nСтатус: {status}\n{err}\n\nГенерация остановлена. Нажмите «Музыка будущего», чтобы попробовать снова.")
                        _clear_music_ctx()
                        return {"ok": True}

                    out = data.get("output") or []
                    if isinstance(out, dict):
                        out = [out]
                    if not out:
                        await tg_send_message(chat_id, "✅ Готово, но PiAPI не вернул output. Я сбросил режим, попробуйте снова через «Музыка будущего».")
                        _clear_music_ctx()
                        return {"ok": True}

                # Отправка результата: стараемся отправить MP3 файлом (плеер), а не только ссылкой.
                def _pick_first_url(val) -> str:
                    if not val:
                        return ""
                    if isinstance(val, str):
                        return val
                    if isinstance(val, dict):
                        for k in ("url", "audio_url", "audioUrl", "song_url", "songUrl", "mp3", "mp3_url"):
                            v = val.get(k)
                            if isinstance(v, str) and v.strip():
                                return v.strip()
                        # иногда лежит глубже
                        for v in val.values():
                            u = _pick_first_url(v)
                            if u:
                                return u
                    if isinstance(val, list):
                        for x in val:
                            u = _pick_first_url(x)
                            if u:
                                return u
                    return ""

                def _extract_audio_url(item: dict) -> str:
                    if not isinstance(item, dict):
                        return ""
                    # прямые варианты
                    for k in ("audio_url", "audioUrl", "song_url", "songUrl", "url"):
                        v = item.get(k)
                        if isinstance(v, str) and v.strip():
                            return v.strip()
                    # часто audio = {"url": ...} или audio = [...]
                    u = _pick_first_url(item.get("audio"))
                    if u:
                        return u
                    # иногда ключи во множественном числе
                    for k in ("audio_urls", "audios", "urls", "songs"):
                        u = _pick_first_url(item.get(k))
                        if u:
                            return u
                    return ""

                await tg_send_message(chat_id, "✅ Музыка готова:", reply_markup=None)

                # Отправляем максимум 2 трека, чтобы не спамить.
                for i, item in enumerate(out[:2], start=1):
                    audio_url = _extract_audio_url(item)
                    video_url = _pick_first_url(item.get("video_url") or item.get("video") or item.get("mp4") or item.get("videoUrl"))
                    image_url = _pick_first_url(item.get("image_url") or item.get("image") or item.get("cover") or item.get("imageUrl"))

                    if audio_url:
                        # Кнопки меню повесим на первый отправленный трек/сообщение
                        markup = _main_menu_for(user_id) if i == 1 else None
                        await tg_send_audio_from_url(
                            chat_id,
                            audio_url,
                            caption=f"🎵 Трек #{i}",
                            reply_markup=markup,
                        )
                    else:
                        # Если PiAPI не дал ссылку — покажем, что пришло (коротко)
                        keys = ", ".join(list(item.keys())[:15]) if isinstance(item, dict) else str(type(item))
                        await tg_send_message(chat_id, f"⚠️ Трек #{i}: PiAPI не вернул ссылку на MP3. Поля: {keys}", reply_markup=_main_menu_for(user_id) if i == 1 else None)

                    extra_lines = []
                    if video_url:
                        extra_lines.append(f"🎬 MP4: {video_url}")
                    if extra_lines:
                        await tg_send_message(chat_id, "\n".join(extra_lines), reply_markup=None)
                _clear_music_ctx()
            except Exception as e:
                await tg_send_message(chat_id, f"❌ Ошибка PiAPI (music): {e}\n\nГенерация остановлена. Нажмите «Музыка будущего», чтобы попробовать снова.")
                _clear_music_ctx()
            return {"ok": True}

        

        # ----- WebApp data (Veo settings) -----
        # Expected (from our WebApp): {type:"veo_settings", provider:"veo", veo_model:"fast|pro", flow:"text|image",
        # duration, aspect_ratio, generate_audio, resolution, use_last_frame, use_reference_images}
        is_veo = (
            (str(payload.get("type") or "").lower().strip() == "veo_settings")
            or (provider_raw == "veo")
            or (feature_raw in ("video_future", "video"))
        ) and (str(payload.get("provider") or provider_raw or "").lower().strip() == "veo")

        if is_veo:
            veo_model = str(payload.get("veo_model") or payload.get("model") or "fast").lower().strip()
            if veo_model not in ("fast", "pro", "veo-3.1", "3.1"):
                veo_model = "fast"
            # map aliases
            if veo_model in ("veo-3.1", "3.1"):
                veo_model = "pro"

            flow = str(payload.get("flow") or "text").lower().strip()
            if flow not in ("text", "image"):
                flow = "text"

            try:
                duration = int(payload.get("duration") or 8)
            except Exception:
                duration = 8
            if duration not in (4, 6, 8):
                duration = 8

            aspect_ratio = str(payload.get("aspect_ratio") or "16:9").strip()
            if aspect_ratio not in ("16:9", "9:16"):
                aspect_ratio = "16:9"

            generate_audio = bool(payload.get("generate_audio"))

            # FAST фикс 720p, PRO 1080p
            resolution = str(payload.get("resolution") or ("1080p" if veo_model == "pro" else "720p")).lower().strip()
            if veo_model == "pro":
                resolution = "1080p"
            else:
                resolution = "720p"

            use_last_frame = bool(payload.get("use_last_frame")) if veo_model == "pro" else False
            use_reference_images = bool(payload.get("use_reference_images")) if veo_model == "pro" else False

            st["veo_settings"] = {
                "model": ("veo-3.1" if veo_model == "pro" else "veo-3-fast"),
                "veo_model": veo_model,
                "flow": flow,
                "duration": duration,
                "aspect_ratio": aspect_ratio,
                "resolution": resolution,
                "generate_audio": generate_audio,
                "use_last_frame": use_last_frame,
                "use_reference_images": use_reference_images,
            }
            st["ts"] = _now()

            if flow == "text":
                _set_mode(chat_id, user_id, "veo_t2v")
                st["veo_t2v"] = {"step": "need_prompt"}
                st["ts"] = _now()
                await tg_send_message(
                    chat_id,
                    """✅ Настройки Veo сохранены.

Теперь пришли ТЕКСТ (промпт), что должно быть в видео.
Пример: «Кот в скафандре идёт по Марсу, кинематографично». """,
                    reply_markup=_help_menu_for(user_id),
                )
                return {"ok": True}
            else:
                _set_mode(chat_id, user_id, "veo_i2v")
                st["veo_i2v"] = {
                    "step": "need_image",
                    "image_bytes": None,
                    "last_frame_bytes": None,
                    "reference_images_bytes": [],
                    "prompt": None,
                }
                st["ts"] = _now()
                extra = []
                if use_last_frame:
                    extra.append("• last frame (финальный кадр)")
                if use_reference_images:
                    extra.append("• референсы (до 4)")
                extra_txt = ("\nДополнительно понадобится: " + ", ".join(extra)) if extra else ""
                await tg_send_message(
                    chat_id,
                    "✅ Настройки Veo сохранены (Image → Video).\n\nШаг 1) Пришли СТАРТОВОЕ фото (кадр 1)." + extra_txt,
                    reply_markup=_help_menu_for(user_id),
                )
                return {"ok": True}


            # сформировать payload для PiAPI и запустить
            input_block = {
                "mv": settings["mv"],
                "title": settings["title"],
                "tags": settings["tags"],
                "make_instrumental": settings["make_instrumental"],
            }
            if settings["music_mode"] == "prompt":
                input_block["gpt_description_prompt"] = settings["gpt_description_prompt"]
            else:
                input_block["prompt"] = settings["prompt"]

            ai_choice = str((settings.get("ai") or "suno")).lower().strip()
            if ai_choice not in ("suno", "udio"):
                ai_choice = "suno"

            if ai_choice == "udio":
                # PiAPI Udio-like (music-u): используем ТОЛЬКО идею (gpt_description_prompt).
                # Не доверяем music_mode: в стейте мог остаться "custom" от прошлых запусков,
                # а у Udio текста песни в WebApp нет -> иначе улетит пустота и PiAPI часто отвечает 500.
                udio_prompt = (
                    (settings.get("gpt_description_prompt") or "").strip()
                    or (settings.get("prompt") or "").strip()
                )
                if not udio_prompt:
                    udio_prompt = "Modern atmospheric music with emotional melody"

                payload_api = {
                    "model": "music-u",
                    "task_type": "generate_music",
                    "input": {
                        "gpt_description_prompt": udio_prompt,
                        "lyrics_type": "instrumental" if settings.get("make_instrumental") else "generate",
                    },
                    # ВАЖНО: для music-u лучше явно ставить public, иначе PiAPI часто возвращает только file_id,
                    # и тогда нужно отдельное скачивание файла.
                    "config": {"service_mode": (settings.get("service_mode") or "public")},
                }
            else:
                payload_api = {
                    "model": "suno",
                    "task_type": "music",
                    "input": input_block,
                    "config": {"service_mode": settings["service_mode"]},
                }

            # генерация стартует — ожидание текста больше не нужно
            sb_clear_user_state(user_id)

            def _clear_music_ctx():
                # Полный сброс музыкального контекста, чтобы не было автоповтора на любой текст/кнопку.
                try:
                    st.pop("music_settings", None)
                except Exception:
                    pass
                try:
                    _set_mode(chat_id, user_id, "chat")
                except Exception:
                    pass
                try:
                    sb_clear_user_state(user_id)
                except Exception:
                    pass

            await tg_send_message(chat_id, "⏳ Запускаю генерацию музыки…")
            try:
                # provider selection:
                # - "piapi" (default): current path
                # - "sunoapi": alternative Suno aggregator (docs.sunoapi.org)
                provider = str(settings.get("provider") or settings.get("api") or settings.get("ai_provider") or settings.get("aiProvider") or "").lower().strip()
                if not provider:
                    provider = os.getenv("MUSIC_PROVIDER_DEFAULT", "piapi").lower().strip()

                # Udio is only available through PiAPI in this build
                if ai_choice == "udio":
                    provider = "piapi"

                async def _run_piapi():
                    created_local = await piapi_create_task(payload_api)
                    task_id_local = ((created_local.get("data") or {}).get("task_id")) or ""
                    if not task_id_local:
                        raise RuntimeError(f"PiAPI did not return task_id: {created_local}")
                    done_local = await piapi_poll_task(task_id_local, timeout_sec=300, sleep_sec=2.0)
                    return ("piapi", done_local)

                async def _run_sunoapi():
                    # Map our settings -> SunoAPI.org request
                    prompt_text = (settings.get("gpt_description_prompt") or "").strip() if settings.get("music_mode") == "prompt" else (settings.get("prompt") or "").strip()
                    if not prompt_text:
                        prompt_text = "A modern catchy song with clear structure and strong hook"
                    mv_local = str(settings.get("mv") or "").lower().strip()
                    model_enum = "V4_5ALL"
                    if "v5" in mv_local:
                        model_enum = "V5"
                    elif "v4_5" in mv_local or "v4.5" in mv_local or "v4-5" in mv_local:
                        model_enum = "V4_5ALL"
                    elif "v4" in mv_local:
                        model_enum = "V4"
                    custom_mode = bool(settings.get("music_mode") != "prompt")
                    instrumental = bool(settings.get("make_instrumental"))
                    title_local = (settings.get("title") or "").strip()
                    style_local = (settings.get("tags") or "").strip()
                    task_id_local = await sunoapi_generate_task(
                        prompt=prompt_text,
                        custom_mode=custom_mode,
                        instrumental=instrumental,
                        model=model_enum,
                        user_id=user_id,
                        chat_id=chat_id,
                        title=title_local,
                        style=style_local,
                    )
                    done_local = await sunoapi_poll_task(task_id_local, timeout_sec=SUNOAPI_POLL_TIMEOUT_SEC, sleep_sec=2.0)
                    return ("sunoapi", done_local)

                # provider может быть: 'sunoapi', 'piapi', 'auto'
                provider_norm = provider if provider in ("piapi", "sunoapi", "auto") else "auto"
                default_primary = os.getenv("MUSIC_PROVIDER_DEFAULT", "piapi").lower().strip()
                if default_primary not in ("piapi", "sunoapi"):
                    default_primary = "piapi"
                primary = (default_primary if provider_norm == "auto" else provider_norm)
                secondary = ("sunoapi" if primary == "piapi" else "piapi")

                try:
                    if primary == "sunoapi":
                        source, done = await _run_sunoapi()
                    else:
                        source, done = await _run_piapi()
                except Exception as e_primary:
                    # fallback допускаем ТОЛЬКО в режиме auto
                    if provider_norm != "auto":
                        await tg_send_message(chat_id, f"❌ Провайдер {primary} вернул ошибку: {e_primary}")
                        _clear_music_ctx()
                        return {"ok": True}

                    can_fallback = (secondary == "sunoapi" and bool(SUNOAPI_API_KEY)) or (secondary == "piapi" and bool(PIAPI_API_KEY))
                    if not can_fallback:
                        await tg_send_message(chat_id, f"❌ Провайдер {primary} упал, а запасной {secondary} недоступен: {e_primary}")
                        _clear_music_ctx()
                        return {"ok": True}

                    await tg_send_message(chat_id, f"⚠️ Основной провайдер ({primary}) упал: {e_primary}\nПробую запасной ({secondary})…")
                    if secondary == "sunoapi":
                        source, done = await _run_sunoapi()
                    else:
                        source, done = await _run_piapi()

# Normalize result for both providers
                if source == "sunoapi":
                    data = done.get("data") or {}
                    status = str(data.get("status") or "").upper().strip()
                    if status not in ("SUCCESS",):
                        await tg_send_message(
                            chat_id,
                            f"❌ Музыка не сгенерировалась (SunoAPI).\nСтатус: {status}\n{done.get('msg') or 'unknown error'}\n\n"
                            "Генерация остановлена. Нажмите «Музыка будущего», чтобы попробовать снова."
                        )
                        _clear_music_ctx()
                        return {"ok": True}

                    out = _sunoapi_extract_tracks(done)
                    if not out:
                        await tg_send_message(chat_id, "⏳ SunoAPI: задача завершена, но ссылки на треки ещё не пришли. Жду callback — как только будет MP3, отправлю сюда.")
                        _clear_music_ctx()
                        return {"ok": True}
                else:
                    data = done.get("data") or {}
                    status = (data.get("status") or "")
                    if str(status).lower() != "completed":
                        err = (data.get("error") or {}).get("message") or "unknown error"
                        await tg_send_message(chat_id, f"❌ Музыка не сгенерировалась.\nСтатус: {status}\n{err}\n\nГенерация остановлена. Нажмите «Музыка будущего», чтобы попробовать снова.")
                        _clear_music_ctx()
                        return {"ok": True}

                    out = data.get("output") or []
                    if isinstance(out, dict):
                        out = [out]
                    if not out:
                        await tg_send_message(chat_id, "✅ Готово, но PiAPI не вернул output. Я сбросил режим, попробуйте снова через «Музыка будущего».")
                        _clear_music_ctx()
                        return {"ok": True}

                # Отправка результата: стараемся отправить MP3 файлом (плеер), а не только ссылкой.
                def _pick_first_url(val) -> str:
                    if not val:
                        return ""
                    if isinstance(val, str):
                        return val
                    if isinstance(val, dict):
                        for k in ("url", "audio_url", "audioUrl", "song_url", "songUrl", "mp3", "mp3_url"):
                            v = val.get(k)
                            if isinstance(v, str) and v.strip():
                                return v.strip()
                        # иногда лежит глубже
                        for v in val.values():
                            u = _pick_first_url(v)
                            if u:
                                return u
                    if isinstance(val, list):
                        for x in val:
                            u = _pick_first_url(x)
                            if u:
                                return u
                    return ""

                def _extract_audio_url(item: dict) -> str:
                    if not isinstance(item, dict):
                        return ""
                    # прямые варианты
                    for k in ("audio_url", "audioUrl", "song_url", "songUrl", "url"):
                        v = item.get(k)
                        if isinstance(v, str) and v.strip():
                            return v.strip()
                    # часто audio = {"url": ...} или audio = [...]
                    u = _pick_first_url(item.get("audio"))
                    if u:
                        return u
                    # иногда ключи во множественном числе
                    for k in ("audio_urls", "audios", "urls", "songs"):
                        u = _pick_first_url(item.get(k))
                        if u:
                            return u
                    return ""
                try:
                    await tg_send_message(
                        chat_id,
                        "DEBUG PiAPI data.output:\n" + json.dumps((data.get("output") or {}), ensure_ascii=False)[:3500]
                    )
                except Exception:
                    pass

                try:
                    print("DEBUG PiAPI data.output =", json.dumps((data.get("output") or {}), ensure_ascii=False)[:3500])
                except Exception as e:
                    print("DEBUG PiAPI dump failed:", e)

                await tg_send_message(chat_id, "✅ Музыка готова:", reply_markup=None)

                # Отправляем максимум 2 трека, чтобы не спамить.
                for i, item in enumerate(out[:2], start=1):
                    audio_url = _extract_audio_url(item)
                    video_url = _pick_first_url(item.get("video_url") or item.get("video") or item.get("mp4") or item.get("videoUrl"))
                    image_url = _pick_first_url(item.get("image_url") or item.get("image") or item.get("cover") or item.get("imageUrl"))

                    if audio_url:
                        # Кнопки меню повесим на первый отправленный трек/сообщение
                        markup = _main_menu_for(user_id) if i == 1 else None
                        await tg_send_audio_from_url(
                            chat_id,
                            audio_url,
                            caption=f"🎵 Трек #{i}",
                            reply_markup=markup,
                        )
                    else:
                        # Если PiAPI не дал ссылку — покажем, что пришло (коротко)
                        keys = ", ".join(list(item.keys())[:15]) if isinstance(item, dict) else str(type(item))
                        await tg_send_message(chat_id, f"⚠️ Трек #{i}: PiAPI не вернул ссылку на MP3. Поля: {keys}", reply_markup=_main_menu_for(user_id) if i == 1 else None)

                    extra_lines = []
                    if video_url:
                        extra_lines.append(f"🎬 MP4: {video_url}")
                    if extra_lines:
                        await tg_send_message(chat_id, "\n".join(extra_lines), reply_markup=None)
                _clear_music_ctx()
            except Exception as e:
                await tg_send_message(chat_id, f"❌ Ошибка PiAPI (music): {e}\n\nГенерация остановлена. Нажмите «Музыка будущего», чтобы попробовать снова.")
                _clear_music_ctx()
            return {"ok": True}

        # из WebApp может прилетать примерно так: {"flow":"motion","mode":"pro"}
        flow = (payload.get("flow") or payload.get("gen_type") or payload.get("genType") or "").lower().strip()
        quality = (payload.get("mode") or payload.get("quality") or "std").lower().strip()

        # нормализация flow
        if flow in ("motion", "motion_control", "mc"):
            flow = "motion"
        elif flow in ("i2v", "image_to_video", "image2video", "image->video"):
            flow = "i2v"
        else:
            flow = "motion" if not flow else flow

        # нормализация quality
        quality = "pro" if quality in ("pro", "professional") else "std"

        # сохраняем настройки Kling в state
        st["kling_settings"] = {"flow": flow, "quality": quality}
        st["ts"] = _now()

        # после сохранения — запускаем нужный сценарий и выходим из апдейта
        if flow == "motion":
            _set_mode(chat_id, user_id, "kling_mc")
            st["kling_mc"] = {"step": "need_avatar", "avatar_bytes": None, "video_bytes": None}

            await tg_send_message(
                chat_id,
                f"✅ Настройки сохранены: Motion Control • {quality.upper()}\n\n"
                "Шаг 1) Пришли ФОТО аватара (кого анимируем).\n"
                "Шаг 2) Потом пришли ВИДЕО с движением (3–30 сек).\n"
                "Шаг 3) Потом текстом напиши, что должно происходить (или просто: Старт).",
                reply_markup=_help_menu_for(user_id),
            )
        else:
            # Image → Video
            # сохраняем выбранную длительность (5/10 сек) из WebApp, если она пришла
            try:
                duration = int(payload.get("duration") or payload.get("seconds") or payload.get("sec") or 5)
            except Exception:
                duration = 5
            if duration not in (5, 10):
                duration = 5

            st["kling_settings"]["duration"] = duration

            _set_mode(chat_id, user_id, "kling_i2v")
            st["kling_i2v"] = {"step": "need_image", "image_bytes": None, "duration": duration}
            st["ts"] = _now()

            await tg_send_message(
                chat_id,
                f"✅ Настройки сохранены: Image → Video • {quality.upper()} • {duration} сек\n\n"
                "Шаг 1) Пришли СТАРТОВОЕ ФОТО.\n"
                "Шаг 2) Потом текстом опиши, что должно происходить (или просто: Старт).",
                reply_markup=_help_menu_for(user_id),
            )

        return {"ok": True}
    # ----- Admin stats -----
    if incoming_text == "📊 Статистика":
        if not _is_admin(user_id):
            await tg_send_message(
                chat_id,
                "Нет доступа.",
                reply_markup=_help_menu_for(user_id),
            )
            return {"ok": True}

        stats = get_basic_stats()
        if not stats.get("ok"):
            await tg_send_message(
                chat_id,
                f"Supabase недоступен: {stats.get('error','')}",
                reply_markup=_help_menu_for(user_id),
            )
            return {"ok": True}

        lines = [
            "📊 Статистика бота",
            f"👤 Всего пользователей: {stats['total_users']}",
            f"✅ DAU сегодня: {stats['dau_today']}",
            f"✅ DAU вчера: {stats['dau_yesterday']}",
            "",
            "📅 DAU (последние 7 дней):",
        ]

        last7 = stats.get("last7") or {}
        if not last7:
            lines.append("— пока данных нет —")
        else:
            for day, cnt in last7.items():
                lines.append(f"{day}: {cnt}")

        await tg_send_message(
            chat_id,
            "\n".join(lines),
            reply_markup=_help_menu_for(user_id),
        )
        return {"ok": True}

    # ----- Video future (Kling Motion Control) -----
    if incoming_text == "🎬 Видео будущего":

        _set_mode(chat_id, user_id, "kling_mc")
        st["kling_mc"] = {
            "step": "need_avatar",
            "avatar_bytes": None,
            "video_bytes": None,
        }
        st["ts"] = _now()

        await tg_send_message(
            chat_id,
            "🎬 Видео будущего → Motion Control\n\n"
            "Шаг 1) Пришли ФОТО аватара (кого анимируем).\n"
            "Шаг 2) Потом пришли ВИДЕО с движением (3–30 сек).\n"
            "Шаг 3) Потом текстом напиши, что должно происходить (или просто: Старт).",
            reply_markup=_help_menu_for(user_id),
        )
        return {"ok": True}


    # /start

    # /start
    if incoming_text.startswith("/start"):
        _set_mode(chat_id, user_id, "chat")
        await tg_send_message(
            chat_id,
            "Привет!\n"
            "Режимы:\n"
            "• «ИИ (чат)» — вопросы/анализ фото/решение задач.\n"
            "• «Фото будущего» — фото-режимы (Афиши / Нейро фотосессии / 2 фото).\n",
            reply_markup=_help_menu_for(user_id),
        )
        return {"ok": True}


    # /reset — сбросить текущий режим/зависшие состояния (крайняя мера)
    if incoming_text.startswith("/reset") or incoming_text.startswith("/resetgen"):
        # чистим in-memory state
        st.clear()
        st.update({"mode": "chat", "ts": _now(), "poster": {}, "dl": {}})
        # снимаем busy-lock (если зависла генерация)
        _busy_end(int(user_id))
        # чистим Supabase FSM (например music_wait_text)
        try:
            sb_clear_user_state(user_id)
        except Exception:
            pass
        await tg_send_message(chat_id, "✅ Сброс выполнен. Возвращаю в главное меню.", reply_markup=_main_menu_for(user_id))
        return {"ok": True}


    if incoming_text in ("⬅ Назад", "Назад"):
        # Возврат в главное меню из любого режима
        _set_mode(chat_id, user_id, "chat")
        await tg_send_message(chat_id, "Главное меню.", reply_markup=_main_menu_for(user_id))
        return {"ok": True}


    
    # ---- SUNO Music: ждём текст (описание или лирику) ----
    if st.get("mode") == "suno_music" and incoming_text:
        settings = st.get("music_settings") or {
            "mv": "chirp-crow",
            "music_mode": "prompt",
            "title": "",
            "tags": "",
            "make_instrumental": False,
            "service_mode": "public",
            "language": "",
        }

                # "Старт" должен быть триггером, а не текстом песни.
        _lc = (incoming_text or "").strip().lower()
        if _lc in ("старт", "start", "go", "запуск"):
            _existing = (settings.get("prompt") or "").strip() if (settings.get("music_mode") or "").lower().strip() == "custom" else (settings.get("gpt_description_prompt") or "").strip()
            if not _existing:
                await tg_send_message(
                    chat_id,
                    "❗️Сначала задай идею/текст в «Музыка будущего» (WebApp), затем напиши «Старт».",
                    reply_markup=_help_menu_for(user_id),
                )
                return {"ok": True}
            incoming_text = _existing

# принимаем текст и кладём в нужное поле
        if (settings.get("music_mode") or "").lower().strip() == "custom":
            settings["prompt"] = incoming_text
        else:
            settings["gpt_description_prompt"] = incoming_text

        st["music_settings"] = settings
        st["ts"] = _now()
        # текст получен — сбрасываем ожидание в Supabase
        sb_clear_user_state(user_id)

        input_block = {
            "mv": settings.get("mv") or "chirp-crow",
            "title": settings.get("title") or "",
            "tags": settings.get("tags") or "",
            "make_instrumental": bool(settings.get("make_instrumental")),
        }
        if (settings.get("music_mode") or "").lower().strip() == "custom":
            input_block["prompt"] = settings.get("prompt") or ""
        else:
            input_block["gpt_description_prompt"] = settings.get("gpt_description_prompt") or ""

        payload_api = {
            "model": "suno",
            "task_type": "music",
            "input": input_block,
            "config": {"service_mode": settings.get("service_mode") or "public"},
        }

        await tg_send_message(chat_id, "⏳ Запускаю генерацию музыки…")
        try:
            provider = str(settings.get("provider") or settings.get("api") or settings.get("ai_provider") or settings.get("aiProvider") or "").lower().strip()
            if not provider:
                provider = os.getenv("MUSIC_PROVIDER_DEFAULT", "piapi").lower().strip()

            async def _run_piapi():
                created_local = await piapi_create_task(payload_api)
                task_id_local = ((created_local.get("data") or {}).get("task_id")) or ""
                if not task_id_local:
                    raise RuntimeError(f"PiAPI did not return task_id: {created_local}")
                done_local = await piapi_poll_task(task_id_local, timeout_sec=300, sleep_sec=2.0)
                return ("piapi", done_local)

            async def _run_sunoapi():
                prompt_text = (input_block.get("gpt_description_prompt") or input_block.get("prompt") or "").strip()
                if not prompt_text:
                    prompt_text = "A modern catchy song with clear structure and strong hook"
                mv_local = str(input_block.get("mv") or "").lower().strip()
                model_enum = "V4_5ALL"
                if "v5" in mv_local:
                    model_enum = "V5"
                elif "v4" in mv_local and "v4_5" not in mv_local:
                    model_enum = "V4"
                custom_mode = bool((settings.get("music_mode") or "").lower().strip() == "custom")
                instrumental = bool(input_block.get("make_instrumental"))
                title_local = (input_block.get("title") or "").strip()
                style_local = (input_block.get("tags") or "").strip()
                task_id_local = await sunoapi_generate_task(
                    prompt=prompt_text,
                    custom_mode=custom_mode,
                    instrumental=instrumental,
                    model=model_enum,
                    user_id=user_id,
                    chat_id=chat_id,
                    title=title_local,
                    style=style_local,
                )
                done_local = await sunoapi_poll_task(task_id_local, timeout_sec=SUNOAPI_POLL_TIMEOUT_SEC, sleep_sec=2.0)
                return ("sunoapi", done_local)

            primary = provider if provider in ("piapi", "sunoapi") else "piapi"
            secondary = "sunoapi" if primary == "piapi" else "piapi"

            try:
                if primary == "sunoapi":
                    source, done = await _run_sunoapi()
                else:
                    source, done = await _run_piapi()
            except Exception as e_primary:
                can_fallback = (secondary == "sunoapi" and bool(SUNOAPI_API_KEY)) or (secondary == "piapi" and bool(PIAPI_API_KEY))
                if not can_fallback:
                    raise
                await tg_send_message(chat_id, f"⚠️ Основной провайдер ({primary}) упал: {e_primary}\nПробую запасной ({secondary})…")
                if secondary == "sunoapi":
                    source, done = await _run_sunoapi()
                else:
                    source, done = await _run_piapi()

            if source == "sunoapi":
                data = done.get("data") or {}
                status = str(data.get("status") or "").upper().strip()
                if status not in ("SUCCESS",):
                    await tg_send_message(chat_id, f"❌ Музыка не сгенерировалась (SunoAPI): {status}\n{done.get('msg') or 'unknown error'}")
                    return {"ok": True}
                out = _sunoapi_extract_tracks(done)
                if not out:
                    await tg_send_message(chat_id, "✅ Готово, но SunoAPI не вернул треки. Проверь task в кабинете.")
                    return {"ok": True}
            else:
                data = done.get("data") or {}
                status = (data.get("status") or "")
                if str(status).lower() != "completed":
                    err = (data.get("error") or {}).get("message") or "unknown error"
                    await tg_send_message(chat_id, f"❌ Музыка не сгенерировалась: {status}\n{err}")
                    return {"ok": True}

                out = data.get("output") or []
                if isinstance(out, dict):
                    out = [out]
                if not out:
                    await tg_send_message(chat_id, "✅ Готово, но PiAPI не вернул output. Проверь task в кабинете.")
                    return {"ok": True}

            lines = ["✅ Музыка готова:"]
            for i, item in enumerate(out[:2], start=1):
                audio_url = item.get("audio_url") or ""
                video_url = item.get("video_url") or ""
                image_url = item.get("image_url") or ""
                lines.append(f"#{i}")
                if audio_url:
                    lines.append(f"🎧 MP3: {audio_url}")
                if video_url:
                    lines.append(f"🎬 MP4: {video_url}")
            await tg_send_message(chat_id, "\n".join(lines), reply_markup=_main_menu_for(user_id))
            _clear_music_ctx()
        except Exception as e:
            await tg_send_message(chat_id, f"❌ Ошибка PiAPI/Suno: {e}", reply_markup=_main_menu_for(user_id))
        return {"ok": True}
    if incoming_text in ("💰 Баланс", "Баланс", "💰Баланс"):
        try:
            ensure_user_row(user_id)
            bal = int(get_balance(user_id) or 0)
        except Exception as e:
            await tg_send_message(chat_id, f"Не смог получить баланс: {e}", reply_markup=_main_menu_for(user_id))
            return {"ok": True}

        await tg_send_message(
            chat_id,
            f"💰 Баланс: {bal} токенов\n\nРасход токенов зависит от режима генерации (выбирается в WebApp).",
            reply_markup=_topup_balance_inline_kb(),
        )
        return {"ok": True}

    if incoming_text in ("Фото будущего", "📸 Фото будущего"):
        # Подменю: объединённая точка входа в фото-режимы
        await tg_send_message(
            chat_id,
            "📸 Фото будущего — выбери режим:",
            reply_markup=_photo_future_menu_keyboard(),
        )
        return {"ok": True}

    if incoming_text in ("ИИ (чат)", "🧠 ИИ (чат)", "🧠 ИИ чат"):
        _set_mode(chat_id, user_id, "chat")
        await tg_send_message(chat_id, "Ок. Режим «ИИ (чат)».", reply_markup=_main_menu_for(user_id))
        return {"ok": True}


    if incoming_text == "Нейро фотосессии":
        _set_mode(chat_id, user_id, "photosession")
        await tg_send_message(
            chat_id,
            "Режим «Нейро фотосессии».\n"
            "1) Пришли фото.\n"
            "2) Потом одним сообщением напиши задачу: локация/стиль/одежда/детали.\n"
            "Я постараюсь сохранить человека максимально 1к1 и сделать фото как профессиональную фотосессию.",
            reply_markup=_help_menu_for(user_id),
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


        # ---- VEO Text→Video: ждём промпт ----
    if st.get("mode") == "veo_t2v" and incoming_text:
        # Если пользователь нажал кнопку меню/навигации — НЕ считаем это промптом
        if _is_nav_or_menu_text(incoming_text):
            _set_mode(chat_id, user_id, "chat")
            st.pop("veo_t2v", None)
            st.pop("veo_settings", None)
            st["ts"] = _now()
            sb_clear_user_state(user_id)
            await tg_send_message(chat_id, "Ок. Вышел из Veo. Главное меню.", reply_markup=_main_menu_for(user_id))
            return {"ok": True}

        # Блокируем параллельные запуски (двойные списания), пока Veo ещё считается/отправляется
        if _busy_is_active(int(user_id)):
            kind = _busy_kind(int(user_id)) or "генерация"
            await tg_send_message(
                chat_id,
                f"⏳ Сейчас выполняется: {kind}. Дождись завершения (или /reset).",
                reply_markup=_help_menu_for(user_id),
            )
            return {"ok": True}

        settings = st.get("veo_settings") or {}
        veo_model = (settings.get("veo_model") or "fast")
        model_slug = "veo-3.1" if veo_model == "pro" else "veo-3-fast"

        duration = int(settings.get("duration") or 8)
        resolution = str(settings.get("resolution") or ("1080p" if veo_model == "pro" else "720p"))
        aspect_ratio = str(settings.get("aspect_ratio") or "16:9")
        generate_audio = bool(settings.get("generate_audio"))

        # ---- VEO BILLING (Text→Video) ----
        _busy_start(int(user_id), "Veo видео")
        try:
            # Баланс + списание
            try:
                ensure_user_row(user_id)
                bal = int(get_balance(user_id) or 0)
            except Exception:
                bal = 0

            ch = calc_veo_charge(
                veo_model=veo_model,
                model_slug=model_slug,
                generate_audio=generate_audio,
                duration_sec=duration,
            )

            if bal < ch.total_tokens:
                await tg_send_message(
                    chat_id,
                    f"❌ Недостаточно токенов.\nНужно: {ch.total_tokens}\nБаланс: {bal}\n\n{format_veo_charge_line(ch)}",
                    reply_markup=_topup_balance_inline_kb(),
                )
                return {"ok": True}

            add_tokens(
                user_id,
                -ch.total_tokens,
                reason="veo_video",
                meta={
                    "tier": ch.tier,
                    "generate_audio": ch.generate_audio,
                    "duration": ch.duration_sec,
                    "tokens_per_sec": ch.tokens_per_sec,
                    "total_tokens": ch.total_tokens,
                    "flow": "t2v",
                },
            )

            info = (
                f"⏳ Генерирую видео (Veo {'3.1' if veo_model == 'pro' else 'Fast'} | "
                f"{resolution} | {duration}s | {aspect_ratio} | звук: {'да' if generate_audio else 'нет'})"
            )
            await tg_send_message(chat_id, info, reply_markup=_help_menu_for(user_id))

            try:
                video_url = await run_veo_text_to_video(
                    user_id=int(user_id),
                    model=model_slug,
                    prompt=incoming_text,
                    duration=duration,
                    resolution=resolution,
                    aspect_ratio=aspect_ratio,
                    generate_audio=generate_audio,
                    negative_prompt=None,
                    reference_images_bytes=None,
                )
            except Exception as e:
                await tg_send_message(chat_id, f"❌ Ошибка Veo: {e}", reply_markup=_help_menu_for(user_id))
                return {"ok": True}

            try:
                await tg_send_video_url(chat_id, video_url, caption="✅ Готово! (Veo)")
            except Exception:
                await tg_send_message(chat_id, f"✅ Готово! Видео: {video_url}", reply_markup=_help_menu_for(user_id))

        finally:
            _busy_end(int(user_id))

        _set_mode(chat_id, user_id, "chat")
        st.pop("veo_t2v", None)
        st.pop("veo_settings", None)
        st["ts"] = _now()
        sb_clear_user_state(user_id)
        await tg_send_message(chat_id, "Главное меню.", reply_markup=_main_menu_for(user_id))
        return {"ok": True}

       # ---- VEO Image→Video: если мы в шаге референсов, можно написать 'Готово' ----
    if st.get("mode") == "veo_i2v" and incoming_text:
        # Если пользователь нажал кнопку меню/навигации — НЕ считаем это промптом/командой для Veo
        if _is_nav_or_menu_text(incoming_text):
            _set_mode(chat_id, user_id, "chat")
            st.pop("veo_i2v", None)
            st.pop("veo_settings", None)
            st["ts"] = _now()
            sb_clear_user_state(user_id)
            await tg_send_message(chat_id, "Ок. Вышел из Veo. Главное меню.", reply_markup=_main_menu_for(user_id))
            return {"ok": True}

        # Блокируем параллельные запуски (двойные списания)
        if _busy_is_active(int(user_id)):
            kind = _busy_kind(int(user_id)) or "генерация"
            await tg_send_message(
                chat_id,
                f"⏳ Сейчас выполняется: {kind}. Дождись завершения (или /reset).",
                reply_markup=_help_menu_for(user_id),
            )
            return {"ok": True}

        vi = st.get("veo_i2v") or {}
        step = (vi.get("step") or "need_image")

        if step == "need_refs" and incoming_text.strip().lower() in ("готово", "done", "старт", "start"):
            vi["step"] = "need_prompt"
            st["veo_i2v"] = vi
            st["ts"] = _now()
            await tg_send_message(chat_id, "Ок ✅ Теперь пришли ТЕКСТ (промпт) для видео.", reply_markup=_help_menu_for(user_id))
            return {"ok": True}

        if step == "need_prompt":
            settings = st.get("veo_settings") or {}
            veo_model = (settings.get("veo_model") or "fast")
            model_slug = "veo-3.1" if veo_model == "pro" else "veo-3-fast"

            duration = int(settings.get("duration") or 8)
            resolution = str(settings.get("resolution") or ("1080p" if veo_model == "pro" else "720p"))
            aspect_ratio = str(settings.get("aspect_ratio") or "16:9")
            generate_audio = bool(settings.get("generate_audio"))

            image_bytes = vi.get("image_bytes")
            if not image_bytes:
                await tg_send_message(chat_id, "Сначала пришли стартовое фото.", reply_markup=_help_menu_for(user_id))
                return {"ok": True}

            last_frame_bytes = vi.get("last_frame_bytes")
            ref_bytes = vi.get("reference_images_bytes") or []
            if not isinstance(ref_bytes, list):
                ref_bytes = []

            # ---- VEO BILLING (Image→Video) ----
            _busy_start(int(user_id), "Veo видео")
            try:
                # Баланс + списание
                try:
                    ensure_user_row(user_id)
                    bal = int(get_balance(user_id) or 0)
                except Exception:
                    bal = 0

                ch = calc_veo_charge(
                    veo_model=veo_model,
                    model_slug=model_slug,
                    generate_audio=generate_audio,
                    duration_sec=duration,
                )

                if bal < ch.total_tokens:
                    await tg_send_message(
                        chat_id,
                        f"❌ Недостаточно токенов.\nНужно: {ch.total_tokens}\nБаланс: {bal}\n\n{format_veo_charge_line(ch)}",
                        reply_markup=_topup_balance_inline_kb(),
                    )
                    return {"ok": True}

                add_tokens(
                    user_id,
                    -ch.total_tokens,
                    reason="veo_video",
                    meta={
                        "tier": ch.tier,
                        "generate_audio": ch.generate_audio,
                        "duration": ch.duration_sec,
                        "tokens_per_sec": ch.tokens_per_sec,
                        "total_tokens": ch.total_tokens,
                        "flow": "i2v",
                    },
                )

                info = (
                    f"⏳ Генерирую видео (Veo {'3.1' if veo_model == 'pro' else 'Fast'} | "
                    f"{resolution} | {duration}s | {aspect_ratio} | звук: {'да' if generate_audio else 'нет'})"
                )
                await tg_send_message(chat_id, info, reply_markup=_help_menu_for(user_id))

                try:
                    video_url = await run_veo_image_to_video(
                        user_id=int(user_id),
                        model=model_slug,
                        image_bytes=image_bytes,
                        prompt=incoming_text,
                        duration=duration,
                        resolution=resolution,
                        aspect_ratio=aspect_ratio,
                        generate_audio=generate_audio,
                        negative_prompt=None,
                        reference_images_bytes=ref_bytes if ref_bytes else None,
                        last_frame_bytes=last_frame_bytes if last_frame_bytes else None,
                    )
                except Exception as e:
                    await tg_send_message(chat_id, f"❌ Ошибка Veo: {e}", reply_markup=_help_menu_for(user_id))
                    return {"ok": True}

                try:
                    await tg_send_video_url(chat_id, video_url, caption="✅ Готово! (Veo)")
                except Exception:
                    await tg_send_message(chat_id, f"✅ Готово! Видео: {video_url}", reply_markup=_help_menu_for(user_id))

            finally:
                _busy_end(int(user_id))

            _set_mode(chat_id, user_id, "chat")
            st.pop("veo_i2v", None)
            st.pop("veo_settings", None)
            st["ts"] = _now()
            sb_clear_user_state(user_id)
            await tg_send_message(chat_id, "Главное меню.", reply_markup=_main_menu_for(user_id))
            return {"ok": True}



    if incoming_text in ("🍌 Nano Banana", "Nano Banana"):
        _set_mode(chat_id, user_id, "nano_banana")
        await tg_send_message(
            chat_id,
            "🍌 Nano Banana — редактирование фото (платно).\n\n"
            "1) Пришли фото.\n"
            "2) Потом одним сообщением напиши что изменить (стиль/фон/детали).\n\n"
            "Стоимость: 1 токен за результат.",
            reply_markup=_photo_future_menu_keyboard(),
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
            reply_markup=_help_menu_for(user_id),
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
            reply_markup=_help_menu_for(user_id),
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
            "• 🔄 Сбросить генерацию — если музыка зациклилась/зависла\n• /reset — сбросить текущий режим\n",
            reply_markup=_help_menu_for(user_id),
        )
        return {"ok": True}

    # ---------------- Фото (photo) ----------------
    photos = message.get("photo") or []
    if photos:
        largest = photos[-1]
        file_id = largest.get("file_id")
        if not file_id:
            await tg_send_message(chat_id, "Не смог прочитать file_id. Отправь фото ещё раз.", reply_markup=_main_menu_for(user_id))
            return {"ok": True}

        try:
            file_path = await tg_get_file_path(file_id)
            img_bytes = await tg_download_file_bytes(file_path)
        except Exception as e:
            await tg_send_message(chat_id, f"Ошибка при загрузке фото: {e}", reply_markup=_main_menu_for(user_id))
            return {"ok": True}



        
        
        
        
        # ---- NANO BANANA: ждём фото ----
        if st.get("mode") == "nano_banana":
            nb = st.get("nano_banana") or {}
            step = (nb.get("step") or "need_photo")
            if step == "need_photo":
                nb["photo_bytes"] = img_bytes
                nb["step"] = "need_prompt"
                st["nano_banana"] = nb
                st["ts"] = _now()
                await tg_send_message(
                    chat_id,
                    "Фото принял ✅\nТеперь напиши одним сообщением, что изменить (фон/стиль/детали).\n\nСтоимость: 1 токен.",
                    reply_markup=_photo_future_menu_keyboard(),
                )
                return {"ok": True}

# ---- KLING Image → Video: step=need_image ----
        if st.get("mode") == "kling_i2v":
            ki = st.get("kling_i2v") or {}
            step = (ki.get("step") or "need_image")

            if step == "need_image":
                ki["image_bytes"] = img_bytes
                ki["step"] = "need_prompt"
                st["kling_i2v"] = ki
                st["ts"] = _now()

                ks = st.get("kling_settings") or {}
                quality = (ks.get("quality") or "std").lower()
                duration = int((ks.get("duration") or ki.get("duration") or 5))
                await tg_send_message(
                    chat_id,
                    f"Фото получил ✅\nТеперь напиши текстом, что должно происходить ({quality.upper()}, {duration} сек)\n"
                    "Пример: «Камера плавно приближается, лёгкое движение волос, реализм».\n"
                    "Можно просто: Старт",
                    reply_markup=_help_menu_for(user_id),
                )
                return {"ok": True}

            await tg_send_message(
                chat_id,
                "Фото уже есть ✅ Теперь жду ТЕКСТ (или /start чтобы выйти).",
                reply_markup=_help_menu_for(user_id),
            )
            return {"ok": True}



        # ---- VEO Image → Video: шаги need_image / need_last_frame / need_refs ----
        if st.get("mode") == "veo_i2v":
            vi = st.get("veo_i2v") or {}
            step = (vi.get("step") or "need_image")

            settings = st.get("veo_settings") or {}
            use_last_frame = bool(settings.get("use_last_frame"))
            use_reference_images = bool(settings.get("use_reference_images"))

            # 1) Стартовое фото
            if step == "need_image":
                vi["image_bytes"] = img_bytes

                # Далее: last_frame -> refs -> prompt
                if use_last_frame:
                    vi["step"] = "need_last_frame"
                    st["veo_i2v"] = vi
                    st["ts"] = _now()
                    await tg_send_message(
                        chat_id,
                        "Стартовое фото получил ✅\nТеперь пришли ФИНАЛЬНЫЙ кадр (last frame) — ещё одно фото.\n"
                        "Если last frame не нужен — нажми /reset и выключи опцию в WebApp.",
                        reply_markup=_help_menu_for(user_id),
                    )
                    return {"ok": True}

                if use_reference_images:
                    vi["step"] = "need_refs"
                    st["veo_i2v"] = vi
                    st["ts"] = _now()
                    await tg_send_message(
                        chat_id,
                        "Стартовое фото получил ✅\nТеперь пришли референсы (до 4 фото).\n"
                        "Когда закончишь — напиши «Готово».",
                        reply_markup=_help_menu_for(user_id),
                    )
                    return {"ok": True}

                vi["step"] = "need_prompt"
                st["veo_i2v"] = vi
                st["ts"] = _now()
                await tg_send_message(
                    chat_id,
                    "Фото получил ✅ Теперь напиши ТЕКСТОМ, что должно происходить в видео.\n"
                    "Пример: «Камера плавно приближается, лёгкое движение волос, реализм».",
                    reply_markup=_help_menu_for(user_id),
                )
                return {"ok": True}

            # 2) Last frame (финальный кадр)
            if step == "need_last_frame":
                vi["last_frame_bytes"] = img_bytes

                if use_reference_images:
                    vi["step"] = "need_refs"
                    st["veo_i2v"] = vi
                    st["ts"] = _now()
                    await tg_send_message(
                        chat_id,
                        "Last frame получил ✅\nТеперь пришли референсы (до 4 фото).\n"
                        "Когда закончишь — напиши «Готово».",
                        reply_markup=_help_menu_for(user_id),
                    )
                    return {"ok": True}

                vi["step"] = "need_prompt"
                st["veo_i2v"] = vi
                st["ts"] = _now()
                await tg_send_message(
                    chat_id,
                    "Last frame получил ✅\nТеперь пришли ТЕКСТ (промпт) для видео.",
                    reply_markup=_help_menu_for(user_id),
                )
                return {"ok": True}

            # 3) Reference images (до 4)
            if step == "need_refs":
                refs = vi.get("reference_images_bytes") or []
                if not isinstance(refs, list):
                    refs = []

                if len(refs) >= 4:
                    await tg_send_message(
                        chat_id,
                        "Референсов уже 4/4 ✅\nНапиши «Готово», чтобы перейти к промпту.",
                        reply_markup=_help_menu_for(user_id),
                    )
                    return {"ok": True}

                refs.append(img_bytes)
                vi["reference_images_bytes"] = refs
                vi["step"] = "need_refs"
                st["veo_i2v"] = vi
                st["ts"] = _now()
                await tg_send_message(
                    chat_id,
                    f"Референс принят ✅ ({len(refs)}/4)\n"
                    "Пришли ещё референс или напиши «Готово».",
                    reply_markup=_help_menu_for(user_id),
                )
                return {"ok": True}

            # если прислал фото в неправильном шаге
            await tg_send_message(
                chat_id,
                "Фото уже есть ✅ Сейчас жду ТЕКСТ (или «Готово» в шаге референсов).",
                reply_markup=_help_menu_for(user_id),
            )
            return {"ok": True}

            await tg_send_message(
                chat_id,
                "Фото уже есть ✅ Теперь жду ТЕКСТ.",
                reply_markup=_help_menu_for(user_id),
            )
            return {"ok": True}


# ---- KLING Motion Control: step=need_avatar ----
        if st.get("mode") == "kling_mc":
            km = st.get("kling_mc") or {}
            step = (km.get("step") or "need_avatar")

            if step == "need_avatar":
                km["avatar_bytes"] = img_bytes
                km["step"] = "need_video"
                st["kling_mc"] = km
                st["ts"] = _now()

                await tg_send_message(
                    chat_id,
                    "Фото аватара получил ✅\nТеперь пришли ВИДЕО с движением (3–10 сек).",
                    reply_markup=_help_menu_for(user_id),
                )
                return {"ok": True}

            await tg_send_message(
                chat_id,
                "Аватар уже есть ✅ Теперь жду ВИДЕО с движением (или /start чтобы выйти).",
                reply_markup=_help_menu_for(user_id),
            )
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
                    reply_markup=_help_menu_for(user_id),
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
                    reply_markup=_help_menu_for(user_id),
                )
                return {"ok": True}

            if step == "need_prompt":
                await tg_send_message(
                    chat_id,
                    "Я уже получил 2 фото. Теперь пришли ТЕКСТОМ, что нужно сделать (или /reset).",
                    reply_markup=_help_menu_for(user_id),
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
                reply_markup=_help_menu_for(user_id),
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
            if st.get("mode") == "chat":
                _ai_hist_add(st, "user", prompt)
                _ai_hist_add(st, "assistant", answer)
            await tg_send_message(chat_id, answer, reply_markup=_main_menu_for(user_id))
            return {"ok": True}
            await tg_send_message(chat_id, "Фото получил. Анализирую...", reply_markup=_main_menu_for(user_id))
        prompt = incoming_text if incoming_text else VISION_DEFAULT_USER_PROMPT
        answer = await openai_chat_answer(
            user_text=prompt,
            system_prompt=VISION_GENERAL_SYSTEM_PROMPT,
            image_bytes=img_bytes,
            temperature=0.4,
            max_tokens=700,
        )
        if st.get("mode") == "chat":
            _ai_hist_add(st, "user", prompt)
            _ai_hist_add(st, "assistant", answer)
        await tg_send_message(chat_id, answer, reply_markup=_main_menu_for(user_id))
        return {"ok": True}

    
    # ---------------- Video (message.video) ----------------
    vid = message.get("video") or {}
    if vid:
        if st.get("mode") == "kling_mc":
            km = st.get("kling_mc") or {}
            step = (km.get("step") or "need_avatar")

            if step != "need_video":
                await tg_send_message(chat_id, "Сначала пришли ФОТО аватара.", reply_markup=_main_menu_for(user_id))
                return {"ok": True}

            file_id = vid.get("file_id")
            if not file_id:
                await tg_send_message(chat_id, "Не смог прочитать file_id видео. Пришли видео ещё раз.", reply_markup=_main_menu_for(user_id))
                return {"ok": True}

            try:
                file_path = await tg_get_file_path(file_id)
                video_bytes = await tg_download_file_bytes(file_path)
            except Exception as e:
                await tg_send_message(chat_id, f"Ошибка при загрузке видео: {e}", reply_markup=_main_menu_for(user_id))
                return {"ok": True}

            km["video_bytes"] = video_bytes
            km["step"] = "need_prompt"
            st["kling_mc"] = km
            st["ts"] = _now()

            await tg_send_message(
                chat_id,
                "Видео получил ✅\nТеперь напиши текстом, что должно происходить (или просто: Старт).",
                reply_markup=_help_menu_for(user_id),
            )
            return {"ok": True}


    # ---------------- Фото (document image/*) ----------------
    doc = message.get("document") or {}
    if doc:
        mime = (doc.get("mime_type") or "").lower()
        file_id = doc.get("file_id")

        # ---- KLING Motion Control: accept video as document ----
        if file_id and mime.startswith("video/") and st.get("mode") == "kling_mc":
            km = st.get("kling_mc") or {}
            step = (km.get("step") or "need_avatar")

            if step != "need_video":
                await tg_send_message(chat_id, "Сначала пришли ФОТО аватара.", reply_markup=_main_menu_for(user_id))
                return {"ok": True}

            try:
                file_path = await tg_get_file_path(file_id)
                video_bytes = await tg_download_file_bytes(file_path)
            except Exception as e:
                await tg_send_message(chat_id, f"Ошибка при загрузке видео: {e}", reply_markup=_main_menu_for(user_id))
                return {"ok": True}

            km["video_bytes"] = video_bytes
            km["step"] = "need_prompt"
            st["kling_mc"] = km
            st["ts"] = _now()

            await tg_send_message(
                chat_id,
                "Видео получил ✅\nТеперь напиши текстом, что должно происходить (или просто: Старт).",
                reply_markup=_help_menu_for(user_id),
            )
            return {"ok": True}
        if file_id and mime.startswith("image/"):
            try:
                file_path = await tg_get_file_path(file_id)
                img_bytes = await tg_download_file_bytes(file_path)
            except Exception as e:
                await tg_send_message(chat_id, f"Ошибка при загрузке фото: {e}", reply_markup=_main_menu_for(user_id))
                return {"ok": True}


            
        # ---- NANO BANANA: ждём фото ----
        if st.get("mode") == "nano_banana":
            nb = st.get("nano_banana") or {}
            step = (nb.get("step") or "need_photo")
            if step == "need_photo":
                nb["photo_bytes"] = img_bytes
                nb["step"] = "need_prompt"
                st["nano_banana"] = nb
                st["ts"] = _now()
                await tg_send_message(
                    chat_id,
                    "Фото принял ✅\nТеперь напиши одним сообщением, что изменить (фон/стиль/детали).\n\nСтоимость: 1 токен.",
                    reply_markup=_photo_future_menu_keyboard(),
                )
                return {"ok": True}

# ---- KLING Image → Video: accept start image as document ----
            if st.get("mode") == "kling_i2v":
                ki = st.get("kling_i2v") or {}
                step = (ki.get("step") or "need_image")

                if step == "need_image":
                    ki["image_bytes"] = img_bytes
                    ki["step"] = "need_prompt"
                    st["kling_i2v"] = ki
                    st["ts"] = _now()

                    ks = st.get("kling_settings") or {}
                    quality = (ks.get("quality") or "std").lower()
                    duration = int((ks.get("duration") or ki.get("duration") or 5))
                    await tg_send_message(
                        chat_id,
                        f"Фото получил ✅\nТеперь напиши текстом, что должно происходить ({quality.upper()}, {duration} сек)\n"
                        "Можно просто: Старт",
                        reply_markup=_help_menu_for(user_id),
                    )
                    return {"ok": True}

                await tg_send_message(chat_id, "Фото уже есть ✅ Теперь жду ТЕКСТ.", reply_markup=_main_menu_for(user_id))
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
                    await tg_send_message(chat_id, "Фото 1 получил. Теперь пришли Фото 2.", reply_markup=_main_menu_for(user_id))
                    return {"ok": True}

                if step == "need_photo_2":
                    tp["photo2_bytes"] = img_bytes
                    tp["photo2_file_id"] = file_id
                    tp["step"] = "need_prompt"
                    st["two_photos"] = tp
                    st["ts"] = _now()
                    await tg_send_message(chat_id, "Фото 2 получил. Теперь напиши текстом, что сделать.", reply_markup=_main_menu_for(user_id))
                    return {"ok": True}

                if step == "need_prompt":
                    await tg_send_message(chat_id, "Я уже получил 2 фото. Пришли текстом задачу (или /reset).", reply_markup=_main_menu_for(user_id))
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
                    reply_markup=_help_menu_for(user_id),
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
                if st.get("mode") == "chat":
                    _ai_hist_add(st, "user", prompt)
                    _ai_hist_add(st, "assistant", answer)
                await tg_send_message(chat_id, answer, reply_markup=_main_menu_for(user_id))
                return {"ok": True}

            await tg_send_message(chat_id, "Фото получил. Анализирую...", reply_markup=_main_menu_for(user_id))
            prompt = incoming_text if incoming_text else VISION_DEFAULT_USER_PROMPT
            answer = await openai_chat_answer(
                user_text=prompt,
                system_prompt=VISION_GENERAL_SYSTEM_PROMPT,
                image_bytes=img_bytes,
                temperature=0.4,
                max_tokens=700,
            )
            if st.get("mode") == "chat":
                _ai_hist_add(st, "user", prompt)
                _ai_hist_add(st, "assistant", answer)
            await tg_send_message(chat_id, answer, reply_markup=_main_menu_for(user_id))
            return {"ok": True}

    # ---------------- Текст без фото ----------------
    if incoming_text:

        # ---- NANO BANANA: текст после фото ----
        if st.get("mode") == "nano_banana":
            # Важно: системные команды и кнопки навигации не считаем промптом
            nav_text = (incoming_text or "").strip()
            if nav_text in ("⬅ Назад", "Назад") or nav_text.startswith("/"):
                # обработается выше в общих обработчиках (/reset, /start, Назад)
                pass
            elif nav_text in ("Фото будущего", "📸 Фото будущего", "Фото/Афиши", "Нейро фотосессии", "2 фото", "🍌 Nano Banana", "Текст→Картинка", "🧠 ИИ (чат)", "ИИ (чат)", "🧠 ИИ чат"):
                # навигация по меню — тоже не промпт
                pass
            else:
                nb = st.get("nano_banana") or {}
                step = (nb.get("step") or "need_photo")

                if step != "need_prompt":
                    await tg_send_message(
                        chat_id,
                        "Сначала пришли ФОТО для Nano Banana.\nОткрой «Фото будущего» → «🍌 Nano Banana».",
                        reply_markup=_photo_future_menu_keyboard(),
                    )
                    return {"ok": True}

                src_bytes = nb.get("photo_bytes")
                if not src_bytes:
                    await tg_send_message(
                        chat_id,
                        "Не хватает фото. Открой «Фото будущего» → «🍌 Nano Banana» и пришли фото заново.",
                        reply_markup=_photo_future_menu_keyboard(),
                    )
                    return {"ok": True}

                user_prompt = nav_text
                if not user_prompt:
                    await tg_send_message(
                        chat_id,
                        "Напиши текстом, что изменить (фон/стиль/детали).",
                        reply_markup=_photo_future_menu_keyboard(),
                    )
                    return {"ok": True}

                # Биллинг: 1 генерация = 1 токен
                ensure_user_row(user_id)
                try:
                    bal = float(get_balance(user_id) or 0)
                except Exception:
                    bal = 0

                cost = 1.0  # при необходимости можно сделать float (0.5/1.5) при поддержке дробных балансов в БД
                if bal < cost:
                    await tg_send_message(
                        chat_id,
                        f"Недостаточно токенов 😕\nНужно: {cost} токен(а) для Nano Banana.",
                        reply_markup=_topup_packs_kb(),
                    )
                    return {"ok": True}

                # списываем токен ДО запроса
                try:
                    add_tokens(user_id, -cost, reason="nano_banana")
                except TypeError:
                    # если billing_db принимает только int
                    add_tokens(user_id, -int(cost), reason="nano_banana")

                # Placeholder + кнопка "Скачать оригинал"
                placeholder = _make_blur_placeholder(src_bytes)
                token = _dl_init_slot(chat_id, user_id)
                msg_id = await tg_send_photo_bytes_return_message_id(
                    chat_id,
                    placeholder,
                    caption="🍌 Nano Banana — генерирую…",
                    reply_markup=_dl_keyboard(token),
                )

                try:
                    _busy_start(int(user_id), "Nano Banana")
                    out_bytes, ext = await run_nano_banana(src_bytes, user_prompt, output_format="jpg")

                    # сохраняем оригинал для скачивания (отдадим как document без сжатия)
                    _dl_set_bytes(chat_id, user_id, token, out_bytes)

                    # пытаемся заменить placeholder на результат в том же сообщении
                    if msg_id is not None:
                        try:
                            await tg_edit_message_media_photo(
                                chat_id,
                                msg_id,
                                out_bytes,
                                caption="🍌 Nano Banana — готово",
                                reply_markup=_dl_keyboard(token),
                            )
                        except Exception:
                            # если edit не сработал — отправим отдельным фото с кнопкой
                            await tg_send_photo_bytes(
                                chat_id,
                                out_bytes,
                                caption="🍌 Nano Banana — готово",
                                reply_markup=_dl_keyboard(token),
                            )
                    else:
                        await tg_send_photo_bytes(
                            chat_id,
                            out_bytes,
                            caption="🍌 Nano Banana — готово",
                            reply_markup=_dl_keyboard(token),
                        )

                except Exception as e:
                    # возврат токена при ошибке
                    try:
                        try:
                            add_tokens(user_id, cost, reason="nano_banana_refund")
                        except TypeError:
                            add_tokens(user_id, int(cost), reason="nano_banana_refund")
                    except Exception:
                        pass
                    # НЕ сбрасываем фото: пользователь может просто поменять текст и повторить
                    await tg_send_message(
                        chat_id,
                        f"Ошибка Nano Banana: {e}",
                        reply_markup=_photo_future_menu_keyboard(),
                    )
                    _busy_end(int(user_id))
                    return {"ok": True}

                # reset state (после успеха)
                _busy_end(int(user_id))
                st["nano_banana"] = {"step": "need_photo", "photo_bytes": None}
                st["ts"] = _now()
                return {"ok": True}


        # TWO PHOTOS: после 2 фото — пользователь пишет инструкцию
        if st.get("mode") == "two_photos":
            tp = st.get("two_photos") or {}
            step = (tp.get("step") or "need_photo_1")
            if step != "need_prompt":
                await tg_send_message(chat_id, "В режиме «2 фото» сначала пришли 2 фото подряд.", reply_markup=_main_menu_for(user_id))
                return {"ok": True}

            photo1_file_id = tp.get("photo1_file_id")
            photo2_file_id = tp.get("photo2_file_id")
            if not photo1_file_id or not photo2_file_id:
                await tg_send_message(chat_id, "Не вижу оба фото. Пришли 2 фото заново (или /reset).", reply_markup=_main_menu_for(user_id))
                return {"ok": True}

            user_task = incoming_text.strip()
            if not user_task:
                await tg_send_message(chat_id, "Напиши текстом, что сделать из этих 2 фото.", reply_markup=_main_menu_for(user_id))
                return {"ok": True}

            await tg_send_message(chat_id, "Делаю генерацию по 2 фото…", reply_markup=_main_menu_for(user_id))
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

                _busy_start(int(user_id), "2 фото")

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
                    reply_markup=_help_menu_for(user_id),
                )
            finally:
                _busy_end(int(user_id))
                # Сбрасываем режим, чтобы можно было сразу начать заново
                _set_mode(chat_id, user_id, "two_photos")
                st["ts"] = _now()

            return {"ok": True}

        
        # ---- KLING Motion Control: step=need_prompt ----
        
        # ---- KLING Image → Video: запуск по тексту ----
        if st.get("mode") == "kling_i2v":
            ki = st.get("kling_i2v") or {}
            step = (ki.get("step") or "need_image")

            if step != "need_prompt":
                await tg_send_message(chat_id, "Сначала пришли СТАРТОВОЕ ФОТО.", reply_markup=_main_menu_for(user_id))
                return {"ok": True}

            start_image_bytes = ki.get("image_bytes")
            if not start_image_bytes:
                await tg_send_message(chat_id, "Не хватает фото. Нажми «🎬 Видео будущего» и начни заново.", reply_markup=_main_menu_for(user_id))
                return {"ok": True}

            user_prompt = incoming_text.strip()
            if user_prompt.lower() in ("старт", "start", "go"):
                user_prompt = "Cinematic realistic video, subtle natural motion, high quality."

            ks = st.get("kling_settings") or {}
            quality = (ks.get("quality") or "std").lower()
            duration = int((ks.get("duration") or ki.get("duration") or 5))
            kling_mode = "pro" if quality in ("pro", "professional") else "std"

            await tg_send_message(chat_id, f"🎬 Генерирую видео ({duration} сек, {kling_mode.upper()})…", reply_markup=_main_menu_for(user_id))

            _busy_start(int(user_id), "Kling I2V")

            try:
                out_url = await run_image_to_video_from_bytes(
                    user_id=user_id,
                    start_image_bytes=start_image_bytes,
                    prompt=user_prompt,
                    duration_seconds=duration,
                    mode=kling_mode,
                    billing_meta={"flow": "i2v"},
                )
                await tg_send_message(chat_id, f"✅ Готово!\n{out_url}", reply_markup=_main_menu_for(user_id))
            except Exception as e:
                await tg_send_message(chat_id, f"❌ Ошибка Kling Image → Video: {e}", reply_markup=_main_menu_for(user_id))
            finally:
                st["kling_i2v"] = {"step": "need_image", "image_bytes": None, "duration": duration}
                _set_mode(chat_id, user_id, "chat")
                _busy_end(int(user_id))

            return {"ok": True}


        if st.get("mode") == "kling_mc":
            km = st.get("kling_mc") or {}
            step = (km.get("step") or "need_avatar")

            if step != "need_prompt":
                await tg_send_message(chat_id, "Жду фото/видео для Motion Control. Нажми «🎬 Видео будущего» и следуй шагам.", reply_markup=_main_menu_for(user_id))
                return {"ok": True}

            avatar_bytes = km.get("avatar_bytes")
            video_bytes = km.get("video_bytes")
            if not avatar_bytes or not video_bytes:
                await tg_send_message(chat_id, "Не хватает фото или видео. Нажми «🎬 Видео будущего» и начни заново.", reply_markup=_main_menu_for(user_id))
                return {"ok": True}

            user_prompt = incoming_text.strip()
            if user_prompt.lower() in ("старт", "start", "go"):
                user_prompt = "A person performs the same motion as in the reference video."

            await tg_send_message(chat_id, "🎬 Генерирую видео (обычно 3–7 минут)…", reply_markup=_main_menu_for(user_id))

            _busy_start(int(user_id), "Kling Motion")

            try:
                # настройки Kling из WebApp (если нет — дефолт std)
                ks = st.get("kling_settings") or {}
                quality = (ks.get("quality") or "std").lower()
                kling_mode = "pro" if quality in ("pro", "professional") else "std"

                out_url = await run_motion_control_from_bytes(
                    user_id=user_id,
                    avatar_bytes=avatar_bytes,
                    motion_video_bytes=video_bytes,
                    prompt=user_prompt or "A person performs the same motion as in the reference video.",
                    mode=kling_mode,
                    character_orientation="video",
                    keep_original_sound=True,
                    duration_seconds=vid.get("duration"),
                )
                await tg_send_message(chat_id, f"✅ Готово!\n{out_url}", reply_markup=_main_menu_for(user_id))
            except Exception as e:
                await tg_send_message(chat_id, f"❌ Ошибка Kling Motion Control: {e}", reply_markup=_main_menu_for(user_id))
            finally:
                st["kling_mc"] = {"step": "need_avatar", "avatar_bytes": None, "video_bytes": None}
                _set_mode(chat_id, user_id, "chat")
                _busy_end(int(user_id))

            return {"ok": True}


        # T2I flow: генерация Seedream по одному тексту (без входного фото)
        if st.get("mode") == "t2i":
            t2i = st.get("t2i") or {}
            step = (t2i.get("step") or "need_prompt")
            if step != "need_prompt":
                st["t2i"] = {"step": "need_prompt"}

            user_prompt = incoming_text.strip()
            if not user_prompt:
                await tg_send_message(chat_id, "Напиши описание для генерации (без фото).", reply_markup=_main_menu_for(user_id))
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
                _busy_start(int(user_id), "Seedream T2I")
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
                await tg_send_message(chat_id, f"Ошибка T2I: {e}", reply_markup=_main_menu_for(user_id))
            finally:
                _busy_end(int(user_id))
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
                await tg_send_message(chat_id, "Пришли фото для режима «Нейро фотосессии».", reply_markup=_main_menu_for(user_id))
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
                _busy_start(int(user_id), "Seedream фотосессия")
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
                await tg_send_message(chat_id, f"Ошибка нейро-фотосессии: {e}", reply_markup=_main_menu_for(user_id))
                # остаёмся в режиме, чтобы пользователь мог попробовать ещё раз
                st["photosession"] = {"step": "need_photo", "photo_bytes": None}
                st["ts"] = _now()
                _busy_end(int(user_id))
                return {"ok": True}

            if not _sent_via_edit:
                await tg_send_photo_bytes(chat_id, out_bytes, caption="Готово. Если нужно ещё — пришли новое фото.")
            _busy_end(int(user_id))  # ← ДОБАВИТЬ ЭТУ СТРОКУ
            st["photosession"] = {"step": "need_photo", "photo_bytes": None}
            st["ts"] = _now()
            return {"ok": True}
        # VISUAL flow (poster mode): после фото -> роутинг POSTER/PHOTO
        if st.get("mode") == "poster":
            poster = st.get("poster") or {}
            step: PosterStep = poster.get("step") or "need_photo"
            photo_bytes = poster.get("photo_bytes")

            if step == "need_photo" or not photo_bytes:
                await tg_send_message(chat_id, "Сначала пришли фото.", reply_markup=_main_menu_for(user_id))
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
                        _busy_start(int(user_id), "Афиша")
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
                        _busy_end(int(user_id))
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
                _busy_end(int(user_id))
                st["poster"] = {"step": "need_photo", "photo_bytes": None, "light": "bright"}
                st["ts"] = _now()
                return {"ok": True}

            await tg_send_message(chat_id, "Пришли фото, затем одним сообщением текст.", reply_markup=_main_menu_for(user_id))
            return {"ok": True}

        # CHAT: обычный текстовый ответ (с памятью только для режима ИИ-чата)
        if st.get("mode") == "chat":
            # update summary if we have enough trimmed messages
            try:
                await _ai_maybe_summarize(st)
            except Exception:
                pass

            summary = _ai_summary_get(st)
            hist = _ai_hist_get(st)

            history_for_model: List[Dict[str, str]] = []
            if summary:
                history_for_model.append({
                    "role": "system",
                    "content": f"Краткое резюме диалога (для контекста):\n{summary}",
                })
            # last N messages
            history_for_model.extend(hist)

            answer = await openai_chat_answer(
                user_text=incoming_text,
                system_prompt=DEFAULT_TEXT_SYSTEM_PROMPT,
                image_bytes=None,
                temperature=0.6,
                max_tokens=700,
                history=history_for_model,
            )

            # store ONLY chat dialog
            _ai_hist_add(st, "user", incoming_text)
            _ai_hist_add(st, "assistant", answer)

            await tg_send_message(chat_id, answer, reply_markup=_main_menu_for(user_id))
            return {"ok": True}

        # fallback (should not happen): if not in chat mode, just answer without memory
        answer = await openai_chat_answer(
            user_text=incoming_text,
            system_prompt=DEFAULT_TEXT_SYSTEM_PROMPT,
            image_bytes=None,
            temperature=0.6,
            max_tokens=700,
        )
        await tg_send_message(chat_id, answer, reply_markup=_main_menu_for(user_id))
        return {"ok": True}

    return {"ok": True}
