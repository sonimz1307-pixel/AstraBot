import os
import base64
import time
import asyncio
import re
import json
import urllib.parse
import hashlib
import hmac
import logging
import tempfile
from uuid import uuid4, uuid5, NAMESPACE_URL
from io import BytesIO
from typing import Optional, Literal, Dict, Any, Tuple, List, Union

import httpx
from queue_redis import enqueue_job, enqueue_job_delayed
from chat_file_text import extract_file_text
from chat_memory_redis import reset_tg_chat_memory
from kie_claude_chat import (
    KIE_CLAUDE_DISPLAY_NAME,
    KIE_CLAUDE_HISTORY_MESSAGES,
    KIE_CLAUDE_MODEL_ID,
    KIE_CLAUDE_OPUS_MODEL_ID,
    KIE_CLAUDE_FABLE_MODEL_ID,
    KIE_CLAUDE_FABLE_DISPLAY_NAME,
    KIE_CLAUDE_FABLE_THINKING_EXTRA_TOKENS,
    KIE_CLAUDE_FABLE_MAX_TOKENS,
    kie_claude_answer,
    kie_claude_fable_tokens,
    kie_claude_is_fable_model,
    kie_claude_display_name,
    kie_claude_summarize_dialogue,
    normalize_kie_claude_model,
)
from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from db_supabase import track_user_activity, get_basic_stats, supabase as sb
from kling_flow import run_motion_control_from_bytes, run_image_to_video_from_bytes, run_text_to_video_from_prompt, upload_bytes_to_supabase
from kling3_motion_kie import run_kling3_motion_kie_from_bytes, normalize_kling3_motion_resolution
from veo_flow import run_veo_text_to_video, run_veo_image_to_video
from veo_billing import calc_veo_charge, format_veo_charge_line
from billing_db import (
    ensure_user_row,
    get_balance,
    get_balance_history,
    add_tokens,
    ledger_ref_exists,
    resolve_billing_user_id,
    charge_photosession_generation,
    refund_photosession_generation,
    grant_welcome_bonus_once,
)
from free_plan_limits import (
    FEATURE_CHAT,
    FEATURE_TTS,
    FreePlanLimitError,
    consume_free_usage,
    is_free_plan_user,
    release_free_usage,
    validate_free_tts_text,
)
from nano_banana import run_nano_banana
from nano_banana_pro_new_kie import nano_banana_pro_new_cost
from gpt_image_2_kie import (
    KIE_GPT_IMAGE_2_MAX_INPUT_MB,
    KIE_GPT_IMAGE_2_MAX_INPUT_BYTES,
    gpt_image_2_kie_cost,
    normalize_gpt_image_2_kie_resolution,
    normalize_gpt_image_2_kie_aspect_ratio,
    normalize_gpt_image_2_kie_options,
    validate_gpt_image_2_kie_reference_bytes,
)
from topaz_pricing import (
    get_photo_preset_tokens,
    get_photo_preset_settings,
    calc_video_retail_tokens,
    get_video_preset_settings,
)
from yookassa_flow import create_yookassa_payment, fetch_yookassa_payment
from subscriptions_db import get_current_subscription, get_subscription_plan, set_user_subscription, extend_user_subscription
from kling3_pricing import calculate_kling3_price
from kling3_telegram_handler import handle_kling3_wait_prompt
from kling3_kie_telegram_handler import handle_kling3_kie_wait_prompt
from kling3_turbo_telegram_handler import handle_kling3_turbo_wait_prompt
from kling3_turbo_kie import (
    calculate_kling3_turbo_price,
    normalize_kling3_turbo_aspect_ratio,
    normalize_kling3_turbo_duration,
    normalize_kling3_turbo_mode,
    normalize_kling3_turbo_resolution,
)
from grok_video_replicate import (
    GROK15_MODEL,
    GROK_LEGACY_MODEL,
    grok15_tokens_for_duration,
    grok_tokens_for_duration,
    is_grok15_model,
    normalize_grok15_aspect_ratio,
    normalize_grok15_duration,
    normalize_grok15_resolution,
    normalize_grok_aspect_ratio,
    normalize_grok_duration,
    normalize_grok_model,
    normalize_grok_provider_mode,
    normalize_grok_resolution,
    validate_grok15_input_image,
)
from gemini_omni_video import (
    KIE_OMNI_VIDEO_EDIT_MAX_DURATION_SEC,
    gemini_omni_tokens_for_duration,
    gemini_omni_tokens_for_run,
    normalize_gemini_omni_aspect_ratio,
    normalize_gemini_omni_duration,
    normalize_gemini_omni_mode,
    normalize_gemini_omni_resolution,
)
from veo31_fast_relax_kie import (
    VEO31_FAST_RELAX_DISPLAY_NAME,
    normalize_veo31_fast_relax_aspect_ratio,
    normalize_veo31_fast_relax_duration,
    normalize_veo31_fast_relax_resolution,
    upload_veo31_fast_relax_input_image,
    veo31_fast_relax_tokens_for_run,
)
from seedance_kie import seedance_kie_tokens_for_duration
from app.routers.tts import router as tts_router
from app.services.video_editor_service import create_workspace_upload_record, probe_media

app = FastAPI()

# CORS: production must allow only real frontends.
# Website frontend: https://nabex.ru / https://www.nabex.ru
# Telegram WebApp frontend: https://astrabot-tchj.onrender.com
# Do not use a wildcard such as https://.*.onrender.com in production.
DEFAULT_WORKSPACE_ALLOWED_ORIGINS = (
    "https://nabex.ru,"
    "https://www.nabex.ru,"
    "https://astrabot-tchj.onrender.com"
)

WORKSPACE_ALLOWED_ORIGINS = [
    origin.strip().rstrip("/")
    for origin in os.getenv("WORKSPACE_ALLOWED_ORIGINS", DEFAULT_WORKSPACE_ALLOWED_ORIGINS).split(",")
    if origin.strip()
]

# Empty env value must mean "no regex", not an accidentally broad regex.
_raw_workspace_origin_regex = (os.getenv("WORKSPACE_ALLOWED_ORIGIN_REGEX") or "").strip()
WORKSPACE_ALLOWED_ORIGIN_REGEX = _raw_workspace_origin_regex or None

app.add_middleware(
    CORSMiddleware,
    allow_origins=WORKSPACE_ALLOWED_ORIGINS,
    allow_origin_regex=WORKSPACE_ALLOWED_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# --- static files (/static/...) ---
app.mount("/static", StaticFiles(directory="static"), name="static")
from app.routers.leads import router as leads_router
from app.routers.kling3 import router as kling3_router
from app.routers.kling3_kie import router as kling3_kie_router
from app.routers.admin_auth import router as admin_auth_router
from app.routers.admin_top import router as admin_top_router
from app.routers.provider_balances_admin import router as provider_balances_admin_router
from app.routers.prompts import router as prompts_router
from app.routers.prompts_admin import router as prompts_admin_router
from app.routers.songwriter import router as songwriter_router
from app.routers.web_workspace_api import router as workspace_router
from app.routers.video_editor_v2 import router as video_editor_v2_router, page_router as video_editor_v2_page_router
from app.routers.site_builder_api import router as site_builder_router
from app.routers.partner_program_api import router as partner_program_router
from app.services.partner_program import ensure_partner_profile
from app.services.legnext_midjourney import (
    build_midjourney_v7_prompt,
    normalize_midjourney_model,
    normalize_midjourney_speed_mode,
)
app.include_router(leads_router, prefix="/api/leads", tags=["leads"])
app.include_router(kling3_router, prefix="/api/kling3", tags=["kling3"])
app.include_router(kling3_kie_router, prefix="/api/kling3-kie", tags=["kling3-kie"])
app.include_router(admin_auth_router)
app.include_router(admin_top_router, prefix="/api/admin", tags=["admin"])
app.include_router(provider_balances_admin_router, prefix="/api/admin", tags=["admin-balances"])
app.include_router(prompts_router, prefix="/api/prompts", tags=["prompts"])
app.include_router(prompts_admin_router, prefix="/api/prompts_admin", tags=["prompts_admin"])
app.include_router(tts_router)
app.include_router(songwriter_router)
app.include_router(workspace_router)
app.include_router(partner_program_router)
app.include_router(site_builder_router)
app.include_router(video_editor_v2_router)
app.include_router(video_editor_v2_page_router)

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
WORKSPACE_MEDIA_QUEUE_NAME = (os.getenv("WORKSPACE_MEDIA_QUEUE_NAME", "workspace_media") or "workspace_media").strip() or "workspace_media"
WORKSPACE_VEO_RELAX_QUEUE_NAME = (os.getenv("WORKSPACE_VEO_RELAX_QUEUE_NAME", "workspace_veo_relax") or "workspace_veo_relax").strip() or "workspace_veo_relax"

def _env_non_negative_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default)) or str(default)))
    except Exception:
        return int(default)

VEO_RELAX_NEXUS_DELAY_SEC = _env_non_negative_int("VEO_RELAX_NEXUS_DELAY_SEC", 1500)
WORKSPACE_GROK15_QUEUE_NAME = (os.getenv("WORKSPACE_GROK15_QUEUE_NAME", "workspace_grok15") or "workspace_grok15").strip() or "workspace_grok15"
PARTNER_EVENTS_QUEUE_NAME = (os.getenv("PARTNER_EVENTS_QUEUE_NAME", "partner_events") or "partner_events").strip() or "partner_events"


def _extract_partner_ref_from_start(text: str) -> str:
    """Extract ref code from /start ref_CODE or /start ref-CODE."""
    raw = str(text or "").strip()
    if not raw.startswith("/start"):
        return ""
    parts = raw.split(maxsplit=1)
    if len(parts) < 2:
        return ""
    payload = parts[1].strip()
    if payload.lower().startswith("ref_"):
        payload = payload[4:]
    elif payload.lower().startswith("ref-"):
        payload = payload[4:]
    else:
        return ""
    payload = re.sub(r"[^A-Za-z0-9_\-]", "", payload).upper()
    return payload[:64]


def _resolve_partner_referred_user_id(user_id: int) -> int:
    """
    Partner accounting must use workspace_accounts.id when a Telegram user is
    linked to a site/email workspace account. Otherwise a referral created on
    the site by workspace account id will not match a later Telegram top-up.
    """
    try:
        raw_user_id = int(user_id or 0)
    except Exception:
        return 0
    if raw_user_id <= 0:
        return 0

    try:
        if sb is None:
            return raw_user_id
        res = (
            sb.table("workspace_accounts")
            .select("id,telegram_user_id")
            .eq("telegram_user_id", raw_user_id)
            .limit(1)
            .execute()
        )
        row = (getattr(res, "data", None) or [None])[0]
        if row:
            account_id = int(row.get("id") or 0)
            if account_id > 0:
                return account_id
    except Exception as exc:
        try:
            print(f"[partner] failed to resolve workspace account for telegram_user_id={raw_user_id}: {exc}", flush=True)
        except Exception:
            pass

    return raw_user_id


async def _enqueue_partner_bind_referral(user_id: int, ref_code: str, *, source: str, meta: Optional[Dict[str, Any]] = None) -> None:
    code = str(ref_code or "").strip().upper()
    raw_user_id = int(user_id or 0)
    partner_referred_user_id = _resolve_partner_referred_user_id(raw_user_id)
    if not code or partner_referred_user_id <= 0:
        return

    meta_payload = dict(meta or {})
    if partner_referred_user_id != raw_user_id:
        meta_payload.setdefault("telegram_user_id", raw_user_id)
        meta_payload.setdefault("workspace_user_id", partner_referred_user_id)

    try:
        await enqueue_job(
            {
                "job_id": f"partner_bind_{uuid4().hex}",
                "kind": "partner_bind_referral",
                "referred_user_id": partner_referred_user_id,
                "ref_code": code,
                "source": source,
                "meta": meta_payload,
            },
            queue_name=PARTNER_EVENTS_QUEUE_NAME,
        )
    except Exception as exc:
        try:
            print(f"[partner] failed to enqueue bind referral: {exc}", flush=True)
        except Exception:
            pass


async def _enqueue_partner_topup_event(
    *,
    user_id: int,
    payment_id: str,
    amount_rub: float,
    tokens: int,
    provider: str,
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    raw_user_id = int(user_id or 0)
    partner_referred_user_id = _resolve_partner_referred_user_id(raw_user_id)
    if partner_referred_user_id <= 0 or not str(payment_id or "").strip() or float(amount_rub or 0) <= 0:
        return

    meta_payload = dict(meta or {})
    if partner_referred_user_id != raw_user_id:
        meta_payload.setdefault("telegram_user_id", raw_user_id)
        meta_payload.setdefault("workspace_user_id", partner_referred_user_id)

    try:
        await enqueue_job(
            {
                "job_id": f"partner_topup_{uuid4().hex}",
                "kind": "partner_topup",
                "referred_user_id": partner_referred_user_id,
                "source_payment_id": str(payment_id).strip(),
                "payment_amount_rub": float(amount_rub or 0),
                "purchased_tokens": int(tokens or 0),
                "payment_provider": str(provider or "unknown"),
                "meta": meta_payload,
            },
            queue_name=PARTNER_EVENTS_QUEUE_NAME,
        )
    except Exception as exc:
        try:
            print(f"[partner] failed to enqueue topup event: {exc}", flush=True)
        except Exception:
            pass

CHAT_WORKER_ENABLED = (os.getenv("CHAT_WORKER_ENABLED", "1") or "1").strip().lower() not in {"0", "false", "no", "off"}
TG_CHAT_OPENAI_QUEUE_NAME = (os.getenv("TG_CHAT_OPENAI_QUEUE_NAME", "tg_chat_openai") or "tg_chat_openai").strip() or "tg_chat_openai"
TG_CHAT_CLAUDE_QUEUE_NAME = (os.getenv("TG_CHAT_CLAUDE_QUEUE_NAME", "tg_chat_claude") or "tg_chat_claude").strip() or "tg_chat_claude"
TG_CHAT_FABLE_QUEUE_NAME = (os.getenv("TG_CHAT_FABLE_QUEUE_NAME", "tg_chat_fable") or "tg_chat_fable").strip() or "tg_chat_fable"
TG_STT_QUEUE_NAME = (os.getenv("TG_STT_QUEUE_NAME", "redactor_tg_stt") or "redactor_tg_stt").strip() or "redactor_tg_stt"

BALANCE_BANNER_PATH = os.getenv("BALANCE_BANNER_PATH", "").strip()

def _read_balance_banner_bytes() -> Optional[bytes]:
    candidates = []
    if BALANCE_BANNER_PATH:
        candidates.append(BALANCE_BANNER_PATH)
    candidates.extend([
        os.path.join(BASE_DIR, "static", "img", "balance_banner.png"),
        os.path.join(BASE_DIR, "static", "img", "balance_banner.jpg"),
        os.path.join(BASE_DIR, "balance_banner.png"),
        os.path.join(BASE_DIR, "balance_banner.jpg"),
    ])
    seen = set()
    for p in candidates:
        if not p or p in seen:
            continue
        seen.add(p)
        try:
            if os.path.exists(p) and os.path.isfile(p):
                with open(p, "rb") as f:
                    return f.read()
        except Exception:
            continue
    return None

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

    # strip common leading menu emojis so exact-match works
    for em in ("🎬", "🎵", "💰", "📊", "⬅", "🔄", "➕", "🍌", "🖼", "🔎", "🎞", "📹"):
        if t.startswith(em):
            t = t[len(em):].strip()

    return t


def _is_nav_or_menu_text(t: str) -> bool:
    """
    True only if the incoming text EXACTLY matches a navigation/menu command (not a free-form prompt).

    IMPORTANT:
    - Do NOT use substring matching (it breaks prompts like "сгенерируй видео ...").
    - We normalize leading emojis (🎬🎵💰📊⬅🔄➕🍌) and checkmarks (✅☑️✔️).
    """
    s = _normalize_btn_text(t).lower()
    if not s:
        return False

    nav_exact = {
        # basic nav
        "наз", "назад", "в меню", "главное меню", "меню", "start", "/start",
        "помощь", "help", "/help",
        "сброс", "reset", "/reset", "отмена", "cancel", "/cancel",
        # main menu buttons (texts)
        "ии (чат)", "ии", "ии чат", "чат", "ai chat", "chatgpt", "gpt",
        "фото будущего", "видео будущего", "музыка будущего",
        "озвучить текст", "для pro", "промпты",
        "баланс", "профиль", "тарифы", "оплата", "пополнить",
        "статистика", "рассылка",
        # submenus you use in keyboards
        "фото/афиши", "нейро фотосессии", "картинка+картинка", "2 фото", "nano banana", "nano banana 2",
        "nano banana pro", "nano banana pro new", "nano banana pro - new",
        "seedream", "midjourney", "миджорни", "апскейл", "апскейл фото", "апскейл видео",
        "topaz фото • standard • 2 токена", "topaz фото • detail • 3 токена", "topaz фото • max • 4 токена",
        "topaz видео • hd smooth • 1 токен / 5 сек",
        "topaz видео • full hd • 2 токена / 5 сек",
        "topaz видео • full hd smooth • 3 токена / 5 сек",
        "афиша: ярко", "афиша: кино",
        "gpt image 2.0", "картинка→картинка",
        "текст→картинка",
        "🔄 сбросить генерацию".lower(),
    }

    return s in nav_exact


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


@app.get("/webapp/top_analizator", response_class=HTMLResponse)
async def webapp_top_analizator():
    with open(os.path.join(BASE_DIR, "webapp_top_analizator.html"), "r", encoding="utf-8") as f:
        return f.read()
        
@app.get("/webapp/prompts", response_class=HTMLResponse)
async def webapp_prompts():
    with open(os.path.join(BASE_DIR, "webapp_prompts.html"), "r", encoding="utf-8") as f:
        return f.read()

@app.get("/webapp/prompts_admin", response_class=HTMLResponse)
async def webapp_prompts_admin():
    with open(os.path.join(BASE_DIR, "webapp_prompts_admin.html"), "r", encoding="utf-8") as f:
        return f.read()


@app.get("/webapp/account", response_class=HTMLResponse)
async def webapp_account():
    with open(os.path.join(BASE_DIR, "webapp_account.html"), "r", encoding="utf-8") as f:
        return f.read()

@app.get("/webapp/topup", response_class=HTMLResponse)
async def webapp_topup():
    with open(os.path.join(BASE_DIR, "webapp_topup.html"), "r", encoding="utf-8") as f:
        return f.read()
        
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
WEBAPP_KLING_URL = os.getenv("WEBAPP_KLING_URL", "https://astrabot-tchj.onrender.com/webapp/kling")
WEBAPP_MUSIC_URL = os.getenv("WEBAPP_MUSIC_URL", "https://astrabot-tchj.onrender.com/webapp/music")
WEBAPP_TOP_ANALIZATOR_URL = os.getenv("WEBAPP_TOP_ANALIZATOR_URL", "https://astrabot-tchj.onrender.com/webapp/top_analizator")
WEBAPP_PROMPTS_URL = os.getenv("WEBAPP_PROMPTS_URL", "https://astrabot-tchj.onrender.com/webapp/prompts")
WEBAPP_PROMPTS_ADMIN_URL = os.getenv("WEBAPP_PROMPTS_ADMIN_URL", "https://astrabot-tchj.onrender.com/webapp/prompts_admin")
WEBAPP_ACCOUNT_URL = os.getenv("WEBAPP_ACCOUNT_URL", "https://astrabot-tchj.onrender.com/webapp/account")
WEBAPP_TOPUP_URL = os.getenv("WEBAPP_TOPUP_URL", "https://astrabot-tchj.onrender.com/webapp/topup")
NABEX_PUBLIC_SITE_URL = (os.getenv("NABEX_PUBLIC_SITE_URL") or os.getenv("PARTNER_SITE_URL") or os.getenv("PUBLIC_SITE_URL") or "https://nabex.ru").strip().rstrip("/")
NABEX_SUPPORT_URL = (os.getenv("NABEX_SUPPORT_URL") or os.getenv("SUPPORT_URL") or "https://t.me/HelpNeiroAstra").strip()
NABEX_PARTNER_BOT_USERNAME = (os.getenv("PARTNER_BOT_USERNAME") or os.getenv("TELEGRAM_BOT_USERNAME") or os.getenv("BOT_USERNAME") or "NeiroAstraBot").strip().lstrip("@")


def _verify_telegram_webapp_init_data(init_data: str) -> Optional[dict]:
    """Verify Telegram WebApp initData and return parsed user dict if valid."""
    raw = str(init_data or "").strip()
    if not raw or not TELEGRAM_BOT_TOKEN:
        return None
    try:
        parsed = urllib.parse.parse_qsl(raw, keep_blank_values=True)
        data = dict(parsed)
        received_hash = data.pop("hash", "")
        if not received_hash:
            return None
        check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret_key = hmac.new(b"WebAppData", TELEGRAM_BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated_hash, received_hash):
            return None
        user_raw = data.get("user") or "{}"
        user = json.loads(user_raw) if isinstance(user_raw, str) else {}
        return user if isinstance(user, dict) else None
    except Exception:
        return None


def _tg_account_ref_links_for(user_id: int) -> Tuple[str, str, str]:
    """Return (ref_code, site_ref, bot_ref) for Telegram account cabinet."""
    ref_code = ""
    try:
        partner_user_id = _resolve_partner_referred_user_id(int(user_id))
        profile = ensure_partner_profile(partner_user_id)
        ref_code = str((profile or {}).get("ref_code") or "").strip().upper()
    except Exception as exc:
        try:
            print(f"[webapp_account] partner profile unavailable for user_id={user_id}: {exc}", flush=True)
        except Exception:
            pass

    if ref_code:
        site_ref = f"{NABEX_PUBLIC_SITE_URL}/?ref={urllib.parse.quote(ref_code)}"
        bot_ref = f"https://t.me/{NABEX_PARTNER_BOT_USERNAME}?start=ref_{urllib.parse.quote(ref_code)}" if NABEX_PARTNER_BOT_USERNAME else ""
    else:
        # Fallback: links stay usable for display even if partner tables are temporarily unavailable.
        site_ref = f"{NABEX_PUBLIC_SITE_URL}/?ref={int(user_id)}"
        bot_ref = f"https://t.me/{NABEX_PARTNER_BOT_USERNAME}?start={int(user_id)}" if NABEX_PARTNER_BOT_USERNAME else ""
    return ref_code, site_ref, bot_ref


def _money_rub_for_tg(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except Exception:
        return 0.0


def _tg_partner_balance_for(user_id: int) -> Dict[str, Any]:
    """Return partner balance for Telegram WebApp account response.

    Website partner cabinet already reads partner_balances. Telegram account
    response used to expose only ref links, so the WebApp showed 0 ₽ even when
    partner_balances had available/earned amounts.
    """
    empty = {
        "available_balance_rub": 0.0,
        "earned_total_rub": 0.0,
        "pending_payout_balance_rub": 0.0,
        "paid_total_rub": 0.0,
    }
    try:
        partner_user_id = _resolve_partner_referred_user_id(int(user_id))
        if partner_user_id <= 0 or sb is None:
            return empty
        res = (
            sb.table("partner_balances")
            .select("partner_user_id,earned_total_rub,available_balance_rub,pending_payout_balance_rub,paid_total_rub,updated_at")
            .eq("partner_user_id", partner_user_id)
            .limit(1)
            .execute()
        )
        row = (getattr(res, "data", None) or [None])[0] or {}
        if not row:
            return empty
        return {
            "partner_user_id": int(row.get("partner_user_id") or partner_user_id),
            "available_balance_rub": _money_rub_for_tg(row.get("available_balance_rub")),
            "earned_total_rub": _money_rub_for_tg(row.get("earned_total_rub")),
            "pending_payout_balance_rub": _money_rub_for_tg(row.get("pending_payout_balance_rub")),
            "paid_total_rub": _money_rub_for_tg(row.get("paid_total_rub")),
            "updated_at": row.get("updated_at"),
        }
    except Exception as exc:
        try:
            print(f"[webapp_account] partner balance unavailable for user_id={user_id}: {exc}", flush=True)
        except Exception:
            pass
        return empty



def _tg_user_id_from_request(request: Request, payload: Optional[Dict[str, Any]] = None) -> int:
    """Resolve Telegram user_id from verified WebApp initData, then fallback query/body uid."""
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    tg_user = _verify_telegram_webapp_init_data(init_data)

    user_id = 0
    if tg_user and tg_user.get("id"):
        try:
            user_id = int(tg_user.get("id") or 0)
        except Exception:
            user_id = 0

    if user_id > 0:
        return user_id

    payload = payload if isinstance(payload, dict) else {}
    for value in (
        payload.get("uid"),
        payload.get("tg_user_id"),
        payload.get("user_id"),
        request.query_params.get("uid"),
        request.query_params.get("tg_user_id"),
        request.query_params.get("user_id"),
    ):
        try:
            user_id = int(value or 0)
        except Exception:
            user_id = 0
        if user_id > 0:
            return user_id

    return 0


def _subscription_public_payload_for_tg(user_id: int) -> Dict[str, Any]:
    try:
        raw = get_current_subscription(int(user_id or 0))
    except Exception:
        raw = {"is_active": False, "status": "free", "plan_code": "free", "plan": {"code": "free", "name": "Free", "tokens": 0, "price_rub": 0}}
    plan = raw.get("plan") if isinstance(raw.get("plan"), dict) else {}
    plan_code = str(raw.get("plan_code") or plan.get("code") or "free").strip().lower() or "free"
    return {
        "is_active": bool(raw.get("is_active")),
        "status": str(raw.get("status") or ("active" if raw.get("is_active") else "free")),
        "plan_code": plan_code,
        "plan": {
            "code": str(plan.get("code") or plan_code),
            "name": str(plan.get("name") or plan_code.title()),
            "tokens": int(float(plan.get("tokens") or 0)),
            "price_rub": int(float(plan.get("price_rub") or 0)),
            "duration_days": int(float(plan.get("duration_days") or 0)),
            "features": plan.get("features") or {},
        },
        "starts_at": raw.get("starts_at"),
        "expires_at": raw.get("expires_at"),
        "days_left": int(float(raw.get("days_left") or 0)),
    }


def _public_subscription_plan_for_tg(plan_code: Any) -> Optional[Dict[str, Any]]:
    code = str(plan_code or "").strip().lower()
    if code not in PUBLIC_SUBSCRIPTION_PLAN_CODES:
        return None
    try:
        plan = get_subscription_plan(code)
    except Exception:
        return None
    if not bool(plan.get("is_active", True)):
        return None
    return dict(plan)


def _seedream_t2i_is_included_for_user(user_id: int) -> bool:
    try:
        sub = get_current_subscription(int(user_id or 0))
        code = str(sub.get("plan_code") or "").strip().lower()
        return bool(sub.get("is_active")) and code in SEEDREAM_T2I_INCLUDED_PLAN_CODES
    except Exception:
        return False


def _active_subscription_plan_code_for_user(user_id: int) -> str:
    try:
        sub = get_current_subscription(int(user_id or 0))
        code = str(sub.get("plan_code") or "").strip().lower()
        return code if bool(sub.get("is_active")) else ""
    except Exception:
        return ""


def _nano_banana_basic_is_included_for_user(user_id: int, provider: str = "nano_banana", resolution: str = "2K") -> bool:
    provider_key = str(provider or "").strip().lower()
    if provider_key != "nano_banana":
        return False
    if str(resolution or "2K").strip().upper() == "4K":
        return False
    return _active_subscription_plan_code_for_user(user_id) in NANO_BANANA_BASIC_INCLUDED_PLAN_CODES


def _nano_banana_basic_cost_hint_for_user(user_id: int, provider: str = "nano_banana", resolution: str = "2K") -> str:
    if _nano_banana_basic_is_included_for_user(user_id, provider=provider, resolution=resolution):
        return "Стоимость: 0 токенов по Pulse/Nexus."
    return "Стоимость: 1 токен за результат."


def _midjourney_is_included_for_user(user_id: int, model: str = "midjourney-v7", speed_mode: str = "fast", resolution: str = "2K") -> bool:
    model_key = normalize_midjourney_model(model)
    speed = normalize_midjourney_speed_mode(speed_mode, model=model_key)
    if model_key not in MIDJOURNEY_INCLUDED_MODELS:
        return False
    if speed == "turbo":
        return False
    if str(resolution or "2K").strip().upper() == "4K":
        return False
    return _active_subscription_plan_code_for_user(user_id) in MIDJOURNEY_INCLUDED_PLAN_CODES


def _midjourney_user_cost(user_id: Optional[int], model: str = "midjourney-v7", speed_mode: str = "fast", resolution: str = "2K") -> int:
    if user_id is not None and _midjourney_is_included_for_user(int(user_id or 0), model=model, speed_mode=speed_mode, resolution=resolution):
        return 0
    return int(_midjourney_cost(model, speed_mode))


def _gpt_image_2_kie_is_included_for_user(user_id: int, resolution: str = "2K") -> bool:
    # Nexus includes GPT Image 2.0 only in non-4K KIE modes.
    if str(resolution or "2K").strip().upper() == "4K":
        return False
    return _active_subscription_plan_code_for_user(int(user_id or 0)) in GPT_IMAGE_2_INCLUDED_PLAN_CODES


def _gpt_image_2_kie_user_cost(user_id: int, resolution: str = "2K") -> int:
    if _gpt_image_2_kie_is_included_for_user(int(user_id or 0), resolution):
        return 0
    return int(gpt_image_2_kie_cost(resolution))


def _veo31_fast_relax_is_included_for_user(user_id: int) -> bool:
    return _active_subscription_plan_code_for_user(int(user_id or 0)) in VEO31_FAST_RELAX_INCLUDED_PLAN_CODES


def _veo31_fast_relax_delay_sec_for_user(user_id: int, cost_tokens: int = 0) -> int:
    # Delayed real provider start is only for the free Nexus Relax run.
    if int(cost_tokens or 0) == 0 and _veo31_fast_relax_is_included_for_user(int(user_id or 0)):
        return int(VEO_RELAX_NEXUS_DELAY_SEC or 0)
    return 0


def _veo31_fast_relax_queue_note(delay_sec: int) -> str:
    if int(delay_sec or 0) > 0:
        return "принят в Relax-очередь; реальный запуск начнётся автоматически, когда подойдёт слот"
    return "поставлен в отдельную очередь"


@app.get("/api/tg/account")
async def tg_account_info(request: Request):
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    tg_user = _verify_telegram_webapp_init_data(init_data)

    user_id = 0
    if tg_user and tg_user.get("id"):
        try:
            user_id = int(tg_user.get("id") or 0)
        except Exception:
            user_id = 0

    # Fallback for testing in browser and for old Telegram clients.
    if user_id <= 0:
        for key in ("uid", "tg_user_id", "user_id"):
            try:
                user_id = int(request.query_params.get(key) or 0)
            except Exception:
                user_id = 0
            if user_id > 0:
                break

    if user_id <= 0:
        return {
            "ok": False,
            "error": "user_id_required",
            "balance": 0,
            "site_ref": "",
            "bot_ref": "",
            "site_url": NABEX_PUBLIC_SITE_URL,
            "ref_cabinet_url": f"{NABEX_PUBLIC_SITE_URL}/#workspace",
            "topup_url": f"{NABEX_PUBLIC_SITE_URL}/tariffs.html",
            "account_webapp_url": WEBAPP_ACCOUNT_URL,
            "topup_webapp_url": WEBAPP_TOPUP_URL,
            "support_url": NABEX_SUPPORT_URL,
            "offer_url": f"{NABEX_PUBLIC_SITE_URL}/terms.html",
            "policy_url": f"{NABEX_PUBLIC_SITE_URL}/privacy.html",
            "contact_email": "",
            "subscription": _subscription_public_payload_for_tg(0),
            "partner_balance": _tg_partner_balance_for(0),
        }

    balance = 0
    try:
        ensure_user_row(user_id)
        balance = int(get_balance(user_id) or 0)
    except Exception as exc:
        try:
            print(f"[webapp_account] balance unavailable for user_id={user_id}: {exc}", flush=True)
        except Exception:
            pass

    ref_code, site_ref, bot_ref = _tg_account_ref_links_for(user_id)
    partner_balance = _tg_partner_balance_for(user_id)
    contact_email = ""
    try:
        contact_email = sb_get_user_email(user_id)
    except Exception:
        contact_email = ""

    return {
        "ok": True,
        "user_id": user_id,
        "balance": balance,
        "ref_code": ref_code,
        "site_ref": site_ref,
        "bot_ref": bot_ref,
        "site_url": NABEX_PUBLIC_SITE_URL,
        "ref_cabinet_url": f"{NABEX_PUBLIC_SITE_URL}/#workspace",
        "topup_url": f"{NABEX_PUBLIC_SITE_URL}/tariffs.html",
        "account_webapp_url": _with_uid(WEBAPP_ACCOUNT_URL, user_id),
        "topup_webapp_url": _with_uid(WEBAPP_TOPUP_URL, user_id),
        "support_url": NABEX_SUPPORT_URL,
        "offer_url": f"{NABEX_PUBLIC_SITE_URL}/terms.html",
        "policy_url": f"{NABEX_PUBLIC_SITE_URL}/privacy.html",
        "contact_email": contact_email,
        "subscription": _subscription_public_payload_for_tg(user_id),
        "partner_balance": partner_balance,
    }


@app.get("/api/tg/balance/history")
async def tg_balance_history(request: Request, limit: int = 50):
    user_id = _tg_user_id_from_request(request)
    if user_id <= 0:
        return {
            "ok": False,
            "error": "user_id_required",
            "items": [],
            "balance_tokens": 0,
            "message": "Откройте личный кабинет из Telegram-бота.",
        }

    try:
        safe_limit = max(1, min(int(limit or 50), 100))
    except Exception:
        safe_limit = 50

    try:
        ensure_user_row(user_id)
        items = get_balance_history(user_id, limit=safe_limit)
        balance = int(get_balance(user_id) or 0)
        return {"ok": True, "items": items, "balance_tokens": balance}
    except Exception as exc:
        try:
            print(f"[webapp_account] balance history unavailable for user_id={user_id}: {exc}", flush=True)
        except Exception:
            pass
        return {
            "ok": False,
            "error": "history_unavailable",
            "items": [],
            "balance_tokens": 0,
            "message": "Не удалось загрузить историю токенов.",
        }


@app.post("/api/tg/subscription/create")
async def tg_subscription_create(request: Request):
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}

    user_id = _tg_user_id_from_request(request, payload)
    if user_id <= 0:
        return {"ok": False, "error": "user_id_required", "message": "Откройте личный кабинет из Telegram-бота."}

    plan_code = str(payload.get("plan_code") or "").strip().lower()
    plan = _public_subscription_plan_for_tg(plan_code)
    if not plan:
        return {"ok": False, "error": "plan_not_available", "message": "Этот тариф пока нельзя подключить через Telegram."}

    plan_name = str(plan.get("name") or plan_code.title())
    try:
        current_sub = get_current_subscription(user_id)
    except Exception:
        current_sub = {}
    current_plan_code = str(current_sub.get("plan_code") or "").strip().lower()
    if bool(current_sub.get("is_active")) and current_plan_code == plan_code:
        return {
            "ok": False,
            "error": "active_same_plan",
            "message": f"У вас уже активен тариф {plan_name}. Повторная покупка этого же тарифа отключена.",
            "subscription": _subscription_public_payload_for_tg(user_id),
        }

    if not _yookassa_enabled():
        return {"ok": False, "error": "yookassa_disabled", "message": "Оплата тарифов через ЮKassa сейчас не настроена."}

    email = str(payload.get("email") or "").strip().lower()
    stored_email = ""
    try:
        stored_email = sb_get_user_email(user_id)
    except Exception:
        stored_email = ""

    if email:
        if not _EMAIL_RE.match(email):
            return {"ok": False, "need_email": True, "message": "Введите корректный email для чека."}
        try:
            sb_set_user_email(user_id, email)
        except Exception:
            pass
        stored_email = email

    if not stored_email:
        return {"ok": False, "need_email": True, "message": "Для оплаты тарифа картой или СБП нужен email для чека."}

    price_rub = int(float(plan.get("price_rub") or 0))
    tokens = int(float(plan.get("tokens") or 0))
    duration_days = int(float(plan.get("duration_days") or 30))
    if price_rub <= 0 or tokens < 0 or duration_days <= 0:
        return {"ok": False, "error": "bad_plan", "message": "Тариф настроен некорректно."}

    return_url = str(payload.get("return_url") or WEBAPP_ACCOUNT_URL or "https://t.me").strip()
    if not (return_url.startswith("https://") or return_url.startswith("http://")):
        return_url = WEBAPP_ACCOUNT_URL or "https://t.me"

    try:
        payment_id, url = await create_yookassa_payment(
            amount_rub=price_rub,
            description=f"Тариф {plan_name}: {tokens} токенов на {duration_days} дней",
            user_id=user_id,
            tokens=tokens,
            customer_email=stored_email,
            return_url=return_url,
            payment_metadata={
                "payment_type": "subscription",
                "plan_code": plan_code,
                "duration_days": duration_days,
                "amount_rub": price_rub,
            },
            receipt_item_description=f"Тариф {plan_name} на {duration_days} дней + {tokens} токенов Nabex",
        )
    except Exception as exc:
        return {"ok": False, "error": "payment_create_failed", "message": f"Не удалось создать оплату тарифа: {exc}"}

    return {
        "ok": True,
        "method": "yookassa",
        "payment_id": payment_id,
        "confirmation_url": url,
        "plan_code": plan_code,
        "plan_name": plan_name,
        "tokens": tokens,
        "amount_rub": price_rub,
        "duration_days": duration_days,
        "customer_email": stored_email,
    }


@app.post("/api/tg/topup/create")
async def tg_topup_create(request: Request):
    """Create a top-up payment directly from the Telegram account WebApp."""
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    tg_user = _verify_telegram_webapp_init_data(init_data)

    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}

    user_id = 0
    if tg_user and tg_user.get("id"):
        try:
            user_id = int(tg_user.get("id") or 0)
        except Exception:
            user_id = 0

    # Fallback for local browser testing only. In Telegram initData is used.
    if user_id <= 0:
        for value in (
            payload.get("uid"),
            payload.get("tg_user_id"),
            payload.get("user_id"),
            request.query_params.get("uid"),
            request.query_params.get("tg_user_id"),
            request.query_params.get("user_id"),
        ):
            try:
                user_id = int(value or 0)
            except Exception:
                user_id = 0
            if user_id > 0:
                break

    if user_id <= 0:
        return {
            "ok": False,
            "error": "user_id_required",
            "message": "Откройте личный кабинет из Telegram-бота.",
        }

    try:
        tokens = int(payload.get("tokens") or 0)
    except Exception:
        tokens = 0

    pack = _find_pack_by_tokens(tokens)
    if not pack:
        return {
            "ok": False,
            "error": "pack_not_found",
            "message": "Пакет пополнения не найден.",
        }

    amount_rub = int(pack.get("rub") or 0)
    stars = int(pack.get("stars") or 0)
    title = f"Пополнение баланса: {tokens} токенов"

    # Main path: YooKassa card/SBP payment. It requires receipt email.
    if _yookassa_enabled():
        email = str(payload.get("email") or "").strip().lower()
        stored_email = ""
        try:
            stored_email = sb_get_user_email(user_id)
        except Exception:
            stored_email = ""

        if email:
            if not _EMAIL_RE.match(email):
                return {
                    "ok": False,
                    "need_email": True,
                    "message": "Введите корректный email для чека.",
                }
            # Payment can still be created if Supabase temporarily fails; storing email is a convenience.
            try:
                sb_set_user_email(user_id, email)
            except Exception:
                pass
            stored_email = email

        if not stored_email:
            return {
                "ok": False,
                "need_email": True,
                "message": "Для оплаты картой или СБП нужен email для чека.",
            }

        return_url = str(payload.get("return_url") or WEBAPP_ACCOUNT_URL or "https://t.me").strip()
        if not (return_url.startswith("https://") or return_url.startswith("http://")):
            return_url = WEBAPP_ACCOUNT_URL or "https://t.me"

        try:
            payment_id, url = await create_yookassa_payment(
                amount_rub=amount_rub,
                description=title,
                user_id=user_id,
                tokens=tokens,
                customer_email=stored_email,
                return_url=return_url,
            )
        except Exception as exc:
            return {
                "ok": False,
                "error": "payment_create_failed",
                "message": f"Не удалось создать платёж: {exc}",
            }

        return {
            "ok": True,
            "method": "yookassa",
            "payment_id": payment_id,
            "confirmation_url": url,
            "tokens": tokens,
            "amount_rub": amount_rub,
            "customer_email": stored_email,
        }

    # Fallback: Telegram Stars invoice is sent into the bot chat.
    try:
        payload_str = f"stars_topup:{tokens}:{user_id}"
        await tg_send_stars_invoice(
            user_id,
            title,
            f"{tokens} токенов • {stars}⭐ (≈{amount_rub}₽)",
            payload_str,
            stars,
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": "stars_invoice_failed",
            "message": f"Не удалось отправить инвойс Stars: {exc}",
        }

    return {
        "ok": True,
        "method": "telegram_stars",
        "sent_to_bot": True,
        "tokens": tokens,
        "amount_rub": amount_rub,
        "stars": stars,
        "message": "Инвойс Telegram Stars отправлен в чат с ботом.",
    }


SORA_QUEUE_NAME = os.getenv("SORA_QUEUE_NAME", "sora").strip() or "sora"
TOPAZ_PHOTO_QUEUE_NAME = os.getenv("TOPAZ_PHOTO_QUEUE_NAME", "topaz_photo").strip() or "topaz_photo"
GPT_IMAGE2_QUEUE_NAME = os.getenv("GPT_IMAGE2_QUEUE_NAME", "gpt_image2").strip() or "gpt_image2"
GPT_IMAGE2_GENERATION_COST = int(os.getenv("GPT_IMAGE2_GENERATION_COST", "1") or "1")
WORKSPACE_IMAGE_QUEUE_NAME = (os.getenv("WORKSPACE_IMAGE_QUEUE_NAME", "workspace_image") or "workspace_image").strip() or "workspace_image"
MIDJOURNEY_TG_QUEUE_NAME = (os.getenv("MIDJOURNEY_TG_QUEUE_NAME", "telegram_midjourney") or "telegram_midjourney").strip() or "telegram_midjourney"
SEEDREAM_T2I_QUEUE_NAME = os.getenv("SEEDREAM_T2I_QUEUE_NAME", "seedream_t2i").strip() or "seedream_t2i"
TG_TTS_QUEUE_NAME = (os.getenv("TG_TTS_QUEUE_NAME", "workspace_tg_tts") or "workspace_tg_tts").strip() or "workspace_tg_tts"
PUBLIC_SUBSCRIPTION_PLAN_CODES = {"spark", "pulse", "nexus"}
SEEDREAM_T2I_INCLUDED_PLAN_CODES = {"spark", "pulse", "nexus"}
NANO_BANANA_BASIC_INCLUDED_PLAN_CODES = {"pulse", "nexus"}
MIDJOURNEY_INCLUDED_PLAN_CODES = {"pulse", "nexus"}
GPT_IMAGE_2_INCLUDED_PLAN_CODES = {"nexus"}
VEO31_FAST_RELAX_INCLUDED_PLAN_CODES = {"nexus"}
MIDJOURNEY_INCLUDED_MODELS = {"midjourney-v7", "midjourney-v8.1"}
NANO_BANANA_QUEUE_NAME = os.getenv("NANO_BANANA_QUEUE_NAME", "nano_banana").strip() or "nano_banana"
TOPAZ_VIDEO_QUEUE_NAME = os.getenv("TOPAZ_VIDEO_QUEUE_NAME", "topaz_video").strip() or "topaz_video"
# --- YooKassa (cards/SBP) ---
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "").strip()
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "").strip()
# Return URL after payment on YooKassa hosted page (can be any page; Telegram will still show the success in browser)
YOOKASSA_RETURN_URL = os.getenv("YOOKASSA_RETURN_URL", WEBAPP_MUSIC_URL).strip()
# Optional: explicit webhook URL (if empty, you can set it in YooKassa cabinet; if set, it will be passed to create-payment)
YOOKASSA_WEBHOOK_URL = os.getenv("YOOKASSA_WEBHOOK_URL", "").strip()
# Optional webhook secret. Phase-1 safe mode: the secret is NOT enforced unless
# YOOKASSA_WEBHOOK_REQUIRE_SECRET=1, so existing YooKassa cabinet URL keeps working.
YOOKASSA_WEBHOOK_SECRET = (
    os.getenv("YOOKASSA_WEBHOOK_TOKEN", "").strip()
    or os.getenv("YOOKASSA_WEBHOOK_SECRET", "").strip()
)
YOOKASSA_WEBHOOK_REQUIRE_SECRET = os.getenv("YOOKASSA_WEBHOOK_REQUIRE_SECRET", "").strip().lower() in ("1", "true", "yes", "y", "on")

def _yookassa_enabled() -> bool:
    return bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY)
PIAPI_API_KEY = os.getenv("PIAPI_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_CHAT_MODEL = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
OPENAI_TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe").strip() or "gpt-4o-mini-transcribe"
AI_CHAT_VOICE_ENABLED = os.getenv("AI_CHAT_VOICE_ENABLED", "true").strip().lower() in ("1", "true", "yes", "y", "on")
try:
    AI_CHAT_VOICE_MAX_SECONDS = int(os.getenv("AI_CHAT_VOICE_MAX_SECONDS", "60") or 60)
except Exception:
    AI_CHAT_VOICE_MAX_SECONDS = 60
try:
    AI_CHAT_VOICE_MAX_BYTES = int(os.getenv("AI_CHAT_VOICE_MAX_BYTES", str(20 * 1024 * 1024)) or (20 * 1024 * 1024))
except Exception:
    AI_CHAT_VOICE_MAX_BYTES = 20 * 1024 * 1024
try:
    SEEDANCE_AUDIO_MAX_UPLOAD_MB = max(1, int(os.getenv("SEEDANCE_AUDIO_MAX_UPLOAD_MB", "30") or "30"))
except Exception:
    SEEDANCE_AUDIO_MAX_UPLOAD_MB = 30
SEEDANCE_AUDIO_MAX_UPLOAD_BYTES = SEEDANCE_AUDIO_MAX_UPLOAD_MB * 1024 * 1024
AI_CHAT_VOICE_LANGUAGE = os.getenv("AI_CHAT_VOICE_LANGUAGE", "ru").strip()
PROMPT_BUILDER_MODEL = os.getenv("PROMPT_BUILDER_MODEL", "gpt-5.4").strip() or "gpt-5.4"
PROMPT_BUILDER_MAX_IMAGES = int(os.getenv("PROMPT_BUILDER_MAX_IMAGES", "9") or 9)
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change_me")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "").strip()

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


# ---- BytePlus / ModelArk (Seedream) ----
ARK_API_KEY = os.getenv("ARK_API_KEY", "").strip()
ARK_BASE_URL = os.getenv("ARK_BASE_URL", "https://ark.ap-southeast.bytepluses.com/api/v3").rstrip("/")
ARK_IMAGE_MODEL = os.getenv("ARK_IMAGE_MODEL", "").strip()  # endpoint id: ep-...
ARK_IMAGE_MODEL_SEEDREAM_45 = (
    os.getenv("ARK_IMAGE_MODEL_SEEDREAM_45", "").strip()
    or os.getenv("SEEDREAM_45_MODEL_ID", "").strip()
)
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
#  💰 5 токенов — 33⭐ (≈ 60 ₽)
#  ⭐ 20 токенов — 110⭐ (≈ 200 ₽)
#  🚀 60 токенов — 302⭐ (≈ 550 ₽)
#  👑 100 токенов — 489⭐ (≈ 890 ₽)
#  💎 200 токенов — 934⭐ (≈ 1700 ₽)
#
# Примечание:
# ⭐ Stars — временный способ оплаты
# ₽ — расчётная стоимость в рублях
# Токены — внутренняя единица сервиса
TOPUP_PACKS = [
    {"tokens": 5, "rub": 60, "stars": 33, "badge": "💰"},
    {"tokens": 20, "rub": 200, "stars": 110, "badge": "⭐"},
    {"tokens": 60, "rub": 550, "stars": 302, "badge": "🚀"},
    {"tokens": 100, "rub": 890, "stars": 489, "badge": "👑"},
    {"tokens": 200, "rub": 1700, "stars": 934, "badge": "💎"},
]

# Admin-only Stars invoice.
# Нужен только для того, чтобы админ мог купить Stars-инвойс у своего бота
# и одновременно получить +200 внутренних токенов. Обычные тарифы не трогаем.
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or default)
    except Exception:
        return int(default)

ADMIN_STARS_200_TOKENS = _env_int("ADMIN_STARS_200_TOKENS", 200)
ADMIN_STARS_200_AMOUNT = _env_int("ADMIN_STARS_200_AMOUNT", 934)
ADMIN_STARS_200_BUTTON_TEXT = "⭐ Админ Stars 200"
ADMIN_STARS_200_REASON = "telegram_stars_admin_200"
ADMIN_STARS_200_PROVIDER = "telegram_stars_admin"

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
    return {"inline_keyboard": [[{"text": "➕ Пополнить ", "callback_data": "topup:menu"}]]}

def _topup_packs_kb() -> dict:
    # 2 кнопки в ряд
    btns = []
    for p in TOPUP_PACKS:
        tokens = int(p["tokens"])
        rub = int(p["rub"])
        badge = str(p.get("badge") or "").strip()
        title = str(p.get("title") or "").strip()
        prefix = f"{badge} " if badge else ""
        suffix = f" • {title}" if title else ""
        btns.append({
            "text": f"{prefix}{rub}₽ • {tokens} токенов{suffix}",
            "callback_data": f"topup:pack:{tokens}"
        })

    def chunk(items, n=2):
        return [items[i:i+n] for i in range(0, len(items), n)]

    kb = [
        [{"text": "Выбери пакет токенов:", "callback_data": "noop"}],
    ]
    kb += chunk(btns, 2)
    return {"inline_keyboard": kb}

def _nano_banana_pro_aspect_inline_kb(current: str = "9:16") -> dict:
    values = ("1:1", "4:5", "9:16", "16:9")
    row = []
    for value in values:
        text = f"✅ {value}" if value == current else value
        row.append({"text": text, "callback_data": f"nbp:aspect:{value}"})
    return {"inline_keyboard": [row]}


def _nano_banana_pro_new_inline_kb(current_aspect: str = "9:16", current_resolution: str = "2K", refs_count: int = 0) -> dict:
    res_values = (("2K", "1 ток."), ("4K", "2 ток."))
    res_row = []
    for value, price in res_values:
        label = f"{value} • {price}"
        text = f"✅ {label}" if value == current_resolution else label
        res_row.append({"text": text, "callback_data": f"nbpn:res:{value}"})

    aspect_values = ("1:1", "4:5", "9:16", "16:9")
    aspect_row = []
    for value in aspect_values:
        text = f"✅ {value}" if value == current_aspect else value
        aspect_row.append({"text": text, "callback_data": f"nbpn:aspect:{value}"})

    action_row = [{"text": f"✅ Готово ({refs_count}/8)" if refs_count else "✅ Готово", "callback_data": "nbpn:done"}]
    if refs_count:
        action_row.append({"text": "🗑 Очистить фото", "callback_data": "nbpn:clear"})

    return {"inline_keyboard": [res_row, aspect_row, action_row]}


def _nano_banana_2_aspect_inline_kb(current: str = "9:16") -> dict:
    values = ("1:1", "4:5", "9:16", "16:9")
    row = []
    for value in values:
        text = f"✅ {value}" if value == current else value
        row.append({"text": text, "callback_data": f"nb2:aspect:{value}"})
    return {"inline_keyboard": [row]}


def _seedream_aspect_inline_kb(mode_key: str, current: str = "9:16") -> dict:
    values = ("1:1", "4:5", "9:16", "16:9")
    row = []
    for value in values:
        text = f"✅ {value}" if value == current else value
        row.append({"text": text, "callback_data": f"sd45:{mode_key}:aspect:{value}"})
    return {"inline_keyboard": [row]}


def _gpt_image_2_size_for_aspect_ratio(aspect_ratio: str) -> str:
    ratio = str(aspect_ratio or "").strip() or "1:1"
    mapping = {
        "1:1": "1024x1024",
        "4:5": "1024x1280",
        "9:16": "864x1536",
        "16:9": "1536x864",
    }
    return mapping.get(ratio, "1024x1024")


def _gpt_image_2_aspect_for_size(size: str) -> str:
    normalized = str(size or "").strip().lower()
    mapping = {
        "1024x1024": "1:1",
        "1024x1280": "4:5",
        "864x1536": "9:16",
        "1536x864": "16:9",
        # Backward compatibility with earlier standard portrait/landscape sizes.
        "1024x1536": "9:16",
        "1536x1024": "16:9",
    }
    return mapping.get(normalized, "1:1")


def _gpt_image_2_aspect_inline_kb(mode_key: str, current: str = "1:1") -> dict:
    values = ("1:1", "4:5", "9:16", "16:9")
    row = []
    for value in values:
        text = f"✅ {value}" if value == current else value
        row.append({"text": text, "callback_data": f"gi2:{mode_key}:aspect:{value}"})
    return {"inline_keyboard": [row]}


def _legacy_gpt_image_2_aspect_to_kie(value: str = "16:9") -> str:
    raw = str(value or "").strip()
    # Official GPT Image 2.0 exposed 4:5, while KIE GPT Image 2 exposes 3:4.
    # Keep old cached Telegram buttons/states usable, but route them to the KIE provider.
    if raw == "4:5":
        return "3:4"
    return raw if raw in {"1:1", "9:16", "16:9", "4:3", "3:4", "21:9"} else "16:9"


def _gpt_image_2_kie_resolution(value: str = "2K") -> str:
    return normalize_gpt_image_2_kie_resolution(value, default="2K")


def _gpt_image_2_kie_aspect(value: str = "16:9") -> str:
    return normalize_gpt_image_2_kie_aspect_ratio(value, default="16:9")


def _gpt_image_2_kie_options(current_resolution: str = "2K", current_aspect: str = "16:9") -> tuple[str, str]:
    return normalize_gpt_image_2_kie_options(current_resolution, current_aspect, default_resolution="2K", default_aspect="16:9")


def _gpt_image_2_kie_inline_kb(mode_key: str, current_aspect: str = "16:9", current_resolution: str = "2K", refs_count: int = 0) -> dict:
    current_resolution, current_aspect = _gpt_image_2_kie_options(current_resolution, current_aspect)
    res_values = (("2K", "1 ток."), ("4K", "2 ток."))
    res_row = []
    for value, price in res_values:
        safe_resolution, safe_aspect = _gpt_image_2_kie_options(value, current_aspect)
        label = f"{value} • {price}"
        text = f"✅ {label}" if safe_resolution == current_resolution else label
        res_row.append({"text": text, "callback_data": f"gi2k:{mode_key}:res:{value}"})

    aspect_values = ("16:9", "21:9", "9:16", "1:1", "4:3", "3:4")
    aspect_row = []
    rows = [res_row]
    for index, value in enumerate(aspect_values):
        text = f"✅ {value}" if value == current_aspect else value
        aspect_row.append({"text": text, "callback_data": f"gi2k:{mode_key}:aspect:{value}"})
        if len(aspect_row) == 4 or index == len(aspect_values) - 1:
            rows.append(aspect_row)
            aspect_row = []

    if mode_key == "i2i":
        action_row = [{"text": f"✅ Готово ({refs_count}/16)" if refs_count else "✅ Готово", "callback_data": "gi2k:done"}]
        if refs_count:
            action_row.append({"text": "🗑 Очистить фото", "callback_data": "gi2k:clear"})
        rows.append(action_row)
    return {"inline_keyboard": rows}


def _midjourney_model_title(model: str) -> str:
    return "Midjourney V8.1" if normalize_midjourney_model(model) == "midjourney-v8.1" else "Midjourney V7"


def _midjourney_cost(model: str = "midjourney-v7", speed_mode: str = "fast") -> int:
    model_key = normalize_midjourney_model(model)
    speed = normalize_midjourney_speed_mode(speed_mode, model=model_key)
    if model_key == "midjourney-v8.1":
        return 1
    return 2 if speed == "turbo" else 1


def _midjourney_default_state(model: str = "midjourney-v7") -> dict:
    model_key = normalize_midjourney_model(model)
    return {
        "step": "need_prompt",
        "model": model_key,
        "prompt": "",
        "aspect_ratio": "1:1",
        "speed_mode": "fast",
        "stylize": 100,
        "chaos": 0,
        "raw": False,
        "style_ref_url": "",
        "style_ref_file_id": "",
        "omni_ref_url": "",
        "omni_ref_file_id": "",
        "image_prompt_urls": [],
        "image_prompt_file_ids": [],
    }


def _midjourney_state(st: Dict[str, Any], model: str = "midjourney-v7") -> dict:
    mj = st.get("midjourney")
    if not isinstance(mj, dict):
        mj = _midjourney_default_state(model)
    mj["model"] = normalize_midjourney_model(mj.get("model") or model)
    mj["speed_mode"] = normalize_midjourney_speed_mode(mj.get("speed_mode") or "fast", model=mj["model"])
    try:
        mj["stylize"] = max(0, min(1000, int(mj.get("stylize") if mj.get("stylize") is not None else 100)))
    except Exception:
        mj["stylize"] = 100
    try:
        mj["chaos"] = max(0, min(100, int(mj.get("chaos") if mj.get("chaos") is not None else 0)))
    except Exception:
        mj["chaos"] = 0
    if str(mj.get("aspect_ratio") or "").strip() not in {"1:1", "16:9", "9:16", "4:5"}:
        mj["aspect_ratio"] = "1:1"
    if mj["model"] == "midjourney-v8.1":
        mj["speed_mode"] = "fast"
        mj["omni_ref_url"] = ""
        mj["omni_ref_file_id"] = ""
    return mj


def _midjourney_settings_text(mj: dict, user_id: Optional[int] = None) -> str:
    model = normalize_midjourney_model(mj.get("model") or "midjourney-v7")
    speed = normalize_midjourney_speed_mode(mj.get("speed_mode") or "fast", model=model)
    prompt = str(mj.get("prompt") or "").strip()
    style_loaded = "загружен" if str(mj.get("style_ref_url") or "").strip() else "не загружен"
    omni_loaded = "загружен" if str(mj.get("omni_ref_url") or "").strip() else "не загружен"
    image_ref_count = len([x for x in (mj.get("image_prompt_urls") or []) if str(x or "").strip()])
    price = _midjourney_user_cost(user_id, model, speed)
    lines = [
        _midjourney_model_title(model),
        "",
        f"Формат: {mj.get('aspect_ratio') or '1:1'}",
        f"Скорость: {speed.title()}",
        f"Stylize: {int(mj.get('stylize') or 100)}",
        f"Chaos: {int(mj.get('chaos') or 0)}",
        f"Raw: {'ON' if bool(mj.get('raw')) else 'OFF'}",
        f"Style ref: {style_loaded}",
    ]
    if model != "midjourney-v8.1":
        lines.append(f"Omni ref: {omni_loaded}")
    else:
        lines.append(f"Image refs: {image_ref_count}/4")
    lines += [
        f"Prompt: {'задан' if prompt else 'не задан'}",
        "",
        f"Цена: {price} токен" if price == 1 else f"Цена: {price} токена",
        "",
        "Отправь prompt или измени параметры ниже.",
    ]
    return "\n".join(lines)


def _midjourney_settings_kb(mj: dict, user_id: Optional[int] = None) -> dict:
    model = normalize_midjourney_model(mj.get("model") or "midjourney-v7")
    speed = normalize_midjourney_speed_mode(mj.get("speed_mode") or "fast", model=model)
    rows = [
        [
            {"text": f"Формат: {mj.get('aspect_ratio') or '1:1'}", "callback_data": "mj:menu:aspect"},
            {"text": f"Скорость: {speed.title()}", "callback_data": "mj:menu:speed"},
        ],
        [
            {"text": f"Stylize: {int(mj.get('stylize') or 100)}", "callback_data": "mj:menu:stylize"},
            {"text": f"Chaos: {int(mj.get('chaos') or 0)}", "callback_data": "mj:menu:chaos"},
        ],
        [{"text": f"Raw: {'ON' if bool(mj.get('raw')) else 'OFF'}", "callback_data": "mj:raw"}],
        [
            {"text": "Style ref", "callback_data": "mj:ref:style"},
            {"text": "Omni ref", "callback_data": "mj:ref:omni"} if model != "midjourney-v8.1" else {"text": "Image ref", "callback_data": "mj:ref:image"},
        ],
    ]
    if str(mj.get("prompt") or "").strip():
        rows.append([{"text": "✏️ Изменить prompt", "callback_data": "mj:prompt:edit"}])
        run_cost = _midjourney_user_cost(user_id, model, speed)
        rows.append([{"text": f"✅ Запустить • {run_cost} ток.", "callback_data": "mj:run"}])
    rows.append([{"text": "⬅️ Назад", "callback_data": "mj:back:photo"}])
    return {"inline_keyboard": rows}


def _midjourney_model_kb() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "Midjourney V7", "callback_data": "mj:model:v7"}],
            [{"text": "Midjourney V8.1", "callback_data": "mj:model:v81"}],
            [{"text": "⬅️ Назад", "callback_data": "mj:back:photo"}],
        ]
    }


def _midjourney_aspect_kb(current: str = "1:1") -> dict:
    values = ("1:1", "16:9", "9:16", "4:5")
    return {"inline_keyboard": [[{"text": f"✅ {v}" if v == current else v, "callback_data": f"mj:ar:{v}"} for v in values], [{"text": "⬅️ Назад", "callback_data": "mj:settings"}]]}


def _midjourney_speed_kb(model: str, current: str = "fast") -> dict:
    model_key = normalize_midjourney_model(model)
    if model_key == "midjourney-v8.1":
        return {"inline_keyboard": [[{"text": "✅ Fast • 1 токен", "callback_data": "mj:speed:fast"}], [{"text": "⬅️ Назад", "callback_data": "mj:settings"}]]}
    speed = normalize_midjourney_speed_mode(current, model=model_key)
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Fast • 1 токен" if speed == "fast" else "Fast • 1 токен", "callback_data": "mj:speed:fast"},
                {"text": "✅ Turbo • 2 токена" if speed == "turbo" else "Turbo • 2 токена", "callback_data": "mj:speed:turbo"},
            ],
            [{"text": "⬅️ Назад", "callback_data": "mj:settings"}],
        ]
    }


def _midjourney_value_kb(kind: str, current: int) -> dict:
    if kind == "stylize":
        values = (0, 100, 250, 500, 750, 1000)
    else:
        values = (0, 10, 25, 50, 75, 100)
    rows = []
    row = []
    for idx, value in enumerate(values, start=1):
        row.append({"text": f"✅ {value}" if int(current) == value else str(value), "callback_data": f"mj:{kind}:{value}"})
        if len(row) == 3 or idx == len(values):
            rows.append(row)
            row = []
    rows.append([{"text": "✏️ Ввести своё", "callback_data": f"mj:{kind}:custom"}])
    rows.append([{"text": "⬅️ Назад", "callback_data": "mj:settings"}])
    return {"inline_keyboard": rows}


def _midjourney_ref_kb(kind: str, loaded: bool = False) -> dict:
    rows = []
    if loaded:
        rows.append([{"text": "🗑 Убрать reference", "callback_data": f"mj:ref_clear:{kind}"}])
    rows.append([{"text": "⬅️ Назад", "callback_data": "mj:settings"}])
    return {"inline_keyboard": rows}


def _midjourney_session_token() -> str:
    return base64.urlsafe_b64encode(os.urandom(9)).decode("ascii").rstrip("=")


def _midjourney_result_caption(session: dict, active_index: Optional[int] = None, *, grid: bool = False) -> str:
    model_title = _midjourney_model_title(str(session.get("model") or "midjourney-v7"))
    aspect = str(session.get("aspect_ratio") or "1:1")
    speed = str(session.get("speed_mode") or "fast").title()
    if grid:
        return f"{model_title}\n\nГотово: 4 варианта\nФормат: {aspect}\nСкорость: {speed}\nВыбери изображение для просмотра или remix."
    idx = int(active_index or 0) + 1
    return f"{model_title}\nИзображение {idx} из 4\nФормат: {aspect}\nСкорость: {speed}"


def _midjourney_grid_result_kb(token: str) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "1️⃣ Открыть #1", "callback_data": f"mjr:{token}:open:0"},
                {"text": "2️⃣ Открыть #2", "callback_data": f"mjr:{token}:open:1"},
            ],
            [
                {"text": "3️⃣ Открыть #3", "callback_data": f"mjr:{token}:open:2"},
                {"text": "4️⃣ Открыть #4", "callback_data": f"mjr:{token}:open:3"},
            ],
            [
                {"text": "🔄 Reroll все 4", "callback_data": f"mjr:{token}:reroll"},
                {"text": "📄 Prompt", "callback_data": f"mjr:{token}:prompt"},
            ],
            [{"text": "⬇️ Скачать все", "callback_data": f"mjr:{token}:download_all"}],
            [{"text": "🆕 Новый prompt", "callback_data": f"mjr:{token}:new"}],
        ]
    }


def _midjourney_image_result_kb(token: str, index: int) -> dict:
    safe_index = max(0, min(3, int(index or 0)))
    prev_index = (safe_index - 1) % 4
    next_index = (safe_index + 1) % 4
    number_row = []
    for i in range(4):
        number_row.append({"text": f"✅ {i + 1}" if i == safe_index else str(i + 1), "callback_data": f"mjr:{token}:open:{i}"})
    return {
        "inline_keyboard": [
            [
                {"text": "◀️", "callback_data": f"mjr:{token}:open:{prev_index}"},
                {"text": f"{safe_index + 1}/4", "callback_data": "noop"},
                {"text": "▶️", "callback_data": f"mjr:{token}:open:{next_index}"},
            ],
            number_row,
            [
                {"text": "✏️ Remix тонкий", "callback_data": f"mjr:{token}:vary:{safe_index}:subtle"},
                {"text": "🎨 Remix креативный", "callback_data": f"mjr:{token}:vary:{safe_index}:strong"},
            ],
            [{"text": "⬇️ Скачать оригинал", "callback_data": f"mjr:{token}:download:{safe_index}"}],
            [{"text": "🧩 Вернуться к сетке", "callback_data": f"mjr:{token}:grid"}],
            [
                {"text": "🔄 Reroll все 4", "callback_data": f"mjr:{token}:reroll"},
                {"text": "📄 Prompt", "callback_data": f"mjr:{token}:prompt"},
            ],
            [{"text": "🆕 Новый prompt", "callback_data": f"mjr:{token}:new"}],
        ]
    }


def _midjourney_reroll_confirm_kb(token: str) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "✅ Да, перегенерировать", "callback_data": f"mjr:{token}:reroll_yes"}],
            [{"text": "❌ Отмена", "callback_data": f"mjr:{token}:reroll_no"}],
        ]
    }


def _midjourney_submit_reply_markup(ok: bool, message_text: str, mj: Optional[dict] = None, user_id: Optional[int] = None) -> Optional[dict]:
    if ok:
        return _photo_future_menu_keyboard()
    if "Недостаточно токенов" in str(message_text or ""):
        return _topup_packs_kb()
    if isinstance(mj, dict):
        return _midjourney_settings_kb(mj, user_id=user_id)
    return None


def _midjourney_dedupe_key(*parts: Any) -> str:
    raw = "|".join(str(part or "") for part in parts)
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:32]


def _midjourney_mark_action_once(holder: dict, key: str, *, ttl_sec: int = 60) -> bool:
    if not isinstance(holder, dict):
        return True
    now = time.time()
    pending = holder.get("pending_actions")
    if not isinstance(pending, dict):
        pending = {}
        holder["pending_actions"] = pending
    for old_key, old_ts in list(pending.items()):
        try:
            if now - float(old_ts or 0) > ttl_sec:
                pending.pop(old_key, None)
        except Exception:
            pending.pop(old_key, None)
    if key in pending:
        return False
    pending[key] = now
    return True


def _midjourney_clear_action_mark(holder: dict, key: str) -> None:
    if not isinstance(holder, dict):
        return
    pending = holder.get("pending_actions")
    if isinstance(pending, dict):
        pending.pop(key, None)


def _midjourney_format_prompt_details(session: dict) -> str:
    model_title = _midjourney_model_title(str(session.get("model") or "midjourney-v7"))
    action = str(session.get("action") or "generate").strip().lower() or "generate"
    prompt = str(session.get("prompt") or "").strip()
    run_prompt = str(session.get("run_prompt") or "").strip()
    lines = [
        "📄 Midjourney prompt",
        "",
        f"Модель: {model_title}",
        f"Действие: {action}",
        f"Формат: {str(session.get('aspect_ratio') or '1:1')}",
        f"Скорость: {str(session.get('speed_mode') or 'fast').title()}",
    ]
    if action == "variation":
        try:
            lines.append(f"Remix image: #{int(session.get('selected_image_no') or 0) + 1}")
        except Exception:
            pass
        lines.append(f"Remix type: {str(session.get('variation_type') or 'subtle')}")
    lines += ["", "Исходный prompt:", prompt or "—"]
    if run_prompt and run_prompt != prompt:
        lines += ["", "Prompt, отправленный провайдеру:", run_prompt]
    return "\n".join(lines).strip()


async def _midjourney_send_prompt_details(chat_id: int, session: dict) -> None:
    text = _midjourney_format_prompt_details(session)
    if len(text) <= 3500:
        await tg_send_message(chat_id, text)
        return
    await tg_send_document_bytes(
        chat_id,
        text.encode("utf-8"),
        filename="midjourney_prompt.txt",
        mime="text/plain; charset=utf-8",
        caption="📄 Midjourney prompt и параметры",
    )


def _midjourney_is_heic_like(image_bytes: bytes, filename: str = "", content_type: str = "") -> bool:
    name = str(filename or "").strip().lower()
    mime = str(content_type or "").strip().lower()
    if name.endswith((".heic", ".heif")) or mime in {"image/heic", "image/heif", "image/heic-sequence", "image/heif-sequence"}:
        return True
    head = bytes(image_bytes[:32] if image_bytes else b"")
    return b"ftypheic" in head or b"ftypheix" in head or b"ftyphevc" in head or b"ftyphevx" in head or b"ftypmif1" in head


def _midjourney_get_session(chat_id: int, user_id: int, token: str) -> Optional[dict]:
    st = _ensure_state(chat_id, user_id)
    sessions = st.get("midjourney_sessions")
    if not isinstance(sessions, dict):
        return None
    session = sessions.get(str(token or "").strip())
    return session if isinstance(session, dict) else None


def _midjourney_session_storage_path(user_id: int, token: str) -> str:
    safe_token = re.sub(r"[^A-Za-z0-9_-]", "", str(token or "").strip())
    return f"midjourney_tg_sessions/{int(user_id)}/{safe_token}.json"


def _midjourney_session_public_url(user_id: int, token: str) -> str:
    if sb is None or not SUPABASE_STORAGE_BUCKET:
        return ""
    try:
        return str(sb.storage.from_(SUPABASE_STORAGE_BUCKET).get_public_url(_midjourney_session_storage_path(user_id, token)) or "").strip()
    except Exception:
        return ""


def _midjourney_cache_session(chat_id: int, user_id: int, token: str, session: dict, *, persist: bool = False) -> None:
    st = _ensure_state(chat_id, user_id)
    sessions = st.setdefault("midjourney_sessions", {})
    if not isinstance(sessions, dict):
        sessions = {}
        st["midjourney_sessions"] = sessions
    sessions[str(token or "").strip()] = session
    st["ts"] = _now()
    if persist:
        try:
            payload = json.dumps(session, ensure_ascii=False).encode("utf-8")
            upload_bytes_to_supabase(_midjourney_session_storage_path(user_id, token), payload, "application/json")
        except Exception:
            logging.exception("Midjourney session storage persist failed")


async def _midjourney_load_session_from_storage(chat_id: int, user_id: int, token: str) -> Optional[dict]:
    url = _midjourney_session_public_url(user_id, token)
    if not url:
        return None
    try:
        payload = await http_download_bytes(url, timeout=30)
        data = json.loads(payload.decode("utf-8"))
        if not isinstance(data, dict):
            return None
        data["ts"] = _now()
        _midjourney_cache_session(chat_id, user_id, token, data, persist=False)
        return data
    except Exception:
        logging.exception("Midjourney session storage load failed")
        return None


def _midjourney_prepare_run_prompt(mj: dict) -> str:
    model = normalize_midjourney_model(mj.get("model") or "midjourney-v7")
    style_ref_url = str(mj.get("style_ref_url") or "").strip()
    omni_ref_url = str(mj.get("omni_ref_url") or "").strip()
    image_prompt_urls = [str(x or "").strip() for x in (mj.get("image_prompt_urls") or []) if str(x or "").strip()]
    return build_midjourney_v7_prompt(
        prompt=str(mj.get("prompt") or "").strip(),
        model=model,
        aspect_ratio=str(mj.get("aspect_ratio") or "1:1"),
        stylize=mj.get("stylize") if mj.get("stylize") is not None else 100,
        chaos=mj.get("chaos") if mj.get("chaos") is not None else 0,
        raw_mode=bool(mj.get("raw")),
        speed_mode=str(mj.get("speed_mode") or "fast"),
        style_ref_urls=[style_ref_url] if style_ref_url else [],
        omni_ref_url=omni_ref_url if model != "midjourney-v8.1" else "",
        image_prompt_urls=image_prompt_urls[:4] if model == "midjourney-v8.1" else [],
        image_weight=mj.get("image_weight") if mj.get("image_weight") is not None else 1,
    )


def _midjourney_upload_reference_bytes(
    *,
    user_id: int,
    image_bytes: bytes,
    filename: str = "reference.jpg",
    content_type: str = "image/jpeg",
) -> str:
    """Upload Telegram reference bytes to public storage before giving the URL to Midjourney.
    Never pass Telegram file URLs to external providers because they contain the bot token.
    """
    if not image_bytes:
        raise RuntimeError("Пустой reference image")
    if _midjourney_is_heic_like(image_bytes, filename=filename, content_type=content_type):
        raise RuntimeError("HEIC/HEIF пока не поддерживается для Midjourney reference. Отправь JPG, PNG или WEBP.")
    try:
        ext, detected_mime = _detect_image_type(image_bytes)
    except Exception:
        ext, detected_mime = "jpg", "image/jpeg"
    raw_name = str(filename or "reference.jpg").strip().lower()
    if raw_name.endswith(".png"):
        ext = "png"
        detected_mime = "image/png"
    elif raw_name.endswith(".webp"):
        ext = "webp"
        detected_mime = "image/webp"
    elif raw_name.endswith((".jpg", ".jpeg")):
        ext = "jpg"
        detected_mime = "image/jpeg"
    mime = detected_mime or content_type or "image/jpeg"
    path = f"midjourney_tg_refs/{int(user_id)}/{int(time.time())}_{uuid4().hex[:12]}.{ext or 'jpg'}"
    url = upload_bytes_to_supabase(path, image_bytes, mime)
    clean_url = str(url or "").strip()
    if not clean_url:
        raise RuntimeError("Не удалось получить public URL для reference image")
    return clean_url


async def _midjourney_charge_and_enqueue(
    *,
    chat_id: int,
    user_id: int,
    action: str,
    model: str,
    prompt: str,
    run_prompt: str,
    aspect_ratio: str,
    speed_mode: str,
    settings: dict,
    source_task_id: str = "",
    selected_image_no: int = 0,
    variation_type: str = "subtle",
) -> Tuple[bool, str]:
    action_name = str(action or "generate").strip().lower() or "generate"
    model_key = normalize_midjourney_model(model)
    speed = normalize_midjourney_speed_mode(speed_mode, model=model_key)
    cost_tokens = int(_midjourney_user_cost(user_id, model_key, speed))
    if action_name == "generate":
        prompt_len = len(str(run_prompt or ""))
        if prompt_len <= 0:
            return False, "❌ Prompt для Midjourney пустой."
        if prompt_len > 8192:
            return False, f"❌ Prompt слишком длинный для Midjourney: {prompt_len}/8192 символов. Уменьши текст или убери часть reference-параметров."
    if action_name == "variation":
        try:
            selected = int(selected_image_no)
        except Exception:
            selected = -1
        if selected < 0 or selected > 3:
            return False, "❌ Remix может запускаться только для изображения 1–4."
    try:
        ensure_user_row(user_id)
        bal = int(get_balance(user_id) or 0)
    except Exception:
        bal = 0
    if bal < cost_tokens:
        return False, f"❌ Недостаточно токенов для Midjourney. Нужно: {cost_tokens}, баланс: {bal}"

    reason = "midjourney_generate"
    if action_name == "reroll":
        reason = "midjourney_reroll"
    elif action_name == "variation":
        reason = "midjourney_variation"

    charge_ref_id = uuid4().hex if cost_tokens > 0 else ""
    charged = False
    try:
        if cost_tokens > 0:
            add_tokens(
                user_id,
                -cost_tokens,
                reason=reason,
                ref_id=charge_ref_id,
                meta={
                    "provider": "midjourney",
                    "action": action_name,
                    "model": model_key,
                    "speed_mode": speed,
                    "aspect_ratio": aspect_ratio,
                    "selected_image_no": int(selected_image_no or 0),
                    "variation_type": variation_type,
                    "cost_tokens": int(cost_tokens),
                },
            )
            charged = True
        await enqueue_job(
            {
                "job_id": uuid4().hex,
                "kind": "telegram_midjourney_run",
                "type": "midjourney",
                "chat_id": int(chat_id),
                "user_id": int(user_id),
                "mj_action": action_name,
                "prompt": str(prompt or ""),
                "run_prompt": str(run_prompt or ""),
                "model": model_key,
                "aspect_ratio": str(aspect_ratio or "1:1"),
                "speed_mode": speed,
                "settings": settings if isinstance(settings, dict) else {},
                "source_task_id": str(source_task_id or ""),
                "selected_image_no": int(selected_image_no or 0),
                "variation_type": str(variation_type or "subtle"),
                "charge_tokens": int(cost_tokens),
                "charge_ref_id": charge_ref_id,
                "refund_reason": "midjourney_refund",
            },
            queue_name=MIDJOURNEY_TG_QUEUE_NAME,
        )
    except Exception as e:
        if charged:
            try:
                add_tokens(user_id, cost_tokens, reason="midjourney_refund", ref_id=charge_ref_id, meta={"stage": "enqueue_failed", "provider": "midjourney", "action": action_name, "error": str(e)[:300]})
            except Exception:
                pass
        return False, f"❌ Не удалось поставить Midjourney в очередь: {e}"

    if cost_tokens > 0:
        return True, f"✅ Midjourney: запрос принят. Списано {cost_tokens} токен. Пришлю результат, как будет готово."
    return True, "✅ Midjourney: запрос принят бесплатно по тарифу Pulse/Nexus. Пришлю результат, как будет готово."


def _seedream_model_for_bot() -> str:
    return (ARK_IMAGE_MODEL_SEEDREAM_45 or ARK_IMAGE_MODEL or "").strip()


def _seedream_size_for_aspect_ratio(aspect_ratio: str) -> str:
    ratio = str(aspect_ratio or "").strip() or "9:16"
    mapping = {
        "1:1": "2048x2048",
        "4:5": "2048x2560",
        "9:16": "1600x2848",
        "16:9": "2848x1600",
    }
    return mapping.get(ratio, mapping["9:16"])

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

def _admin_stars_200_payload(user_id: int) -> str:
    return f"admin_stars_200:{int(ADMIN_STARS_200_TOKENS)}:{int(user_id)}:{uuid4().hex}"


def _parse_admin_stars_200_payload(payload: str) -> Optional[Dict[str, Any]]:
    # admin_stars_200:<tokens>:<admin_user_id>:<nonce>
    parts = str(payload or "").strip().split(":")
    if len(parts) != 4 or parts[0] != "admin_stars_200":
        return None
    try:
        tokens = int(parts[1])
        admin_user_id = int(parts[2])
    except Exception:
        return None
    nonce = str(parts[3] or "").strip()
    if tokens != int(ADMIN_STARS_200_TOKENS) or admin_user_id <= 0 or not nonce:
        return None
    return {"tokens": tokens, "admin_user_id": admin_user_id, "nonce": nonce}


def _payment_ledger_ref(*, provider: str, charge_id: str, payload: str) -> str:
    # bot_balance_ledger.ref_id is UUID in this project, so use stable UUIDv5
    # for idempotency instead of storing raw Telegram charge_id in ref_id.
    key = f"{provider}:{charge_id or payload}"
    return str(uuid5(NAMESPACE_URL, key))


async def tg_answer_pre_checkout_query(cq_id: str, *, ok: bool, error_message: str = "") -> None:
    body = {"pre_checkout_query_id": str(cq_id), "ok": bool(ok)}
    if not ok and error_message:
        body["error_message"] = str(error_message)[:200]
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(f"{TELEGRAM_API_BASE}/answerPreCheckoutQuery", json=body)


# --- YooKassa helpers (payments in RUB: cards + SBP on hosted checkout) ---
_YK_PROCESSED: Dict[str, float] = {}  # successfully completed payment_id -> ts
_YK_PROCESSING: Dict[str, float] = {}  # payment_id currently handled by this process -> ts

def _yk_cleanup_processed(now_ts: Optional[float] = None, ttl_seconds: int = 7 * 24 * 3600) -> None:
    now = float(now_ts or time.time())
    for storage in (_YK_PROCESSED, _YK_PROCESSING):
        dead = [pid for pid, ts in storage.items() if (now - float(ts)) > ttl_seconds]
        for pid in dead:
            storage.pop(pid, None)

def _yk_mark_processed(payment_id: str) -> None:
    pid = str(payment_id or "").strip()
    if pid:
        _YK_PROCESSED[pid] = time.time()
        _YK_PROCESSING.pop(pid, None)

def _yookassa_subscription_payment_already_applied(user_id: int, payment_id: str) -> bool:
    """Best-effort idempotency guard for subscription side effects.

    Balance idempotency is checked through bot_balance_ledger(ref_id).
    Subscription activation/extension is checked separately by YooKassa payment_id,
    so a retry after partial failure will not extend the tariff twice.
    """
    pid = str(payment_id or "").strip()
    if not pid:
        return False
    try:
        uid = int(resolve_billing_user_id(int(user_id or 0)) or int(user_id or 0))
    except Exception:
        try:
            uid = int(user_id or 0)
        except Exception:
            uid = 0
    if uid <= 0:
        return False

    try:
        ev = (
            sb.table("subscription_events")
            .select("id")
            .eq("user_id", uid)
            .eq("source", "yookassa")
            .eq("payment_id", pid)
            .limit(1)
            .execute()
        )
        if getattr(ev, "data", None):
            return True
    except Exception:
        pass

    try:
        sub = (
            sb.table("user_subscriptions")
            .select("id")
            .eq("user_id", uid)
            .eq("payment_id", pid)
            .limit(1)
            .execute()
        )
        return bool(getattr(sub, "data", None))
    except Exception:
        return False

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
MIDJOURNEY_SESSION_TTL_SECONDS = int(os.getenv("MIDJOURNEY_SESSION_TTL_SECONDS", "86400") or "86400")
SUPABASE_STORAGE_BUCKET = (os.getenv("SUPABASE_BUCKET") or "").strip()
STATE: Dict[Tuple[int, int], Dict[str, Any]] = {}

# ---------------- AI chat memory (in-RAM, only for mode=chat) ----------------
AI_CHAT_HISTORY_MAX = int(os.getenv("AI_CHAT_HISTORY_MAX", str(KIE_CLAUDE_HISTORY_MESSAGES)))  # last N messages (user+assistant)
AI_CHAT_TTL_SECONDS = int(os.getenv("AI_CHAT_TTL_SECONDS", "7200"))  # 2 hours
AI_CHAT_SUMMARY_MAX_CHARS = int(os.getenv("AI_CHAT_SUMMARY_MAX_CHARS", "5000"))
AI_CHAT_SUMMARY_BATCH = int(os.getenv("AI_CHAT_SUMMARY_BATCH", "10"))  # summarize each N trimmed messages
AI_CHAT_FILE_MAX_BYTES = int(os.getenv("AI_CHAT_FILE_MAX_BYTES", str(10 * 1024 * 1024)))
AI_CHAT_FILE_TEXT_MAX_CHARS = int(os.getenv("AI_CHAT_FILE_TEXT_MAX_CHARS", "50000"))

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
        has_live_mj_session = False
        mj_sessions = v.get("midjourney_sessions")
        if isinstance(mj_sessions, dict):
            for meta in mj_sessions.values():
                try:
                    ts_mj = float((meta or {}).get("ts", 0) or 0)
                except Exception:
                    ts_mj = 0.0
                if ts_mj and (now - ts_mj <= MIDJOURNEY_SESSION_TTL_SECONDS):
                    has_live_mj_session = True
                    break
        if now - ts > STATE_TTL_SECONDS and not has_live_mj_session:
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

        mj_sessions = _v.get("midjourney_sessions")
        if isinstance(mj_sessions, dict):
            expired_mj = []
            for tok, meta in mj_sessions.items():
                try:
                    ts3 = float((meta or {}).get("ts", 0) or 0)
                except Exception:
                    ts3 = 0.0
                if now - ts3 > MIDJOURNEY_SESSION_TTL_SECONDS:
                    expired_mj.append(tok)
            for tok in expired_mj:
                mj_sessions.pop(tok, None)

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

    try:
        return await kie_claude_summarize_dialogue(
            messages=chunk,
            previous_summary=prev_summary,
            max_chars=AI_CHAT_SUMMARY_MAX_CHARS,
        )
    except Exception:
        return (prev_summary or "")[:AI_CHAT_SUMMARY_MAX_CHARS]


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


def _set_mode(chat_id: int, user_id: int, mode: str):
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
        st["t2i"] = {"step": "need_prompt", "aspect_ratio": "9:16", "model": "seedream_45"}

    elif mode == "gpt_image_2_t2i":
        st["gpt_image_2_t2i"] = {"step": "need_prompt", "aspect_ratio": "1:1", "size": "1024x1024"}

    elif mode == "gpt_image_2_i2i":
        st["gpt_image_2_i2i"] = {"step": "need_image", "photo_file_id": None, "photo_file_ids": [], "photo_urls": [], "aspect_ratio": "1:1", "size": "1024x1024"}

    elif mode == "gpt_image_2_kie_t2i":
        st["gpt_image_2_kie_t2i"] = {"step": "need_prompt", "aspect_ratio": "16:9", "resolution": "2K"}

    elif mode == "gpt_image_2_kie_i2i":
        st["gpt_image_2_kie_i2i"] = {"step": "need_image", "photo_file_id": None, "photo_file_ids": [], "photo_urls": [], "aspect_ratio": "16:9", "resolution": "2K"}

    elif mode == "midjourney":
        st["midjourney"] = _midjourney_default_state("midjourney-v7")

    elif mode == "two_photos":
        # 2 фото: multi-image (если эндпоинт поддерживает)
        st["two_photos"] = {
            "step": "need_photo_1",
            "photo1_bytes": None,
            "photo1_file_id": None,
            "photo2_bytes": None,
            "photo2_file_id": None,
            "aspect_ratio": "9:16",
            "model": "seedream_45",
        }


    elif mode == "nano_banana":
        # Nano Banana (Replicate): image editing
        st["nano_banana"] = {"step": "need_photo", "photo_bytes": None, "photo_file_id": None}
        
    elif mode == "nano_banana_pro":
        st["nano_banana_pro"] = {
            "step": "need_photo",
            "photo_bytes": None,
            "resolution": "2K",
            "aspect_ratio": "9:16",
        }

    elif mode == "nano_banana_pro_new":
        st["nano_banana_pro_new"] = {
            "step": "need_photo",
            "photo_bytes": None,
            "photo_file_id": None,
            "photo_file_ids": [],
            "resolution": "2K",
            "aspect_ratio": "9:16",
        }

    elif mode == "nano_banana_2":
        st["nano_banana_2"] = {
            "step": "need_photo",
            "photo_bytes": None,
            "resolution": "2K",
            "aspect_ratio": "9:16",
        }

    elif mode == "topaz_photo":
        st["topaz_photo"] = {"step": "choose_preset", "preset_slug": None}

    elif mode == "topaz_video":
        st["topaz_video"] = {"step": "choose_preset", "preset_slug": None}

    elif mode == "sora_t2v":
        st["sora_t2v"] = {"step": "need_prompt"}

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

    elif mode == "grok_t2v":
        st["grok_t2v"] = {"step": "need_prompt"}

    elif mode == "grok_i2v":
        st["grok_i2v"] = {"step": "need_image", "image_bytes": None, "image_name": None}

    elif mode == "omni_flash_t2v":
        # Google Omni Flash Telegram flow.
        # Keep omni_flash_settings alive; prompt handler reads duration/resolution/aspect_ratio from it.
        st["omni_flash_t2v"] = {"step": "need_prompt"}

    elif mode == "omni_flash_i2v":
        # Do not fall into the default branch, because it clears omni_flash_settings
        # and causes generation to use default 16:9 even after user selected 9:16.
        st["omni_flash_i2v"] = {"step": "need_images", "images": [], "image_urls": []}

    elif mode == "omni_flash_video_edit":
        st["omni_flash_video_edit"] = {"step": "need_video", "image_urls": []}

    elif mode == "kling3_turbo_wait_prompt":
        prev = st.get("kling3_turbo_settings") if isinstance(st.get("kling3_turbo_settings"), dict) else {}
        st["kling3_turbo_settings"] = prev or {"gen_mode": "text_to_video", "resolution": "720p", "duration": 5, "aspect_ratio": "16:9"}

    else:
        # chat / default
        st.pop("poster", None)
        st.pop("photosession", None)
        st.pop("t2i", None)
        st.pop("gpt_image_2_t2i", None)
        st.pop("gpt_image_2_i2i", None)
        st.pop("gpt_image_2_kie_t2i", None)
        st.pop("gpt_image_2_kie_i2i", None)
        st.pop("midjourney", None)
        st.pop("two_photos", None)
        st.pop("nano_banana", None)
        st.pop("nano_banana_pro", None)
        st.pop("nano_banana_pro_new", None)
        st.pop("nano_banana_2", None)
        st.pop("topaz_photo", None)
        st.pop("topaz_video", None)
        st.pop("photo_submenu", None)
        st.pop("sora_t2v", None)
        st.pop("sora_settings", None)
        st.pop("veo_t2v", None)
        st.pop("veo_i2v", None)
        st.pop("grok_settings", None)
        st.pop("omni_flash_settings", None)
        st.pop("grok_t2v", None)
        st.pop("grok_i2v", None)
        st.pop("kling3_turbo_settings", None)
        st.pop("ai_chat_mode", None)
        st.pop("ai_chat_model", None)
        st.pop("ai_prompt", None)



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


def _normalize_music_ai_choice(settings: dict) -> str:
    ai_choice = str((settings.get("ai") or "suno")).lower().strip()
    return ai_choice if ai_choice in ("suno", "udio") else "suno"


def _normalize_music_provider(settings: dict, ai_choice: str) -> str:
    provider = str(
        settings.get("provider")
        or settings.get("api")
        or settings.get("ai_provider")
        or settings.get("aiProvider")
        or ""
    ).lower().strip()
    if provider in ("suno-api", "suno_api", "suno api"):
        provider = "sunoapi"
    if ai_choice == "udio":
        return "piapi"
    if provider in ("piapi", "sunoapi", "auto"):
        return provider
    provider = os.getenv("MUSIC_PROVIDER_DEFAULT", "piapi").lower().strip() or "piapi"
    return provider if provider in ("piapi", "sunoapi", "auto") else "piapi"


def _build_music_piapi_payload(settings: dict, ai_choice: str) -> dict:
    if ai_choice == "udio":
        udio_prompt = (
            (settings.get("gpt_description_prompt") or "").strip()
            or (settings.get("prompt") or "").strip()
            or "Modern atmospheric music with emotional melody"
        )
        return {
            "model": "music-u",
            "task_type": "generate_music",
            "input": {
                "gpt_description_prompt": udio_prompt,
                "lyrics_type": "instrumental" if settings.get("make_instrumental") else "generate",
            },
            "config": {"service_mode": (settings.get("service_mode") or "public")},
        }

    input_block = {
        "mv": settings.get("mv") or "chirp-crow",
        "title": settings.get("title") or "",
        "tags": settings.get("tags") or "",
        "make_instrumental": bool(settings.get("make_instrumental")),
    }
    if str(settings.get("music_mode") or "prompt").lower().strip() == "custom":
        input_block["prompt"] = settings.get("prompt") or ""
    else:
        input_block["gpt_description_prompt"] = settings.get("gpt_description_prompt") or ""

    return {
        "model": "suno",
        "task_type": "music",
        "input": input_block,
        "config": {"service_mode": settings.get("service_mode") or "public"},
    }


async def _enqueue_music_job(*, chat_id: int, user_id: int, settings: dict, charge_tokens: int = 0) -> dict:
    settings = dict(settings or {})
    ai_choice = _normalize_music_ai_choice(settings)
    provider = _normalize_music_provider(settings, ai_choice)

    if ai_choice == "udio":
        job_type = "music_piapi"
    elif provider == "piapi":
        job_type = "music_piapi"
    elif provider == "sunoapi":
        job_type = "music_suno"
    else:
        job_type = "music"

    job = {
        "job_id": uuid4().hex,
        "type": job_type,
        "chat_id": int(chat_id),
        "user_id": int(user_id),
        "settings": settings,
        "ai": ai_choice,
        "provider": provider,
        "refund_reason": "suno_music_refund",
    }

    if provider != "sunoapi" or ai_choice == "udio":
        job["payload_api"] = _build_music_piapi_payload(settings, ai_choice)

    if int(charge_tokens or 0) > 0:
        job["charge_tokens"] = int(charge_tokens)

    await enqueue_job(job, queue_name="music")
    return job


async def _enqueue_tg_grok_job(*, chat_id: int, user_id: int, mode: str, prompt: str, settings: dict, image_bytes: bytes | None = None, image_name: str | None = None, charge_tokens: int = 0, charge_ref_id: str = "") -> dict:
    settings = dict(settings or {})
    model = normalize_grok_model(settings.get("model") or GROK_LEGACY_MODEL)
    if is_grok15_model(model):
        if mode != "image_to_video":
            raise RuntimeError("Grok 1.5 Preview пока доступен только в Image → Video")
        duration = normalize_grok15_duration(settings.get("duration") or 5)
        resolution = normalize_grok15_resolution(settings.get("resolution") or "480p")
        aspect_ratio = normalize_grok15_aspect_ratio(settings.get("aspect_ratio") or "16:9")
        provider_mode = ""
    else:
        duration = normalize_grok_duration(settings.get("duration") or 6)
        resolution = normalize_grok_resolution(settings.get("resolution") or "480p")
        aspect_ratio = normalize_grok_aspect_ratio(settings.get("aspect_ratio") or "16:9")
        provider_mode = normalize_grok_provider_mode(settings.get("provider_mode") or "normal")
    start_frame_url = None
    if mode == "image_to_video":
        if not image_bytes:
            raise RuntimeError("Для Grok Image → Video нужно стартовое фото")
        if is_grok15_model(model):
            validate_grok15_input_image(image_bytes, image_name)
        ext = "jpg"
        mime = "image/jpeg"
        head = bytes((image_bytes or b"")[:16])
        if head.startswith(b"\x89PNG"):
            ext = "png"
            mime = "image/png"
        elif head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            ext = "webp"
            mime = "image/webp"
        elif isinstance(image_name, str) and "." in image_name:
            tail = image_name.rsplit(".", 1)[-1].lower().strip()
            if tail in ("jpg", "jpeg"):
                ext = "jpg"
                mime = "image/jpeg"
            elif tail == "png":
                ext = "png"
                mime = "image/png"
            elif tail == "webp":
                ext = "webp"
                mime = "image/webp"
        path = f"grok_inputs/{int(user_id)}/{int(time.time())}_{uuid4().hex[:10]}.{ext}"
        start_frame_url = upload_bytes_to_supabase(path, image_bytes, mime)

    job = {
        "job_id": uuid4().hex,
        "kind": "tg_grok_video_run",
        "chat_id": int(chat_id),
        "user_id": int(user_id),
        "provider": "grok",
        "model": model,
        "mode": "image_to_video" if mode == "image_to_video" else "text_to_video",
        "prompt": str(prompt or "").strip(),
        "duration": duration,
        "resolution": resolution,
        "aspect_ratio": aspect_ratio,
        "provider_mode": provider_mode,
        "start_frame_url": start_frame_url,
        "charge_tokens": int(charge_tokens or 0),
        "charge_ref_id": str(charge_ref_id or ""),
        "refund_reason": "grok_video_refund",
        "origin": "telegram",
    }
    target_queue = WORKSPACE_GROK15_QUEUE_NAME if is_grok15_model(model) else WORKSPACE_MEDIA_QUEUE_NAME
    await enqueue_job(job, queue_name=target_queue)
    return job


async def _enqueue_tg_omni_flash_job(*, chat_id: int, user_id: int, mode: str, prompt: str, settings: dict, reference_images: list[tuple[bytes, str]] | None = None, reference_image_urls: list[str] | None = None, source_video_upload_id: str = "", source_video_end: int | None = None, charge_tokens: int = 0, charge_ref_id: str = "") -> dict:
    settings = dict(settings or {})
    duration = normalize_gemini_omni_duration(settings.get("duration") or 8)
    resolution = normalize_gemini_omni_resolution(settings.get("resolution") or "1080p")
    aspect_ratio = normalize_gemini_omni_aspect_ratio(settings.get("aspect_ratio") or "16:9")
    normalized_mode = normalize_gemini_omni_mode(mode)
    max_refs = 5 if normalized_mode == "video_edit" else 7
    reference_image_urls = [str(url or "").strip() for url in (reference_image_urls or []) if str(url or "").strip()][:max_refs]
    source_video_upload_id = str(source_video_upload_id or "").strip()
    if normalized_mode == "video_edit" and not source_video_upload_id:
        raise RuntimeError("Для Google Omni Flash Video Edit нужно исходное видео")
    normalized_source_video_end = None
    if normalized_mode == "video_edit":
        try:
            normalized_source_video_end = int(max(1, min(float(KIE_OMNI_VIDEO_EDIT_MAX_DURATION_SEC), float(source_video_end or KIE_OMNI_VIDEO_EDIT_MAX_DURATION_SEC))))
        except Exception:
            normalized_source_video_end = int(KIE_OMNI_VIDEO_EDIT_MAX_DURATION_SEC)
    if normalized_mode == "image_to_video":
        # В нормальном Telegram flow фото уже загружены в Supabase сразу при получении,
        # чтобы не хранить bytes в user state/Redis. Этот fallback оставлен для старых вызовов.
        if not reference_image_urls:
            pairs = list(reference_images or [])
            for idx, pair in enumerate(pairs[:max_refs], start=1):
                image_bytes, image_name = pair
                ext = "jpg"
                mime = "image/jpeg"
                head = bytes((image_bytes or b"")[:16])
                if head.startswith(b"\x89PNG"):
                    ext = "png"
                    mime = "image/png"
                elif head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                    ext = "webp"
                    mime = "image/webp"
                elif isinstance(image_name, str) and "." in image_name:
                    tail = image_name.rsplit(".", 1)[-1].lower().strip()
                    if tail in ("jpg", "jpeg"):
                        ext = "jpg"
                        mime = "image/jpeg"
                    elif tail == "png":
                        ext = "png"
                        mime = "image/png"
                    elif tail == "webp":
                        ext = "webp"
                        mime = "image/webp"
                path = f"omni_flash_inputs/{int(user_id)}/{int(time.time())}_{idx}_{uuid4().hex[:10]}.{ext}"
                uploaded_url = upload_bytes_to_supabase(path, image_bytes, mime)
                if uploaded_url:
                    reference_image_urls.append(str(uploaded_url).strip())
        if not reference_image_urls:
            raise RuntimeError("Для Google Omni Flash нужен хотя бы один reference image")

    job = {
        "job_id": uuid4().hex,
        "kind": "tg_omni_flash_video_run",
        "chat_id": int(chat_id),
        "user_id": int(user_id),
        "provider": "google",
        "model": "gemini-omni-video",
        "mode": normalized_mode if normalized_mode in {"image_to_video", "video_edit"} else "text_to_video",
        "prompt": str(prompt or "").strip(),
        "duration": duration,
        "resolution": resolution,
        "aspect_ratio": aspect_ratio,
        "reference_image_urls": reference_image_urls,
        "source_video_upload_id": source_video_upload_id if normalized_mode == "video_edit" else "",
        "source_video_end": normalized_source_video_end if normalized_mode == "video_edit" else None,
        "charge_tokens": int(charge_tokens or 0),
        "charge_ref_id": str(charge_ref_id or ""),
        "refund_reason": "gemini_omni_video_refund",
        "origin": "telegram",
    }
    await enqueue_job(job, queue_name=WORKSPACE_MEDIA_QUEUE_NAME)
    return job


async def _enqueue_tg_veo_relax_job(*, chat_id: int, user_id: int, mode: str, prompt: str, settings: dict, image_bytes: bytes | None = None, image_name: str = "start_frame.jpg", last_frame_bytes: bytes | None = None, last_frame_name: str = "last_frame.jpg", charge_tokens: int = 0, charge_ref_id: str = "", delay_sec: int = 0) -> dict:
    settings = dict(settings or {})
    normalized_mode = "image_to_video" if str(mode or "").strip().lower() in {"image", "image_to_video", "i2v", "image2video"} else "text_to_video"
    duration = normalize_veo31_fast_relax_duration(settings.get("duration") or 8)
    resolution = normalize_veo31_fast_relax_resolution(settings.get("resolution") or "1080p")
    aspect_ratio = normalize_veo31_fast_relax_aspect_ratio(settings.get("aspect_ratio") or "16:9")
    start_frame_url = ""
    last_frame_url = ""
    if normalized_mode == "image_to_video":
        if not image_bytes:
            raise RuntimeError("Для Veo 3.1 Fast Relax Image → Video нужен первый кадр")
        start_frame_url = upload_veo31_fast_relax_input_image(user_id=int(user_id), image_bytes=image_bytes, filename_hint=image_name, slot="start")
        if last_frame_bytes:
            last_frame_url = upload_veo31_fast_relax_input_image(user_id=int(user_id), image_bytes=last_frame_bytes, filename_hint=last_frame_name, slot="last")

    job = {
        "job_id": uuid4().hex,
        "kind": "tg_veo_relax_video_run",
        "chat_id": int(chat_id),
        "user_id": int(user_id),
        "provider": "veo",
        "model": "veo-3.1-fast-relax",
        "mode": normalized_mode,
        "prompt": str(prompt or "").strip(),
        "duration": duration,
        "resolution": resolution,
        "aspect_ratio": aspect_ratio,
        "start_frame_url": start_frame_url,
        "last_frame_url": last_frame_url,
        "charge_tokens": int(charge_tokens or 0),
        "charge_ref_id": str(charge_ref_id or ""),
        "refund_reason": "veo31_fast_relax_video_refund",
        "origin": "telegram",
        "delayed_start_sec": int(delay_sec or 0),
    }
    if int(delay_sec or 0) > 0:
        await enqueue_job_delayed(job, delay_sec=int(delay_sec or 0), queue_name=WORKSPACE_VEO_RELAX_QUEUE_NAME)
    else:
        await enqueue_job(job, queue_name=WORKSPACE_VEO_RELAX_QUEUE_NAME)
    return job


def _detect_video_type(raw: bytes, filename: str = "", content_type: str = "") -> tuple[str, str]:
    ctype = str(content_type or "").split(";", 1)[0].strip().lower()
    suffix = str(filename or "").rsplit(".", 1)[-1].lower().strip() if "." in str(filename or "") else ""
    head = bytes((raw or b"")[:16])
    if suffix == "mov" or ctype in {"video/quicktime", "video/mov"}:
        return "mov", "video/quicktime"
    if suffix == "webm" or ctype == "video/webm":
        return "webm", "video/webm"
    if suffix == "mp4" or ctype == "video/mp4" or head[4:8] == b"ftyp":
        return "mp4", "video/mp4"
    return "mp4", "video/mp4"


def _probe_video_duration_from_bytes(raw: bytes, ext: str = "mp4") -> float:
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=f".{(ext or 'mp4').strip('.').lower() or 'mp4'}") as tmp:
            tmp.write(raw or b"")
            tmp.flush()
            tmp_path = tmp.name
        meta = probe_media(tmp_path)
        return float(meta.get("duration") or meta.get("duration_sec") or 0.0)
    except Exception:
        return 0.0
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def _upload_omni_flash_source_video(*, user_id: int, video_bytes: bytes, filename: str = "omni_flash_source.mp4", content_type: str = "video/mp4", duration_hint: float = 0.0) -> tuple[str, float]:
    if not video_bytes:
        raise RuntimeError("Пустое видео")
    ext, mime = _detect_video_type(video_bytes, filename, content_type)
    duration_sec = float(duration_hint or 0)
    if duration_sec <= 0:
        duration_sec = _probe_video_duration_from_bytes(video_bytes, ext)
    if duration_sec <= 0:
        raise RuntimeError("Не удалось определить длительность видео")
    if duration_sec > float(KIE_OMNI_VIDEO_EDIT_MAX_DURATION_SEC):
        raise RuntimeError(f"Видео слишком длинное. Максимум {KIE_OMNI_VIDEO_EDIT_MAX_DURATION_SEC} секунд.")

    # Telegram Video Edit source videos use the same workspace upload pipeline as the site:
    # ffprobe metadata, workspace-videos bucket, workspace_video_uploads row, signed/public access URL, TTL cleanup.
    # Important: store/pass upload_id, not a short-lived signed URL. The worker will build a fresh URL right before KIE request.
    upload_row = create_workspace_upload_record(
        user_id=int(user_id),
        filename=filename or f"omni_flash_source.{ext}",
        content_type=mime,
        raw_bytes=video_bytes,
    )
    upload_id = str(upload_row.get("id") or "").strip()
    if not upload_id:
        raise RuntimeError("Не удалось сохранить исходное видео")
    row_duration = float(upload_row.get("duration_sec") or 0)
    if row_duration > 0:
        duration_sec = row_duration
    return upload_id, duration_sec


def _clear_music_ctx(st: dict, chat_id: int, user_id: int) -> None:
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

def _main_menu_keyboard(is_admin: bool = False, user_id: Optional[int] = None) -> dict:
    account_url = _with_uid(WEBAPP_ACCOUNT_URL, int(user_id)) if user_id else WEBAPP_ACCOUNT_URL
    rows = [
        [{"text": "ИИ (чат)"}, {"text": "Фото будущего"}],
        [
            {"text": "🎬 Видео будущего", "web_app": {"url": WEBAPP_KLING_URL}},
            {"text": "🎵 Музыка будущего", "web_app": {"url": WEBAPP_MUSIC_URL}},
        ],
        [
            {"text": "🔊 Озвучить текст"},
            {"text": "📚 Промпты", "web_app": {"url": _with_uid(WEBAPP_PROMPTS_URL, int(user_id)) if user_id else WEBAPP_PROMPTS_URL}},
        ],
        [{"text": "💰 Баланс"}, {"text": "👤 Кабинет", "web_app": {"url": account_url}}],
    ]
    if is_admin:
        rows.append([{"text": "📊 Статистика"}, {"text": "📣 Рассылка"}])
        rows.append([{"text": ADMIN_STARS_200_BUTTON_TEXT}, {"text": "Для Pro"}])

    return {
        "keyboard": rows,
        "resize_keyboard": True,
        "one_time_keyboard": False,
        "selective": False,
    }


def _main_menu_for(user_id: int) -> dict:
    return _main_menu_keyboard(_is_admin(user_id), user_id=user_id)



def _with_uid(url: str, user_id: int) -> str:
    """Append ?uid=<user_id> to a URL (preserving existing query params)."""
    try:
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        # do not overwrite if already present
        if "uid" not in qs and "tg_user_id" not in qs and "user_id" not in qs:
            qs["uid"] = [str(user_id)]
        new_query = urllib.parse.urlencode(qs, doseq=True)
        return urllib.parse.urlunparse(parsed._replace(query=new_query))
    except Exception:
        # best-effort fallback
        joiner = "&" if ("?" in url) else "?"
        return f"{url}{joiner}uid={user_id}"


def _pro_menu_keyboard(user_id: int) -> dict:
    rows = [
        [{"text": "🏆 Top Analizator", "web_app": {"url": _with_uid(WEBAPP_TOP_ANALIZATOR_URL, user_id)}}],
        [{"text": "📚 Промпты", "web_app": {"url": _with_uid(WEBAPP_PROMPTS_URL, user_id)}}],
        [{"text": "⬅️ Назад"}],
    ]
    if _is_admin(user_id):
        rows.insert(2, [{"text": "🛠 Админка промптов", "web_app": {"url": _with_uid(WEBAPP_PROMPTS_ADMIN_URL, user_id)}}])
    return {
        "keyboard": rows,
        "resize_keyboard": True,
        "one_time_keyboard": False,
        "selective": False,
    }

def _pro_menu_for(user_id: int) -> dict:
    return _pro_menu_keyboard(user_id)


def _tg_free_limit_message(exc: FreePlanLimitError) -> str:
    return str(getattr(exc, "message", "") or str(exc) or "Дневной лимит Free исчерпан. Доступ обновится завтра.")


async def _tg_consume_free_chat_or_notify(chat_id: int, user_id: int) -> bool:
    try:
        consume_free_usage(user_id, FEATURE_CHAT)
        return True
    except FreePlanLimitError as exc:
        await tg_send_message(chat_id, "🔒 " + _tg_free_limit_message(exc), reply_markup=_main_menu_for(user_id))
        return False
    except Exception as exc:
        await tg_send_message(chat_id, f"❌ Не удалось проверить лимит Free-чата: {exc}", reply_markup=_main_menu_for(user_id))
        return False




def _is_fable_chat_model_key(model_key: Any) -> bool:
    return _ai_chat_model_key_from_value(model_key) == "claude_fable"


def _tg_fable_thinking_enabled(st: Dict[str, Any]) -> bool:
    return bool((st or {}).get("ai_fable_thinking"))


def _tg_fable_chat_cost_tokens(st: Dict[str, Any], *, has_files: bool = False) -> int:
    return kie_claude_fable_tokens(has_files=has_files, thinking=_tg_fable_thinking_enabled(st))


async def _tg_charge_fable_chat_or_notify(
    *,
    chat_id: int,
    user_id: int,
    st: Dict[str, Any],
    has_files: bool = False,
) -> str:
    cost_tokens = _tg_fable_chat_cost_tokens(st, has_files=has_files)
    try:
        ensure_user_row(user_id)
        balance = int(get_balance(user_id) or 0)
    except Exception as exc:
        await tg_send_message(chat_id, f"❌ Не удалось проверить баланс для Claude Fable 5: {exc}", reply_markup=_main_menu_for(user_id))
        return ""

    if balance < cost_tokens:
        await tg_send_message(
            chat_id,
            f"❌ Недостаточно токенов для Claude Fable 5\nНужно: {cost_tokens}\nБаланс: {balance}",
            reply_markup=_topup_balance_inline_kb(),
        )
        return ""

    ref_id = str(uuid4())
    try:
        add_tokens(
            user_id,
            -cost_tokens,
            reason="claude_fable_chat",
            ref_id=ref_id,
            meta={
                "source": "telegram",
                "model": KIE_CLAUDE_FABLE_MODEL_ID,
                "cost_tokens": cost_tokens,
                "has_files": bool(has_files),
                "thinking": _tg_fable_thinking_enabled(st),
            },
        )
        return ref_id
    except Exception as exc:
        await tg_send_message(chat_id, f"❌ Не удалось списать токены для Claude Fable 5: {exc}", reply_markup=_main_menu_for(user_id))
        return ""

async def _tg_consume_free_tts_or_notify(chat_id: int, user_id: int, text: str) -> bool:
    try:
        validate_free_tts_text(user_id, text)
        consume_free_usage(user_id, FEATURE_TTS)
        return True
    except FreePlanLimitError as exc:
        await tg_send_message(chat_id, "🔒 " + _tg_free_limit_message(exc), reply_markup=_help_menu_for(user_id))
        return False
    except Exception as exc:
        await tg_send_message(chat_id, f"❌ Не удалось проверить лимит Free-озвучки: {exc}", reply_markup=_help_menu_for(user_id))
        return False


async def _tg_prompt_access_or_notify(chat_id: int, user_id: int) -> bool:
    try:
        if not is_free_plan_user(user_id):
            return True
    except Exception as exc:
        await tg_send_message(chat_id, f"❌ Не удалось проверить тариф: {exc}", reply_markup=_main_menu_for(user_id))
        return False
    await tg_send_message(
        chat_id,
        "🔒 Библиотека готовых промтов доступна на платных тарифах. Режим «🪄 Промт» в AI-чате доступен на Free в пределах дневного лимита чата.",
        reply_markup=_ai_chat_mode_inline_kb(),
    )
    return False


def _tts_gender_keyboard() -> dict:
    return {
        "keyboard": [
            [{"text": "👨 Мужские голоса"}, {"text": "👩 Женские голоса"}],
            [{"text": "⬅️ Назад"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False,
        "selective": False,
    }
# ---- TTS voices (curated) ----
_TTS_VOICES_MALE = [
    {"voice_id": "huXlXYhtMIZkTYxM93t6", "name": "Масон"},
    {"voice_id": "kwajW3Xh5svCeKU5ky2S", "name": "Дмитрий"},
    {"voice_id": "gJEfHTTiifXEDmO687lC", "name": "Принц Нур"},
    {"voice_id": "oKxkBkm5a8Bmrd1Whf2c", "name": "Нур"},
    {"voice_id": "3EuKHIEZbSzrHGNmdYsx", "name": "Николай"},
]

_TTS_VOICES_FEMALE = [
    {"voice_id": "IO0VLmDxIb8N5msewtV4", "name": "Анна"},
    {"voice_id": "gCqVHuQpLDMkHrGiG95I", "name": "Татьяна"},
    {"voice_id": "OowtKaZH9N7iuGbsd00l", "name": "Вероника"},
]

# Быстрый индекс по названию кнопки -> voice
_TTS_BY_BTN = {}
for _v in (_TTS_VOICES_MALE + _TTS_VOICES_FEMALE):
    _TTS_BY_BTN[f"🎙 {_v['name']}"] = _v


def _tts_voices_keyboard(gender: str) -> dict:
    voices = _TTS_VOICES_MALE if gender == "male" else _TTS_VOICES_FEMALE

    rows = []
    row = []
    for v in voices:
        row.append({"text": f"🎙 {v['name']}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    rows.append([{"text": "⬅️ Назад"}])
    return {
        "keyboard": rows,
        "resize_keyboard": True,
        "one_time_keyboard": False,
        "selective": False,
    }
    
def _help_menu_for(user_id: int) -> dict:
    """Главное меню + экстренная кнопка сброса генерации (показываем ТОЛЬКО в «Помощь»)."""
    base = _main_menu_keyboard(_is_admin(user_id), user_id=user_id)
    # defensive copy
    rows = [list(r) for r in (base.get("keyboard") or [])]
    rows.append([{"text": "🔄 Сбросить генерацию"}])
    base2 = dict(base)
    base2["keyboard"] = rows
    return base2


def _seedance_refs_collect_kb() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "✅ Готово", "callback_data": "seedance_refs:done"}],
            [{"text": "❌ Отмена", "callback_data": "seedance_refs:cancel"}],
        ]
    }


def _seedance_prompt_collect_kb(mode: str = "") -> dict:
    rows = [
        [{"text": "✅ Запустить", "callback_data": "seedance_prompt:done"}],
        [{"text": "🗑 Очистить промпт", "callback_data": "seedance_prompt:clear"}],
    ]
    if str(mode or "").strip() in ("seedance_i2v", "seedance_omni"):
        rows.append([{"text": "⬅️ Вернуться к refs", "callback_data": "seedance_refs:back"}])
    rows.append([{"text": "❌ Отмена", "callback_data": "seedance_prompt:cancel"}])
    return {"inline_keyboard": rows}


def _seedance_prompt_back_kb() -> dict:
    # Backward-compatible wrapper for old call sites.
    return _seedance_prompt_collect_kb("seedance_omni")


def _seedance_uses_kie_backend(settings: Optional[Dict[str, Any]]) -> bool:
    settings = settings or {}
    provider_kind = str(settings.get("provider_kind") or "seedance").strip().lower()
    seedance_model = str(settings.get("seedance_model") or "").strip().lower()
    task_type = str(settings.get("task_type") or "").strip().lower()
    return (
        provider_kind == "seedance_kie"
        or seedance_model in {"seedance-kie-mini", "seedance-2-mini", "seedance-mini", "mini"}
        or task_type == "seedance-2-mini"
    )


def _seedance_prompt_limit_from_settings(settings: Optional[Dict[str, Any]]) -> int:
    return 20000 if _seedance_uses_kie_backend(settings) else 4000


def _seedance_prompt_state_key(st: Dict[str, Any]) -> str:
    mode_now = str((st or {}).get("mode") or "").strip()
    return mode_now if mode_now in ("seedance_t2v", "seedance_i2v", "seedance_omni") else ""


def _seedance_prompt_text_from_state(st: Dict[str, Any]) -> str:
    key = _seedance_prompt_state_key(st)
    if not key:
        return ""
    ctx = (st or {}).get(key) or {}
    parts = ctx.get("prompt_parts") or []
    cleaned = [str(x or "").strip() for x in parts if str(x or "").strip()]
    if cleaned:
        return "\n\n".join(cleaned).strip()
    return str(ctx.get("prompt") or "").strip()


def _seedance_prompt_clear_state(st: Dict[str, Any]) -> None:
    key = _seedance_prompt_state_key(st)
    if not key:
        return
    ctx = (st or {}).get(key) or {}
    ctx["prompt_parts"] = []
    ctx["prompt"] = None
    st[key] = ctx
    st["ts"] = _now()


def _seedance_prompt_append_part(st: Dict[str, Any], text: str, limit: int) -> Tuple[bool, str, int, int]:
    key = _seedance_prompt_state_key(st)
    if not key:
        return False, "Сейчас я не жду Seedance-промпт.", 0, 0

    part = str(text or "").strip()
    if not part:
        return False, "Пустой текст не добавил. Пришли часть промпта текстом.", 0, 0

    ctx = (st or {}).get(key) or {"step": "need_prompt"}
    parts = [str(x or "").strip() for x in (ctx.get("prompt_parts") or []) if str(x or "").strip()]
    next_text = "\n\n".join(parts + [part]).strip()

    if int(limit or 0) > 0 and len(next_text) > int(limit):
        return (
            False,
            f"Промпт станет слишком длинным: {len(next_text)} символов. Лимит для этого режима: {int(limit)}.",
            len(parts),
            len("\n\n".join(parts).strip()),
        )

    parts.append(part)
    ctx["prompt_parts"] = parts
    ctx["prompt"] = next_text
    st[key] = ctx
    st["ts"] = _now()
    return True, "", len(parts), len(next_text)


async def _seedance_start_generation_from_prompt(chat_id: int, user_id: int, st: Dict[str, Any], prompt: str) -> Dict[str, bool]:
    mode_now = str((st or {}).get("mode") or "").strip()
    if mode_now not in ("seedance_t2v", "seedance_i2v", "seedance_omni"):
        await tg_send_message(chat_id, "Сейчас я не жду Seedance-промпт. Открой настройки Seedance заново.", reply_markup=_main_menu_for(user_id))
        return {"ok": True}

    settings = st.get("seedance_settings") or {}
    provider_kind = str(settings.get("provider_kind") or "seedance").strip() or "seedance"
    seedance_model = str(settings.get("seedance_model") or ("seedance-kie-480p" if provider_kind == "seedance_kie" else "preview")).strip()
    task_type = str(settings.get("task_type") or ("seedance-2-fast" if seedance_model == "seedance-kie-480p" else ("seedance-2" if provider_kind == "seedance_kie" else "seedance-2-preview"))).strip()
    if seedance_model.lower() in {"seedance-kie-mini", "seedance-2-mini", "seedance-mini", "mini"} or task_type.lower() == "seedance-2-mini":
        seedance_model = "seedance-kie-mini"
        task_type = "seedance-2-mini"
    duration = int(settings.get("duration") or 5)
    aspect_ratio = str(settings.get("aspect_ratio") or "16:9").strip()
    max_images = int(settings.get("max_images") or (7 if _seedance_uses_kie_backend(settings) else 2))
    max_videos = int(settings.get("max_videos") or 0)
    max_audios = int(settings.get("max_audios") or 0)
    prompt_limit = _seedance_prompt_limit_from_settings(settings)

    prompt = str(prompt or "").strip()
    if not prompt:
        await tg_send_message(
            chat_id,
            "Промпт пока пустой. Пришли текст одной или несколькими частями, потом нажми «✅ Запустить».",
            reply_markup=_seedance_prompt_collect_kb(mode_now),
        )
        return {"ok": True}

    if len(prompt) > prompt_limit:
        await tg_send_message(
            chat_id,
            f"Промпт слишком длинный: {len(prompt)} символов. Лимит для этого режима: {prompt_limit}.",
            reply_markup=_seedance_prompt_collect_kb(mode_now),
        )
        return {"ok": True}

    if mode_now == "seedance_i2v":
        si = st.get("seedance_i2v") or {}
        imgs = [x for x in (si.get("image_file_ids") or []) if str(x or "").strip()]
        if not imgs:
            await tg_send_message(chat_id, "Сначала пришли хотя бы 1 фото.", reply_markup=_seedance_refs_collect_kb())
            return {"ok": True}

    if mode_now == "seedance_omni":
        so = st.get("seedance_omni") or {}
        image_ids = [x for x in (so.get("image_file_ids") or []) if str(x or "").strip()]
        video_ids = [x for x in (so.get("video_file_ids") or []) if str(x or "").strip()]
        audio_ids = [x for x in (so.get("audio_file_ids") or []) if str(x or "").strip()]
        if len(image_ids) + len(video_ids) + len(audio_ids) <= 0:
            await tg_send_message(chat_id, "Сначала пришли хотя бы один image/video/audio reference.", reply_markup=_seedance_refs_collect_kb())
            return {"ok": True}
        if audio_ids and not (image_ids or video_ids):
            await tg_send_message(chat_id, "Для Omni Reference аудио нельзя отправлять отдельно. Добавь хотя бы фото или видео reference.", reply_markup=_seedance_refs_collect_kb())
            return {"ok": True}

    if _busy_is_active(int(user_id)):
        kind = _busy_kind(int(user_id)) or "генерация"
        await tg_send_message(
            chat_id,
            f"⏳ Сейчас выполняется: {kind}. Дождись завершения (или /reset).",
            reply_markup=_help_menu_for(user_id),
        )
        return {"ok": True}

    if _seedance_uses_kie_backend(settings):
        seedance_input_video_sec = 0.0
        if mode_now == "seedance_omni":
            so_price = st.get("seedance_omni") or {}
            video_ids_for_price = [x for x in (so_price.get("video_file_ids") or []) if str(x or "").strip()]
            video_durations_for_price = list(so_price.get("video_durations_sec") or [])
            if video_ids_for_price:
                while len(video_durations_for_price) < len(video_ids_for_price):
                    video_durations_for_price.append(15.4)
                seedance_input_video_sec = float(sum(float(x or 0.0) for x in video_durations_for_price[:len(video_ids_for_price)]))
        cost_tokens = int(seedance_kie_tokens_for_duration(
            seedance_model,
            duration,
            input_video_duration_sec=seedance_input_video_sec,
        ))
    else:
        # Legacy fallback only. Seedance 2.0 Mini is normalized to KIE upstream.
        preview_price_map = {5: 9, 10: 18, 15: 27}
        cost_tokens = int(preview_price_map.get(int(duration), preview_price_map[5]))

    _busy_start(int(user_id), "Seedance видео")
    seedance_charged = False
    try:
        try:
            ensure_user_row(user_id)
            bal = int(get_balance(user_id) or 0)
        except Exception:
            bal = 0

        if bal < cost_tokens:
            await tg_send_message(
                chat_id,
                f"❌ Недостаточно токенов.\nНужно: {cost_tokens}\nБаланс: {bal}",
                reply_markup=_topup_balance_inline_kb(),
            )
            return {"ok": True}

        charge_meta = {
            "provider_kind": provider_kind,
            "seedance_model": seedance_model,
            "task_type": task_type,
            "duration": int(duration),
            "aspect_ratio": aspect_ratio,
            "cost_tokens": int(cost_tokens),
        }
        try:
            add_tokens(user_id, -cost_tokens, reason="seedance_video", meta=charge_meta)
        except TypeError:
            add_tokens(user_id, -int(cost_tokens), reason="seedance_video", meta=charge_meta)
        seedance_charged = True

        job_id = uuid4().hex
        job: Dict[str, Any] = {
            "job_id": job_id,
            "type": "seedance_video",
            "chat_id": int(chat_id),
            "user_id": int(user_id),
            "provider_kind": provider_kind,
            "seedance_model": seedance_model,
            "task_type": task_type,
            "prompt": prompt,
            "duration": int(duration),
            "aspect_ratio": aspect_ratio,
            "charge_tokens": int(cost_tokens),
        }

        if mode_now == "seedance_i2v":
            si = st.get("seedance_i2v") or {}
            job["mode"] = "image_to_video"
            job["image_file_ids"] = list(si.get("image_file_ids") or [])[:max_images]
        elif mode_now == "seedance_omni":
            so = st.get("seedance_omni") or {}
            job["mode"] = "omni_reference"
            job["image_file_ids"] = list(so.get("image_file_ids") or [])[:max_images]
            job["video_file_ids"] = list(so.get("video_file_ids") or [])[:max_videos]
            job["video_durations_sec"] = list(so.get("video_durations_sec") or [])[:max_videos]
            job["audio_file_ids"] = list(so.get("audio_file_ids") or [])[:max_audios]
        else:
            job["mode"] = "text_to_video"

        if not _seedance_uses_kie_backend(settings):
            se = st.get("seedance_extend") or {}
            se["task_type"] = task_type
            st["seedance_extend"] = se

        st.pop("seedance_t2v", None)
        st.pop("seedance_i2v", None)
        st.pop("seedance_omni", None)
        st.pop("seedance_settings", None)
        st["ts"] = _now()
        sb_clear_user_state(user_id)
        _set_mode(chat_id, user_id, "chat")

        await tg_send_message(chat_id, "⏳ Генерация может занять от 5 до 30 минут. Как будет готово — пришлю видео.", reply_markup=_help_menu_for(user_id))
        await enqueue_job(job, queue_name="gen")
        return {"ok": True}

    except Exception as e:
        try:
            if seedance_charged:
                try:
                    add_tokens(user_id, int(cost_tokens), reason="seedance_video_refund", meta={"stage": "main_exception"})
                except TypeError:
                    add_tokens(user_id, int(cost_tokens), reason="seedance_video_refund")
        except Exception:
            pass
        await tg_send_message(chat_id, f"❌ Ошибка Seedance: {e}", reply_markup=_main_menu_for(user_id))
        return {"ok": True}
    finally:
        _busy_end(int(user_id))


def _seedance_collect_summary_text(mode: str, settings: Optional[Dict[str, Any]] = None) -> str:
    settings = settings or {}
    try:
        max_images = int(settings.get("max_images") or (7 if str(settings.get("provider_kind") or "") == "seedance_kie" else 2))
    except Exception:
        max_images = 7
    try:
        max_videos = int(settings.get("max_videos") or 0)
    except Exception:
        max_videos = 0
    try:
        max_audios = int(settings.get("max_audios") or 0)
    except Exception:
        max_audios = 0
    try:
        max_total_refs = int(settings.get("max_total_refs") or max_images)
    except Exception:
        max_total_refs = max_images

    if mode == "seedance_omni":
        return (
            f"Можешь прислать refs: фото до {max_images}, видео до {max_videos}, аудио до {max_audios}, "
            f"всего до {max_total_refs}.\n"
            "Видео и аудио необязательны. Audio-only нельзя. Когда закончишь — нажми «✅ Готово»."
        )

    return (
        f"Пришли фото (1–{max_images}). "
        "Когда закончишь — нажми «✅ Готово»."
    )


def _photo_future_menu_keyboard() -> dict:
    return {
        "keyboard": [
            [{"text": "Gpt Image 2"}, {"text": "Midjourney"}],
            [{"text": "🍌 Nano Banana"}, {"text": "🍌 Nano Banana 2"}],
            [{"text": "🍌 Nano Banana Pro - NEW"}, {"text": "Seedream"}],
            [{"text": "Нейро фотосессии"}, {"text": "Апскейл"}],
            [{"text": "⬅️ Назад"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False,
        "selective": False,
    }


def _photo_gpt_image_2_menu_keyboard() -> dict:
    return {
        "keyboard": [
            [{"text": "Текст→Картинка"}],
            [{"text": "Картинка→Картинка"}],
            [{"text": "⬅️ Назад"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False,
        "selective": False,
    }


def _photo_gpt_image_2_kie_menu_keyboard() -> dict:
    return {
        "keyboard": [
            [{"text": "Текст→Картинка"}],
            [{"text": "Картинка→Картинка"}],
            [{"text": "⬅️ Назад"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False,
        "selective": False,
    }


def _photo_seedream_menu_keyboard() -> dict:
    return {
        "keyboard": [
            [{"text": "Seedream 4.5"}],
            [{"text": "Текст→Картинка"}],
            [{"text": "Картинка+Картинка"}],
            [{"text": "⬅️ Назад"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False,
        "selective": False,
    }


def _photo_upscale_menu_keyboard() -> dict:
    return {
        "keyboard": [
            [{"text": "🖼 Апскейл фото"}],
            [{"text": "🎬 Апскейл видео"}],
            [{"text": "⬅️ Назад"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False,
        "selective": False,
    }


def _topaz_photo_presets_keyboard() -> dict:
    return {
        "keyboard": [
            [{"text": "Topaz Фото • Standard • 2 токена"}],
            [{"text": "Topaz Фото • Detail • 3 токена"}],
            [{"text": "Topaz Фото • Max • 4 токена"}],
            [{"text": "⬅️ Назад"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False,
        "selective": False,
    }


def _topaz_video_presets_keyboard() -> dict:
    return {
        "keyboard": [
            [{"text": "Topaz Видео • HD Smooth • 1 токен / 5 сек"}],
            [{"text": "Topaz Видео • Full HD • 2 токена / 5 сек"}],
            [{"text": "Topaz Видео • Full HD Smooth • 3 токена / 5 сек"}],
            [{"text": "⬅️ Назад"}],
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
            [{"text": "⬅ Назад"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False,
        "selective": False,
    }

# ---------------- Telegram helpers ----------------

def _dl_keyboard(token: str) -> dict:
    return {"inline_keyboard": [[{"text": "⬇️ Скачать оригинал 2К", "callback_data": f"dl2k:{token}"}]]}
    
def _seedance_continue_kb(task_id: str) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "🎬 Продолжить видео", "callback_data": f"seedance_extend:{task_id}"}]
        ]
    }

def _ai_chat_mode_inline_kb(fable_thinking: bool = False, *, selected_model: Any = None) -> dict:
    """Inline menu for Telegram AI chat.

    The paid Fable thinking toggle is shown only when Fable is the selected
    model, so Sonnet/Opus/ChatGPT menus do not expose a setting that silently
    switches the user to another paid model.
    """
    rows = [
        [
            {"text": "💬 Claude Sonnet", "callback_data": "aichat:model:claude"},
            {"text": "💬 Claude Opus 4.7", "callback_data": "aichat:model:opus"},
        ],
        [
            {"text": f"💬 {KIE_CLAUDE_FABLE_DISPLAY_NAME}", "callback_data": "aichat:model:fable"},
            {"text": "💬 ChatGPT", "callback_data": "aichat:model:openai"},
        ],
    ]
    if _ai_chat_model_key_from_value(selected_model) == "claude_fable":
        thinking_label = "🧠 Углублённое мышление: ON" if fable_thinking else "🧠 Углублённое мышление: OFF"
        rows.append([
            {"text": f"{thinking_label} (+{KIE_CLAUDE_FABLE_THINKING_EXTRA_TOKENS} ток.)", "callback_data": "aichat:fable_thinking:toggle"},
        ])
    rows.extend([
        [
            {"text": "🪄 Промт", "callback_data": "aichat:mode:prompt"},
            {"text": "🆕 New Chat", "callback_data": "aichat:new_chat"},
        ],
    ])
    return {"inline_keyboard": rows}


def _ai_chat_model_key_from_value(value: Any) -> str:
    model = str(value or "claude").strip().lower()
    if model in ("openai", "chatgpt", "gpt"):
        return "openai"
    if model in ("opus", "claude_opus", "claude-opus", "claude-opus-4-7", "opus-4-7"):
        return "claude_opus"
    if model in ("fable", "claude_fable", "claude-fable", "claude-fable-5", "fable-5"):
        return "claude_fable"
    return "claude"


def _ai_chat_model_title(model: str) -> str:
    key = _ai_chat_model_key_from_value(model)
    if key == "openai":
        return "ChatGPT"
    if key == "claude_opus":
        return kie_claude_display_name(KIE_CLAUDE_OPUS_MODEL_ID)
    if key == "claude_fable":
        return kie_claude_display_name(KIE_CLAUDE_FABLE_MODEL_ID)
    return kie_claude_display_name(KIE_CLAUDE_MODEL_ID)


def _ai_chat_model_key(st: Dict[str, Any]) -> str:
    return _ai_chat_model_key_from_value((st or {}).get("ai_chat_model") or "claude")


def _ai_chat_model_actual(model_key: str) -> str:
    key = _ai_chat_model_key_from_value(model_key)
    if key == "openai":
        return OPENAI_CHAT_MODEL
    if key == "claude_opus":
        return normalize_kie_claude_model(KIE_CLAUDE_OPUS_MODEL_ID) or KIE_CLAUDE_OPUS_MODEL_ID
    if key == "claude_fable":
        return normalize_kie_claude_model(KIE_CLAUDE_FABLE_MODEL_ID) or KIE_CLAUDE_FABLE_MODEL_ID
    return normalize_kie_claude_model(KIE_CLAUDE_MODEL_ID) or KIE_CLAUDE_MODEL_ID


def _ai_chat_system_prompt(model_key: str) -> str:
    key = _ai_chat_model_key_from_value(model_key)
    if key == "openai":
        return DEFAULT_TEXT_SYSTEM_PROMPT
    title = _ai_chat_model_title(key)
    thinking_line = (
        "Углублённое мышление может быть включено отдельной настройкой, но внутренние рассуждения не раскрывай — сразу давай готовый ответ. "
        if key == "claude_fable"
        else "Рассуждение включено, но не раскрывай внутренние рассуждения — сразу давай готовый ответ. "
    )
    return (
        f"Ты {title} внутри AstraBot. Отвечай на русском, кратко и по делу. "
        + thinking_line
        + "Интернет выключен. Если нужны актуальные данные, честно скажи, что без интернета их нельзя проверить. "
        + "Файлы анализируй только по тексту, который передал backend. Не используй LaTeX/TeX."
    )


def _tg_chat_queue_for_model(model_key: str) -> str:
    key = _ai_chat_model_key_from_value(model_key)
    if key == "openai":
        return TG_CHAT_OPENAI_QUEUE_NAME
    if key == "claude_fable":
        return TG_CHAT_FABLE_QUEUE_NAME
    return TG_CHAT_CLAUDE_QUEUE_NAME


def _clear_ai_chat_memory_state(st: Dict[str, Any]) -> None:
    """Clear only Telegram AI-chat dialogue memory; keep the selected model and other bot modes."""
    for key in ("ai_hist", "ai_summary", "ai_pending", "ai_ts"):
        st.pop(key, None)


async def _reset_tg_ai_chat_dialogue(chat_id: int, user_id: int, st: Dict[str, Any]) -> Optional[str]:
    """Start a clean Telegram AI-chat thread for one user without touching site/workspace chat."""
    _clear_ai_chat_memory_state(st)
    st["ai_prompt"] = _new_ai_prompt_state()
    st["ts"] = _now()
    try:
        await reset_tg_chat_memory(chat_id, user_id)
    except Exception as exc:
        return str(exc)
    return None


async def _enqueue_tg_ai_chat_job(
    *,
    chat_id: int,
    user_id: int,
    text: str,
    model_key: str,
    file_meta: Optional[Dict[str, Any]] = None,
    thinking: bool = True,
    charge_tokens: int = 0,
    charge_ref_id: str = "",
) -> bool:
    """Queue a Telegram GPT/Claude chat request so main.py does not wait for the model."""
    if not CHAT_WORKER_ENABLED:
        return False

    status_message_id: Optional[int] = None
    try:
        status_message_id = await tg_send_message(chat_id, "⏳ Думаю...")
    except Exception:
        status_message_id = None

    normalized_model_key = _ai_chat_model_key_from_value(model_key)
    job: Dict[str, Any] = {
        "job_id": f"tg_ai_chat_{uuid4().hex}",
        "kind": "tg_ai_chat",
        "chat_id": int(chat_id),
        "user_id": int(user_id),
        "text": str(text or ""),
        "model_key": normalized_model_key,
        "model": _ai_chat_model_actual(normalized_model_key),
        "system_prompt": _ai_chat_system_prompt(normalized_model_key),
        "thinking": bool(thinking),
        "charge_tokens": int(charge_tokens or 0),
        "charge_ref_id": str(charge_ref_id or ""),
        "refund_reason": "claude_fable_chat_refund" if normalized_model_key == "claude_fable" else "",
        # Показываем inline-меню ИИ-чата под ответом, чтобы New Chat всегда был под рукой.
        # Reply-клавиатура главного меню при этом не ломается — Telegram оставляет её внизу.
        "reply_markup": _ai_chat_mode_inline_kb(bool(thinking) if normalized_model_key == "claude_fable" else False, selected_model=normalized_model_key),
    }
    if status_message_id:
        job["status_message_id"] = int(status_message_id)
    if file_meta:
        job["file"] = file_meta

    try:
        await enqueue_job(job, queue_name=_tg_chat_queue_for_model(model_key))
        return True
    except Exception as e:
        try:
            await tg_send_message(
                chat_id,
                f"❌ Очередь чата недоступна: {e}",
                reply_markup=_main_menu_for(user_id),
            )
        except Exception:
            pass
        return False


async def _enqueue_tg_fable_image_chat_or_notify(
    *,
    chat_id: int,
    user_id: int,
    st: Dict[str, Any],
    file_id: str,
    filename: str = "telegram_photo.jpg",
    mime_type: str = "image/jpeg",
    size_bytes: int = 0,
    prompt: str = "",
) -> bool:
    """Handle Telegram photo/image-document through Claude Fable when Fable is selected.

    Returns True when the message was handled (queued, rejected for balance, or
    failed with a user-facing error). Returns False for non-Fable models so the
    legacy OpenAI vision path can continue unchanged.
    """
    model_key = _ai_chat_model_key(st)
    if not _is_fable_chat_model_key(model_key):
        return False

    charge_tokens = _tg_fable_chat_cost_tokens(st, has_files=True)
    charge_ref_id = await _tg_charge_fable_chat_or_notify(chat_id=chat_id, user_id=user_id, st=st, has_files=True)
    if not charge_ref_id:
        st["ts"] = _now()
        return True

    queued = await _enqueue_tg_ai_chat_job(
        chat_id=chat_id,
        user_id=user_id,
        text=(prompt or VISION_DEFAULT_USER_PROMPT),
        model_key=model_key,
        file_meta={
            "filename": filename or "telegram_photo.jpg",
            "file_id": str(file_id or ""),
            "mime_type": mime_type or "image/jpeg",
            "size_bytes": int(size_bytes or 0),
            "kind": "image",
        },
        thinking=_tg_fable_thinking_enabled(st),
        charge_tokens=charge_tokens,
        charge_ref_id=charge_ref_id,
    )
    if queued:
        return True

    try:
        add_tokens(user_id, int(charge_tokens), reason="claude_fable_chat_refund", ref_id=charge_ref_id, meta={"stage": "enqueue_failed", "source": "telegram_photo"})
    except Exception:
        pass
    await tg_send_message(chat_id, "❌ Не удалось поставить Claude Fable 5 в очередь. Проверь REDIS_URL и worker_chat.py.", reply_markup=_main_menu_for(user_id))
    return True


def _ai_prompt_root_inline_kb() -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "🖼 Фото", "callback_data": "aichat:prompt_root:photo"},
                {"text": "🎬 Видео", "callback_data": "aichat:prompt_root:video"},
            ],
            [
                {"text": "🎵 Музыка", "callback_data": "aichat:prompt_root:music"},
                {"text": "✨ Универсальный", "callback_data": "aichat:prompt_root:universal"},
            ],
            [
                {"text": "↩️ ИИ-меню", "callback_data": "aichat:mode:menu"},
            ],
        ]
    }


def _ai_prompt_video_inline_kb() -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "Veo", "callback_data": "aichat:prompt_video:veo"},
                {"text": "Kling", "callback_data": "aichat:prompt_video:kling"},
            ],
            [
                {"text": "Seedance", "callback_data": "aichat:prompt_video:seedance"},
                {"text": "Sora", "callback_data": "aichat:prompt_video:sora"},
            ],
            [
                {"text": "✨ Универсальный", "callback_data": "aichat:prompt_video:universal"},
            ],
            [
                {"text": "↩️ Разделы", "callback_data": "aichat:prompt_reset"},
                {"text": "↩️ ИИ-меню", "callback_data": "aichat:mode:menu"},
            ],
        ]
    }


def _new_ai_prompt_state() -> Dict[str, Any]:
    return {
        "step": "choose_root",
        "root": None,
        "provider": None,
        "images": [],
        "last_prompt": None,
    }


def _ai_prompt_title(pb: Dict[str, Any]) -> str:
    root = str(pb.get("root") or "").strip()
    provider = str(pb.get("provider") or "").strip()
    if root == "video" and provider:
        return f"Видео → {provider}"
    return {
        "photo": "Фото",
        "video": "Видео",
        "music": "Музыка",
        "universal": "Универсальный",
    }.get(root, "Промт")


def _ai_prompt_tools_inline_kb(pb: Optional[Dict[str, Any]] = None) -> dict:
    img_count = len((pb or {}).get("images") or [])
    clear_label = f"🗑 Очистить фото ({img_count})" if img_count else "🗑 Очистить фото"
    return {
        "inline_keyboard": [
            [
                {"text": clear_label, "callback_data": "aichat:prompt_clear"},
                {"text": "🔁 Выбрать заново", "callback_data": "aichat:prompt_reset"},
            ],
            [
                {"text": "↩️ ИИ-меню", "callback_data": "aichat:mode:menu"},
            ],
        ]
    }


def _ai_prompt_waiting_text(pb: Dict[str, Any]) -> str:
    title = _ai_prompt_title(pb)
    img_count = len(pb.get("images") or [])
    extra = ""
    if str(pb.get("root") or "") == "video" and str(pb.get("provider") or "") == "seedance":
        extra = (
            "\n\nДля Seedance можешь прислать несколько фото-референсов. "
            "Я соберу промт со структурой @image1, @image2 и т.д. по порядку загруженных фото."
        )
    elif img_count:
        extra = "\n\nФото уже сохранены в контексте — можешь просто прислать идею текстом."

    return (
        f"🪄 Режим «{title}».\n\n"
        "Теперь пришли идею текстом. Можно дополнительно отправить фото-референсы, и я учту их в промте."
        f"\nСейчас фото в контексте: {img_count}/{PROMPT_BUILDER_MAX_IMAGES}."
        f"{extra}"
    )


def _ai_prompt_system_prompt(root: str, provider: str, image_count: int) -> str:
    provider_line = f"Целевой движок: {provider}." if provider else ""
    seedance_rules = (
        "Для Seedance: если приложены изображения, сначала выдай строки вида '@image1 = роль референса', "
        "'@image2 = роль референса' и т.д. строго по порядку приложенных фото, затем пустую строку и финальный промт. "
        "Используй только те @imageN, которые реально есть. Если фото одно — используй только @image1. "
        "Роли должны быть короткими и практичными: character reference, outfit reference, style reference, environment reference, product reference и т.п. "
        "Финальный Seedance-промт должен естественно ссылаться на эти плейсхолдеры. "
        "Не добавляй markdown, заголовки, комментарии или пояснения."
    )
    return (
        "Ты senior prompt engineer для генеративных моделей. "
        "Твоя задача — вернуть один готовый, сильный, практический промт, который можно сразу вставлять в генератор. "
        "Пиши на языке пользователя. Не веди диалог, не задавай вопросов, не добавляй вводные фразы вроде 'вот промт'. "
        f"Тип запроса: {root}. {provider_line} "
        "Если приложены изображения, проанализируй их и используй как референсы: внешность, одежду, композицию, свет, стиль, предметы, фактуру и настроение. "
        "Не выдумывай деталей, которых нет. Если пользователь просит изменить сцену, сохрани важные элементы референса и адаптируй их под задачу. "
        "Для фото-промтов делай акцент на композиции, свете, стиле, деталях и визуальном результате. "
        "Для видео-промтов описывай сцену, действие, движение камеры, ритм, физику движения, свет и атмосферу. "
        "Для музыки формируй prompt как задание для AI-генерации трека: жанр, настроение, темп, инструменты, вокал, структура, энергия. "
        "Для универсального режима делай нейтральный, мощный промт без жёсткой привязки к одной модели. "
        "Для Veo — cinematic natural language, для Kling — акцент на motion, realism и continuity, для Sora — cinematic scene prompt с богатой визуальной детализацией. "
        f"Изображений приложено: {image_count}. "
        + (seedance_rules if provider == "seedance" else "Верни только финальный промт без пояснений и без markdown.")
    )


async def _ai_prompt_generate(pb: Dict[str, Any], user_text: str) -> str:
    root = str(pb.get("root") or "universal").strip().lower() or "universal"
    provider = str(pb.get("provider") or "").strip().lower()
    images = list(pb.get("images") or [])[:PROMPT_BUILDER_MAX_IMAGES]

    if provider == "universal":
        provider = ""

    system_prompt = _ai_prompt_system_prompt(root=root, provider=provider, image_count=len(images))
    user_payload = (
        f"Запрос пользователя:\n{(user_text or '').strip() or 'Собери лучший готовый промт по задаче и референсам.'}\n\n"
        f"Тип: {root}.\n"
        + (f"Движок: {provider}.\n" if provider else "")
        + (
            "Если приложены изображения, используй их как реальные референсы и встрои важные детали в итоговый промт.\n"
            if images else
            "Изображений нет — работай только по тексту.\n"
        )
    )

    out = await openai_chat_answer(
        user_text=user_payload,
        system_prompt=system_prompt,
        image_bytes=None,
        image_bytes_list=images if images else None,
        temperature=0.5,
        max_completion_tokens=1200,
        model=PROMPT_BUILDER_MODEL,
    )
    return (out or "").strip()

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

async def tg_send_audio_bytes(
    chat_id: int,
    audio_bytes: bytes,
    filename: str = "tts.mp3",
    caption: str | None = None,
    reply_markup: dict | None = None,
):
    """Send MP3 bytes as Telegram audio."""
    if not TELEGRAM_BOT_TOKEN:
        return
    if not audio_bytes:
        raise RuntimeError("Empty audio bytes for sendAudio")

    files = {"audio": (filename, audio_bytes, "audio/mpeg")}
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)

    async with httpx.AsyncClient(timeout=240) as client:
        r = await client.post(f"{TELEGRAM_API_BASE}/sendAudio", data=data, files=files)

    # Если Telegram вернул ошибку — поднимем исключение
    try:
        j = r.json()
        if isinstance(j, dict) and not j.get("ok", False):
            raise RuntimeError(f"Telegram sendAudio error: {j}")
    except Exception:
        if r.status_code >= 400:
            raise RuntimeError(f"Telegram sendAudio HTTP {r.status_code}: {r.text[:1200]}")

async def tg_send_message(chat_id: int, text: str, reply_markup: Optional[dict] = None) -> Optional[int]:
    if not TELEGRAM_BOT_TOKEN:
        return None
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{TELEGRAM_API_BASE}/sendMessage", json=payload)
    try:
        j = r.json()
        if isinstance(j, dict) and j.get("ok") and j.get("result"):
            return int((j.get("result") or {}).get("message_id") or 0) or None
    except Exception:
        pass
    return None
        

async def _admin_broadcast_send(admin_chat_id: int, text: str) -> Tuple[int, int]:
    """
    Рассылка текста по всем пользователям из Supabase таблицы bot_users (telegram_user_id).
    Возвращает (ok_count, fail_count).
    """
    if sb is None:
        await tg_send_message(admin_chat_id, "❌ Supabase не настроен (sb=None).")
        return (0, 0)

    page_size = 1000
    start = 0
    user_ids: List[int] = []
    seen_ids = set()

    try:
        while True:
            resp = (
                sb.table("bot_users")
                .select("telegram_user_id")
                .range(start, start + page_size - 1)
                .execute()
            )
            rows = getattr(resp, "data", None) or []
            if not rows:
                break

            for r in rows:
                try:
                    raw_uid = r.get("telegram_user_id")
                    if raw_uid is None:
                        continue
                    uid = int(raw_uid)
                    if uid in seen_ids:
                        continue
                    seen_ids.add(uid)
                    user_ids.append(uid)
                except Exception:
                    continue

            if len(rows) < page_size:
                break
            start += page_size
    except Exception as e:
        await tg_send_message(admin_chat_id, f"❌ Не смог получить список пользователей: {e}")
        return (0, 0)

    if not user_ids:
        await tg_send_message(admin_chat_id, "⚠️ Пользователей для рассылки нет (bot_users пуст).")
        return (0, 0)

    ok = 0
    fail = 0

    # Рассылаем с небольшим интервалом, чтобы не упереться в лимиты Telegram
    for uid in user_ids:
        try:
            await tg_send_message(uid, text)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)

    return (ok, fail)

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


def _telegram_api_assert_ok(response: httpx.Response, method: str) -> dict:
    try:
        payload = response.json()
    except Exception:
        payload = {}
    if response.status_code >= 400 or not (isinstance(payload, dict) and payload.get("ok")):
        detail = payload.get("description") if isinstance(payload, dict) else None
        detail = detail or response.text[:600] or f"Telegram {method} failed with HTTP {response.status_code}"
        raise RuntimeError(detail)
    return payload


async def tg_edit_message_media_photo(chat_id: int, message_id: int, image_bytes: bytes, caption: Optional[str] = None, reply_markup: Optional[dict] = None):
    """
    Заменяет фото в существующем сообщении (эффект: был силуэт/превью → стало финальное изображение).
    Бросает исключение, если Telegram не принял editMessageMedia — вызывающий код сможет сделать fallback.
    """
    if not TELEGRAM_BOT_TOKEN:
        return
    if not image_bytes:
        raise RuntimeError("Empty image bytes for editMessageMedia")
    media = {"type": "photo", "media": "attach://photo"}
    if caption:
        media["caption"] = caption

    try:
        ext, mime = _detect_image_type(image_bytes)
    except Exception:
        ext, mime = "png", "image/png"
    files = {"photo": (f"image.{ext or 'png'}", image_bytes, mime or "image/png")}
    data = {"chat_id": str(chat_id), "message_id": str(message_id), "media": json.dumps(media, ensure_ascii=False)}
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)

    async with httpx.AsyncClient(timeout=180) as client:
        r = await client.post(f"{TELEGRAM_API_BASE}/editMessageMedia", data=data, files=files)
    _telegram_api_assert_ok(r, "editMessageMedia")


async def tg_edit_message_media_photo_url(chat_id: int, message_id: int, image_url: str, caption: Optional[str] = None, reply_markup: Optional[dict] = None):
    """Заменяет фото в существующем сообщении по публичной URL без хранения bytes в RAM."""
    if not TELEGRAM_BOT_TOKEN:
        return
    url = str(image_url or "").strip()
    if not url:
        raise RuntimeError("Empty image URL for editMessageMedia")
    media = {"type": "photo", "media": url}
    if caption:
        media["caption"] = caption
    payload = {"chat_id": int(chat_id), "message_id": int(message_id), "media": media}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{TELEGRAM_API_BASE}/editMessageMedia", json=payload)
    _telegram_api_assert_ok(r, "editMessageMedia")


async def tg_send_photo_url(chat_id: int, image_url: str, caption: Optional[str] = None, reply_markup: Optional[dict] = None) -> Optional[int]:
    if not TELEGRAM_BOT_TOKEN:
        return None
    payload: Dict[str, Any] = {"chat_id": int(chat_id), "photo": str(image_url or "").strip()}
    if caption:
        payload["caption"] = caption
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{TELEGRAM_API_BASE}/sendPhoto", json=payload)
    data = _telegram_api_assert_ok(r, "sendPhoto")
    try:
        return int(((data.get("result") or {}) if isinstance(data.get("result"), dict) else {}).get("message_id") or 0) or None
    except Exception:
        return None


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


# STT / распознавание Telegram voice вынесено в worker_redactor.py.
# main.py больше не скачивает голосовые, не запускает ffmpeg и не ждёт OpenAI STT.
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

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()

async def elevenlabs_tts_mp3_bytes(
    text: str,
    voice_id: str,
    model_id: str = "eleven_multilingual_v2",
) -> bytes:
    if not ELEVENLABS_API_KEY:
        raise RuntimeError("ELEVENLABS_API_KEY не задан в переменных окружения.")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
    }
    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
        },
    }

    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(url, headers=headers, json=payload)

    if r.status_code >= 400:
        raise RuntimeError(f"ElevenLabs TTS ({r.status_code}): {r.text[:2000]}")

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

CLAUDE_TEXT_SYSTEM_PROMPT = (
    "Ты Claude Sonnet 4.6 внутри AstraBot. Отвечай на русском, кратко и по делу. "
    "Рассуждение включено, но не раскрывай внутренние рассуждения — сразу давай готовый ответ. "
    "Интернет выключен. Если нужны актуальные данные, честно скажи, что без интернета их нельзя проверить. "
    "Файлы анализируй только по тексту, который передал backend. Не используй LaTeX/TeX."
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
    max_tokens: Optional[int] = 800,
    max_completion_tokens: Optional[int] = None,
    history: Optional[List[Dict[str, str]]] = None,
    model: str = "gpt-4o-mini",
    image_bytes_list: Optional[List[bytes]] = None,
) -> str:
    if not OPENAI_API_KEY:
        return "OPENAI_API_KEY не задан в переменных окружения."

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    token_limit = max_completion_tokens if max_completion_tokens is not None else max_tokens

    images_to_send: List[bytes] = []
    if image_bytes_list:
        images_to_send.extend([bytes(b) for b in image_bytes_list if isinstance(b, (bytes, bytearray))])
    elif image_bytes is not None:
        images_to_send.append(image_bytes)

    if images_to_send:
        user_content: List[Dict[str, Any]] = []
        if user_text:
            user_content.append({"type": "text", "text": user_text})
        for img in images_to_send[:PROMPT_BUILDER_MAX_IMAGES]:
            _ext, mime = _detect_image_type(img)
            b64 = base64.b64encode(img).decode("utf-8")
            user_content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"}
            })

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": temperature,
        }
        if token_limit is not None:
            payload["max_completion_tokens"] = token_limit
    else:
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
            "model": model,
            "messages": msgs,
            "temperature": temperature,
        }
        if token_limit is not None:
            payload["max_completion_tokens"] = token_limit

    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json=payload,
        )

    if r.status_code != 200:
        return f"Ошибка OpenAI ({r.status_code}): {r.text[:1600]}"

    data = r.json()
    return (data["choices"][0]["message"]["content"] or "").strip() or "Пустой ответ от модели."


def _looks_like_heif_image(b: bytes) -> bool:
    if not b or len(b) < 12:
        return False
    if b[4:8] != b"ftyp":
        return False
    brand = b[8:12].lower()
    return brand in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}


def _detect_image_type(b: bytes) -> Tuple[str, str]:
    if not b:
        return ("jpg", "image/jpeg")
    if b.startswith(b"\xFF\xD8\xFF"):
        return ("jpg", "image/jpeg")
    if b.startswith(b"\x89PNG\r\n\x1a\n"):
        return ("png", "image/png")
    if b.startswith(b"RIFF") and len(b) >= 12 and b[8:12] == b"WEBP":
        return ("webp", "image/webp")
    if _looks_like_heif_image(b):
        return ("heic", "image/heic")
    return ("jpg", "image/jpeg")


def _normalize_openai_image_input(image_bytes: bytes, *, source_label: str = "image") -> Tuple[bytes, str, str]:
    """Normalize user/reference image bytes before sending them to OpenAI Images Edit.

    Fixes common phone uploads: EXIF orientation, CMYK/P/16-bit modes, and returns
    a provider-safe JPEG/PNG payload. HEIC/HEIF is handled through system ffmpeg
    instead of pip HEIC wheels, because Render can fail while compiling libheif wrappers.
    """
    if not isinstance(image_bytes, (bytes, bytearray)) or not image_bytes:
        raise RuntimeError(f"{source_label}: empty image bytes")

    raw = bytes(image_bytes)

    def _pil_to_openai_safe(payload: bytes) -> Tuple[bytes, str, str]:
        from PIL import Image, ImageOps  # type: ignore

        with Image.open(BytesIO(payload)) as img:
            img.load()
            img = ImageOps.exif_transpose(img)

            has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
            if has_alpha:
                if img.mode != "RGBA":
                    img = img.convert("RGBA")
                out = BytesIO()
                img.save(out, format="PNG", optimize=True)
                return out.getvalue(), "png", "image/png"

            if img.mode != "RGB":
                img = img.convert("RGB")

            out = BytesIO()
            img.save(out, format="JPEG", quality=95, optimize=True, progressive=True)
            return out.getvalue(), "jpg", "image/jpeg"

    def _heic_to_jpeg_with_ffmpeg(payload: bytes) -> bytes:
        import subprocess

        in_path = None
        out_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".heic") as src:
                src.write(payload)
                in_path = src.name
            with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as dst:
                out_path = dst.name

            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                in_path,
                "-frames:v",
                "1",
                "-vf",
                "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                "-q:v",
                "2",
                out_path,
            ]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45)
            if proc.returncode != 0:
                err = proc.stderr.decode("utf-8", "ignore")[:1000]
                raise RuntimeError(f"ffmpeg HEIC convert failed: {err}")
            with open(out_path, "rb") as f:
                converted = f.read()
            if not converted:
                raise RuntimeError("ffmpeg HEIC convert returned empty output")
            return converted
        finally:
            for path in (in_path, out_path):
                if path:
                    try:
                        os.remove(path)
                    except Exception:
                        pass

    try:
        if _looks_like_heif_image(raw):
            raw = _heic_to_jpeg_with_ffmpeg(raw)
        return _pil_to_openai_safe(raw)
    except Exception as e:
        ext, mime = _detect_image_type(raw)
        hint = ""
        if ext == "heic":
            hint = " Проверь, что ffmpeg установлен в сервисе Render и поддерживает HEIC/HEIF."
        raise RuntimeError(
            f"{source_label}: не удалось нормализовать изображение для GPT Image 2.0 "
            f"(detected={ext}/{mime}, first_bytes={raw[:16].hex()}).{hint} Ошибка: {e}"
        ) from e




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
    size: Optional[str] = None,
    mask_png_bytes: Optional[bytes] = None,
    *,
    source_image_url: Optional[str] = None,
    source_image_urls: Optional[List[str]] = None,
    model: Optional[str] = None,
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
            "model": (model or ARK_IMAGE_MODEL),
            "prompt": prompt,
            "response_format": "url",
            # ModelArk expects list for multi-image fusion; single image works too
            "image": img_list,
            "sequential_image_generation": "disabled",
            "stream": False,
            "watermark": bool(ARK_WATERMARK),
        }
        if size:
            payload["size"] = size

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
            "model": (model or ARK_IMAGE_MODEL),
            "prompt": prompt,
            "response_format": "url",
            "sequential_image_generation": "disabled",
            "stream": "false",
            "watermark": "true" if ARK_WATERMARK else "false",
        }
        if size:
            data["size"] = size

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



async def ark_text_to_image(prompt: str, size: str = "2K", model: Optional[str] = None) -> bytes:
    """Text-to-image via ModelArk (Seedream) using /images/generations."""
    url = f"{ARK_BASE_URL.rstrip('/')}/images/generations"
    headers = {"Authorization": f"Bearer {ARK_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": (model or ARK_IMAGE_MODEL),
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


async def openai_generate_image_v2(
    prompt: str,
    size: str = "1024x1024",
) -> bytes:
    """Text-to-image via OpenAI Images API (gpt-image-2)."""
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY не задан в переменных окружения.")

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {"model": "gpt-image-2", "prompt": prompt, "size": size, "n": 1}

    async with httpx.AsyncClient(timeout=300) as client:
        r = await client.post(
            "https://api.openai.com/v1/images/generations",
            headers=headers,
            json=payload,
        )

    if r.status_code != 200:
        raise RuntimeError(f"Ошибка Images Generations API ({r.status_code}): {r.text[:2000]}")

    resp = r.json()
    b64_img = (resp.get("data") or [{}])[0].get("b64_json")
    if not b64_img:
        raise RuntimeError("Images Generations API вернул ответ без b64_json.")
    return base64.b64decode(b64_img)


async def openai_edit_image_v2(
    source_image_bytes: Union[bytes, List[bytes]],
    prompt: str,
    size: str = "1024x1024",
    mask_png_bytes: Optional[bytes] = None,
) -> bytes:
    """Image-to-image via OpenAI Images API (gpt-image-2).

    Supports either a single source image or multiple reference/source images.
    When multiple images are provided, they are sent as repeated `image[]` fields.
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY не задан в переменных окружения.")

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}

    image_items = source_image_bytes if isinstance(source_image_bytes, list) else [source_image_bytes]
    normalized_items = [item for item in image_items if isinstance(item, (bytes, bytearray)) and item]
    if not normalized_items:
        raise RuntimeError("openai_edit_image_v2 requires at least one source image.")

    files: List[Tuple[str, Tuple[str, bytes, str]]] = []
    for idx, raw in enumerate(normalized_items, start=1):
        safe_bytes, ext, mime = _normalize_openai_image_input(bytes(raw), source_label=f"GPT Image 2.0 source_{idx}")
        files.append(("image[]", (f"source_{idx}.{ext}", safe_bytes, mime)))
    if mask_png_bytes:
        safe_mask, mask_ext, mask_mime = _normalize_openai_image_input(mask_png_bytes, source_label="GPT Image 2.0 mask")
        files.append(("mask", (f"mask.{mask_ext}", safe_mask, mask_mime)))

    data = {"model": "gpt-image-2", "prompt": prompt, "size": size, "n": "1"}

    async with httpx.AsyncClient(timeout=300) as client:
        r = await client.post("https://api.openai.com/v1/images/edits", headers=headers, data=data, files=files)

    if r.status_code != 200:
        raise RuntimeError(f"Ошибка Images Edit API ({r.status_code}): {r.text[:2000]}")

    resp = r.json()
    b64_img = (resp.get("data") or [{}])[0].get("b64_json")
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
    Phase-1 safe hardening:
    - keeps the current webhook URLs unchanged;
    - does not require a secret unless YOOKASSA_WEBHOOK_REQUIRE_SECRET=1;
    - verifies payment_id through YooKassa API before any tokens/subscription are granted;
    - avoids logging full webhook payload / receipt / customer data.
    """
    if YOOKASSA_WEBHOOK_REQUIRE_SECRET:
        auth = (request.headers.get("authorization") or "").strip()
        header_token = (request.headers.get("x-webhook-token") or "").strip()
        query_token = (request.query_params.get("token") or request.query_params.get("secret") or "").strip()
        expected = str(YOOKASSA_WEBHOOK_SECRET or "").strip()
        ok = False
        if expected:
            if auth.lower().startswith("bearer "):
                ok = hmac.compare_digest(auth.split(" ", 1)[1].strip(), expected)
            if header_token and hmac.compare_digest(header_token, expected):
                ok = True
            if query_token and hmac.compare_digest(query_token, expected):
                ok = True
        if not ok:
            return Response(status_code=401, content="unauthorized")

    try:
        payload = await request.json()
    except Exception:
        return Response(status_code=400, content="bad json")

    event = (payload.get("event") or payload.get("type") or "").strip() if isinstance(payload, dict) else ""
    obj = payload.get("object") if isinstance(payload, dict) else None
    if not isinstance(obj, dict):
        return {"ok": True}

    payment_id = (obj.get("id") or "").strip()
    if not payment_id:
        return {"ok": True}

    # We process only successful payment notifications. The final status is still rechecked below via YooKassa API.
    webhook_status = (obj.get("status") or "").strip()
    if webhook_status != "succeeded" and event != "payment.succeeded":
        return {"ok": True}

    _yk_cleanup_processed()
    if payment_id in _YK_PROCESSED:
        return {"ok": True}
    if payment_id in _YK_PROCESSING:
        return {"ok": True}
    _YK_PROCESSING[payment_id] = time.time()

    uid = 0
    tokens = 0
    payment_type = ""
    plan_code = ""
    is_subscription_payment = False
    reason = "yookassa_topup"
    payment_ref_id = str(uuid5(NAMESPACE_URL, f"nabex:yookassa:{payment_id}"))
    amount_rub = 0.0
    plan: Optional[Dict[str, Any]] = None

    try:
        verified = await fetch_yookassa_payment(payment_id)
        verified_id = (verified.get("id") or "").strip()
        status = (verified.get("status") or "").strip()
        paid = bool(verified.get("paid"))

        if verified_id != payment_id:
            raise RuntimeError("YooKassa payment id mismatch")

        # Do not trust the webhook body for money/status. Use only the object fetched from YooKassa.
        if status != "succeeded" or not paid:
            _yk_mark_processed(payment_id)
            return {"ok": True}

        amount_obj = verified.get("amount") if isinstance(verified, dict) else {}
        if not isinstance(amount_obj, dict):
            amount_obj = {}
        currency = str(amount_obj.get("currency") or "").strip().upper()
        if currency and currency != "RUB":
            if ADMIN_IDS:
                try:
                    await tg_send_message(next(iter(ADMIN_IDS)), f"⚠️ YooKassa webhook: currency mismatch. payment_id={payment_id} currency={currency}")
                except Exception:
                    pass
            _yk_mark_processed(payment_id)
            return {"ok": True}
        try:
            amount_rub = float(amount_obj.get("value") or 0)
        except Exception:
            amount_rub = 0.0

        md = verified.get("metadata") or {}
        if not isinstance(md, dict):
            md = {}

        try:
            uid = int(md.get("user_id") or 0)
            tokens = int(md.get("tokens") or 0)
        except Exception:
            uid = 0
            tokens = 0

        payment_type = str(md.get("payment_type") or "").strip().lower()
        plan_code = str(md.get("plan_code") or "").strip().lower()
        is_subscription_payment = payment_type == "subscription" or plan_code in PUBLIC_SUBSCRIPTION_PLAN_CODES
        reason = "yookassa_subscription" if is_subscription_payment else "yookassa_topup"

        # If metadata is missing, do nothing and do not leak payload/customer data to logs.
        if uid <= 0 or tokens <= 0:
            if ADMIN_IDS:
                try:
                    admin_id = next(iter(ADMIN_IDS))
                    await tg_send_message(admin_id, f"⚠️ YooKassa webhook: missing metadata user_id/tokens. payment_id={payment_id}")
                except Exception:
                    pass
            _yk_mark_processed(payment_id)
            return {"ok": True}

        tokens_already_granted = False
        try:
            tokens_already_granted = ledger_ref_exists(reason=reason, ref_id=payment_ref_id)
        except Exception:
            tokens_already_granted = False

        subscription_already_applied = False
        if is_subscription_payment:
            subscription_already_applied = _yookassa_subscription_payment_already_applied(uid, payment_id)
            if tokens_already_granted and subscription_already_applied:
                _yk_mark_processed(payment_id)
                return {"ok": True}
        elif tokens_already_granted:
            _yk_mark_processed(payment_id)
            return {"ok": True}

        ensure_user_row(uid)

        # Apply subscription first. If token credit fails afterwards, YooKassa retry will see
        # the subscription payment_id and will not extend/activate it a second time.
        if is_subscription_payment and not subscription_already_applied:
            plan = _public_subscription_plan_for_tg(plan_code)
            if not plan:
                raise RuntimeError(f"Unknown or unavailable subscription plan: {plan_code}")
            duration_days = int(float(md.get("duration_days") or plan.get("duration_days") or 30))
            current_sub = get_current_subscription(uid)
            if bool(current_sub.get("is_active")) and str(current_sub.get("plan_code") or "").strip().lower() == plan_code:
                extend_user_subscription(
                    uid,
                    days=duration_days,
                    source="yookassa",
                    payment_id=payment_id,
                    comment=f"YooKassa subscription payment {payment_id}",
                )
            else:
                set_user_subscription(
                    uid,
                    plan_code,
                    duration_days=duration_days,
                    source="yookassa",
                    payment_id=payment_id,
                    comment=f"YooKassa subscription payment {payment_id}",
                    meta={"payment_id": payment_id, "amount_rub": amount_rub, "tokens_granted": tokens},
                )

        if not tokens_already_granted:
            add_tokens(
                uid,
                tokens,
                reason=reason,
                ref_id=payment_ref_id,
                meta={
                    "payment_id": payment_id,
                    "event": event,
                    "status": status,
                    "amount_rub": amount_rub,
                    "provider": "yookassa",
                    "payment_type": payment_type or ("subscription" if is_subscription_payment else "topup"),
                    "plan_code": plan_code if is_subscription_payment else "",
                },
            )

        _yk_mark_processed(payment_id)

    except Exception as e:
        _YK_PROCESSING.pop(payment_id, None)
        if ADMIN_IDS:
            try:
                admin_id = next(iter(ADMIN_IDS))
                await tg_send_message(admin_id, f"❌ YooKassa начисление упало: {e}\nuser={uid} payment_id={payment_id}")
            except Exception:
                pass
        # Return non-2xx so YooKassa can retry the webhook instead of silently losing the payment.
        return Response(status_code=500, content="processing error")

    # Non-critical side effects: do not retry financial operations only because a notification failed.
    try:
        await _enqueue_partner_topup_event(
            user_id=uid,
            payment_id=payment_id,
            amount_rub=amount_rub,
            tokens=tokens,
            provider="yookassa",
            meta={"event": event, "status": "succeeded", "payment_type": payment_type or ("subscription" if is_subscription_payment else "topup")},
        )
    except Exception as e:
        if ADMIN_IDS:
            try:
                admin_id = next(iter(ADMIN_IDS))
                await tg_send_message(admin_id, f"⚠️ YooKassa partner event не записался: {e}\nuser={uid} payment_id={payment_id}")
            except Exception:
                pass

    try:
        bal = int(get_balance(uid) or 0)
        if is_subscription_payment:
            if plan is None:
                try:
                    plan = _public_subscription_plan_for_tg(plan_code)
                except Exception:
                    plan = None
            plan_name = str((plan or {}).get("name") or plan_code.title())
            await tg_send_message(uid, f"✅ Тариф {plan_name} подключён!\nНачислено: +{tokens} токенов\nБаланс: {bal}", reply_markup=_help_menu_for(uid))
        else:
            await tg_send_message(uid, f"✅ Оплата ЮKassa прошла!\nНачислено: +{tokens} токенов\nБаланс: {bal}", reply_markup=_help_menu_for(uid))
    except Exception:
        pass

    return {"ok": True}

@app.post("/internal/dl2k")
async def internal_dl2k(request: Request):
    """
    Worker -> main: register bytes for inline callback "dl2k:<token>".
    Stores bytes in in-memory per-user state (same mechanism as _dl_* helpers).
    Security: set INTERNAL_API_KEY env and send it as header X-Internal-Key from the worker.
    """
    if INTERNAL_API_KEY:
        key = (request.headers.get("x-internal-key") or "").strip()
        if key != INTERNAL_API_KEY:
            return Response(status_code=401)

    payload = await request.json()
    chat_id = int(payload.get("chat_id") or 0)
    user_id = int(payload.get("user_id") or 0)
    bytes_b64 = (payload.get("bytes_b64") or "").strip()

    if not (chat_id and user_id and bytes_b64):
        return Response(status_code=400)

    try:
        img_bytes = base64.b64decode(bytes_b64)
    except Exception:
        return Response(status_code=400)

    token = _dl_init_slot(chat_id, user_id)
    _dl_set_bytes(chat_id, user_id, token, img_bytes)
    return {"ok": True, "token": token}


@app.post("/internal/midjourney/register")
async def internal_midjourney_register(request: Request):
    """
    Worker -> main: register Midjourney 2x2 grid + 4 original images for Telegram inline gallery.
    Stores session in the same per-user state used by inline download buttons.
    """
    if not INTERNAL_API_KEY:
        logging.error("/internal/midjourney/register blocked: INTERNAL_API_KEY is not set")
        return Response(status_code=503, content="INTERNAL_API_KEY is required")
    key = (request.headers.get("x-internal-key") or "").strip()
    if key != INTERNAL_API_KEY:
        return Response(status_code=401)

    payload = await request.json()
    chat_id = int(payload.get("chat_id") or 0)
    user_id = int(payload.get("user_id") or 0)
    if not (chat_id and user_id):
        return Response(status_code=400)

    token = _midjourney_session_token()
    images: List[Dict[str, Any]] = []
    for item in (payload.get("images") or [])[:4]:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        ext = str(item.get("ext") or "jpg").strip().lower().lstrip(".") or "jpg"
        images.append({"url": url, "ext": ext})

    grid_url = str(payload.get("grid_url") or "").strip()

    if not images or not grid_url:
        return Response(status_code=400)

    session = {
        "ts": _now(),
        "chat_id": int(chat_id),
        "user_id": int(user_id),
        "provider_task_id": str(payload.get("provider_task_id") or "").strip(),
        "source_task_id": str(payload.get("source_task_id") or "").strip(),
        "prompt": str(payload.get("prompt") or ""),
        "run_prompt": str(payload.get("run_prompt") or ""),
        "model": normalize_midjourney_model(payload.get("model") or "midjourney-v7"),
        "action": str(payload.get("action") or "generate").strip().lower(),
        "selected_image_no": int(payload.get("selected_image_no") or 0),
        "variation_type": str(payload.get("variation_type") or "subtle").strip().lower(),
        "settings": payload.get("settings") if isinstance(payload.get("settings"), dict) else {},
        "aspect_ratio": str(payload.get("aspect_ratio") or "1:1").strip() or "1:1",
        "speed_mode": normalize_midjourney_speed_mode(payload.get("speed_mode") or "fast", model=payload.get("model") or "midjourney-v7"),
        "charge_tokens": int(payload.get("charge_tokens") or 0),
        "images": images[:4],
        "grid_url": grid_url,
    }
    _midjourney_cache_session(chat_id, user_id, token, session, persist=True)
    return {"ok": True, "token": token}

async def process_telegram_update(update: Dict[str, Any]):
    """Process one Telegram update.

    This function contains the old Telegram webhook logic.
    Keeping it separate lets a lightweight tg_webhook.py enqueue updates
    without changing the existing bot behaviour.
    """
    if not isinstance(update, dict):
        return {"ok": True}

    _cleanup_state()

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

        if chat_id and user_id and data.startswith("mj:"):
            st = _ensure_state(chat_id, user_id)
            parts = data.split(":")
            mj = _midjourney_state(st)
            st["mode"] = "midjourney"

            if data == "mj:back:photo":
                mj["step"] = "need_prompt"
                st["midjourney"] = mj
                st["ts"] = _now()
                await tg_send_message(chat_id, "📸 Фото будущего — выбери режим:", reply_markup=_photo_future_menu_keyboard())
                return {"ok": True}

            if data == "mj:settings":
                mj = _midjourney_state(st)
                mj["step"] = "need_prompt"
                st["midjourney"] = mj
                st["ts"] = _now()
                await tg_send_message(chat_id, _midjourney_settings_text(mj, user_id=user_id), reply_markup=_midjourney_settings_kb(mj, user_id=user_id))
                return {"ok": True}

            if len(parts) >= 3 and parts[1] == "model":
                model = "midjourney-v8.1" if parts[2] in ("v81", "v8.1") else "midjourney-v7"
                mj = _midjourney_default_state(model)
                st["midjourney"] = mj
                st["ts"] = _now()
                await tg_send_message(chat_id, _midjourney_settings_text(mj, user_id=user_id), reply_markup=_midjourney_settings_kb(mj, user_id=user_id))
                return {"ok": True}

            if len(parts) >= 3 and parts[1] == "menu":
                mj["step"] = "need_prompt"
                st["midjourney"] = mj
                st["ts"] = _now()
                menu = parts[2]
                if menu == "aspect":
                    await tg_send_message(chat_id, "Выбери формат изображения:", reply_markup=_midjourney_aspect_kb(str(mj.get("aspect_ratio") or "1:1")))
                    return {"ok": True}
                if menu == "speed":
                    model = normalize_midjourney_model(mj.get("model") or "midjourney-v7")
                    await tg_send_message(chat_id, "Выбери скорость Midjourney:", reply_markup=_midjourney_speed_kb(model, str(mj.get("speed_mode") or "fast")))
                    return {"ok": True}
                if menu == "stylize":
                    await tg_send_message(
                        chat_id,
                        "Stylize отвечает за силу художественной стилизации.\n\n0 — ближе к prompt\n100 — стандартно\n1000 — максимально художественно\n\nТекущее значение: " + str(int(mj.get("stylize") or 100)),
                        reply_markup=_midjourney_value_kb("stylize", int(mj.get("stylize") or 100)),
                    )
                    return {"ok": True}
                if menu == "chaos":
                    await tg_send_message(
                        chat_id,
                        "Chaos отвечает за непредсказуемость результата.\n\n0 — максимально стабильно\n100 — максимально хаотично\n\nТекущее значение: " + str(int(mj.get("chaos") or 0)),
                        reply_markup=_midjourney_value_kb("chaos", int(mj.get("chaos") or 0)),
                    )
                    return {"ok": True}

            if len(parts) >= 3 and parts[1] == "ar":
                value = ":".join(parts[2:]).strip()
                if value in {"1:1", "16:9", "9:16", "4:5"}:
                    mj["aspect_ratio"] = value
                    mj["step"] = "need_prompt"
                    st["midjourney"] = mj
                    st["ts"] = _now()
                await tg_send_message(chat_id, _midjourney_settings_text(mj, user_id=user_id), reply_markup=_midjourney_settings_kb(mj, user_id=user_id))
                return {"ok": True}

            if len(parts) >= 3 and parts[1] == "speed":
                value = parts[2].strip().lower()
                mj["speed_mode"] = normalize_midjourney_speed_mode(value, model=mj.get("model") or "midjourney-v7")
                mj["step"] = "need_prompt"
                st["midjourney"] = mj
                st["ts"] = _now()
                await tg_send_message(chat_id, _midjourney_settings_text(mj, user_id=user_id), reply_markup=_midjourney_settings_kb(mj, user_id=user_id))
                return {"ok": True}

            if data == "mj:raw":
                mj["raw"] = not bool(mj.get("raw"))
                mj["step"] = "need_prompt"
                st["midjourney"] = mj
                st["ts"] = _now()
                await tg_send_message(chat_id, _midjourney_settings_text(mj, user_id=user_id), reply_markup=_midjourney_settings_kb(mj, user_id=user_id))
                return {"ok": True}

            if len(parts) >= 3 and parts[1] in {"stylize", "chaos"}:
                kind = parts[1]
                raw_value = parts[2]
                if raw_value == "custom":
                    mj["step"] = "need_custom_stylize" if kind == "stylize" else "need_custom_chaos"
                    st["midjourney"] = mj
                    st["ts"] = _now()
                    await tg_send_message(chat_id, "Введи значение Stylize от 0 до 1000." if kind == "stylize" else "Введи значение Chaos от 0 до 100.")
                    return {"ok": True}
                try:
                    value = int(raw_value)
                except Exception:
                    value = 100 if kind == "stylize" else 0
                if kind == "stylize":
                    mj["stylize"] = max(0, min(1000, value))
                else:
                    mj["chaos"] = max(0, min(100, value))
                mj["step"] = "need_prompt"
                st["midjourney"] = mj
                st["ts"] = _now()
                await tg_send_message(chat_id, _midjourney_settings_text(mj, user_id=user_id), reply_markup=_midjourney_settings_kb(mj, user_id=user_id))
                return {"ok": True}

            if len(parts) >= 3 and parts[1] == "ref":
                ref_kind = parts[2].strip().lower()
                if ref_kind == "style":
                    mj["step"] = "need_style_ref"
                    st["midjourney"] = mj
                    st["ts"] = _now()
                    await tg_send_message(chat_id, "Отправь изображение для Style Reference. Оно задаёт визуальный стиль результата.", reply_markup=_midjourney_ref_kb("style", bool(mj.get("style_ref_url"))))
                    return {"ok": True}
                if ref_kind == "omni" and normalize_midjourney_model(mj.get("model") or "midjourney-v7") != "midjourney-v8.1":
                    mj["step"] = "need_omni_ref"
                    st["midjourney"] = mj
                    st["ts"] = _now()
                    await tg_send_message(chat_id, "Отправь изображение для Omni Reference. Оно задаёт персонажа/объект для результата.", reply_markup=_midjourney_ref_kb("omni", bool(mj.get("omni_ref_url"))))
                    return {"ok": True}
                if ref_kind == "image":
                    mj["step"] = "need_image_prompt_ref"
                    st["midjourney"] = mj
                    st["ts"] = _now()
                    count = len([x for x in (mj.get("image_prompt_urls") or []) if str(x or "").strip()])
                    await tg_send_message(chat_id, f"Отправь image reference для V8.1. Можно добавить до 4 фото. Сейчас: {count}/4.", reply_markup=_midjourney_ref_kb("image", count > 0))
                    return {"ok": True}

            if len(parts) >= 3 and parts[1] == "ref_clear":
                ref_kind = parts[2].strip().lower()
                if ref_kind == "style":
                    mj["style_ref_url"] = ""
                    mj["style_ref_file_id"] = ""
                elif ref_kind == "omni":
                    mj["omni_ref_url"] = ""
                    mj["omni_ref_file_id"] = ""
                elif ref_kind == "image":
                    mj["image_prompt_urls"] = []
                    mj["image_prompt_file_ids"] = []
                mj["step"] = "need_prompt"
                st["midjourney"] = mj
                st["ts"] = _now()
                await tg_send_message(chat_id, _midjourney_settings_text(mj, user_id=user_id), reply_markup=_midjourney_settings_kb(mj, user_id=user_id))
                return {"ok": True}

            if data == "mj:prompt:edit":
                mj["step"] = "need_prompt"
                mj["prompt"] = ""
                st["midjourney"] = mj
                st["ts"] = _now()
                await tg_send_message(chat_id, "Пришли новый prompt для Midjourney.")
                return {"ok": True}

            if data == "mj:run":
                mj = _midjourney_state(st)
                prompt = str(mj.get("prompt") or "").strip()
                if not prompt:
                    await tg_send_message(chat_id, "Сначала пришли prompt для Midjourney.", reply_markup=_midjourney_settings_kb(mj, user_id=user_id))
                    return {"ok": True}
                try:
                    run_prompt = _midjourney_prepare_run_prompt(mj)
                except Exception as e:
                    await tg_send_message(chat_id, f"❌ Не смог собрать Midjourney prompt: {e}", reply_markup=_midjourney_settings_kb(mj, user_id=user_id))
                    return {"ok": True}
                dedupe_key = _midjourney_dedupe_key(
                    "generate",
                    user_id,
                    str(mj.get("model") or "midjourney-v7"),
                    str(mj.get("speed_mode") or "fast"),
                    str(mj.get("aspect_ratio") or "1:1"),
                    prompt,
                    run_prompt,
                )
                if not _midjourney_mark_action_once(mj, dedupe_key):
                    st["midjourney"] = mj
                    st["ts"] = _now()
                    await tg_send_message(chat_id, "⏳ Этот Midjourney-запрос уже принят. Повторное нажатие не списывает токены.", reply_markup=_photo_future_menu_keyboard())
                    return {"ok": True}
                ok, message_text = await _midjourney_charge_and_enqueue(
                    chat_id=chat_id,
                    user_id=user_id,
                    action="generate",
                    model=str(mj.get("model") or "midjourney-v7"),
                    prompt=prompt,
                    run_prompt=run_prompt,
                    aspect_ratio=str(mj.get("aspect_ratio") or "1:1"),
                    speed_mode=str(mj.get("speed_mode") or "fast"),
                    settings=mj,
                )
                if not ok:
                    _midjourney_clear_action_mark(mj, dedupe_key)
                await tg_send_message(chat_id, message_text, reply_markup=_midjourney_submit_reply_markup(ok, message_text, mj, user_id=user_id))
                mj["step"] = "need_prompt"
                st["midjourney"] = mj
                st["ts"] = _now()
                return {"ok": True}

            return {"ok": True}

        if chat_id and user_id and data.startswith("mjr:"):
            parts = data.split(":")
            token = parts[1].strip() if len(parts) > 1 else ""
            action = parts[2].strip() if len(parts) > 2 else ""
            session = _midjourney_get_session(chat_id, user_id, token)
            if not session:
                session = await _midjourney_load_session_from_storage(chat_id, user_id, token)
            if not session:
                await tg_answer_callback_query(str(cq_id), text="Midjourney результат устарел. Сгенерируй заново.", show_alert=True)
                return {"ok": True}
            message_id = int((msg or {}).get("message_id") or 0)

            if action == "open":
                try:
                    index = max(0, min(3, int(parts[3] if len(parts) > 3 else 0)))
                except Exception:
                    index = 0
                images = session.get("images") or []
                if index >= len(images):
                    await tg_answer_callback_query(str(cq_id), text="Такого изображения нет.", show_alert=True)
                    return {"ok": True}
                image = images[index]
                image_url = str(image.get("url") or "").strip()
                try:
                    await tg_edit_message_media_photo_url(chat_id, message_id, image_url, caption=_midjourney_result_caption(session, index), reply_markup=_midjourney_image_result_kb(token, index))
                except Exception:
                    await tg_send_photo_url(chat_id, image_url, caption=_midjourney_result_caption(session, index), reply_markup=_midjourney_image_result_kb(token, index))
                session["active_index"] = index
                session["ts"] = _now()
                return {"ok": True}

            if action == "grid":
                grid_url = str(session.get("grid_url") or "").strip()
                try:
                    await tg_edit_message_media_photo_url(chat_id, message_id, grid_url, caption=_midjourney_result_caption(session, grid=True), reply_markup=_midjourney_grid_result_kb(token))
                except Exception:
                    await tg_send_photo_url(chat_id, grid_url, caption=_midjourney_result_caption(session, grid=True), reply_markup=_midjourney_grid_result_kb(token))
                session["ts"] = _now()
                return {"ok": True}

            if action == "download":
                try:
                    index = max(0, min(3, int(parts[3] if len(parts) > 3 else session.get("active_index") or 0)))
                except Exception:
                    index = 0
                images = session.get("images") or []
                if index >= len(images):
                    await tg_answer_callback_query(str(cq_id), text="Оригинал не найден.", show_alert=True)
                    return {"ok": True}
                image = images[index]
                image_url = str(image.get("url") or "").strip()
                try:
                    file_bytes = await http_download_bytes(image_url, timeout=180)
                    await tg_send_document_bytes(chat_id, file_bytes, filename=f"midjourney_{index + 1}.{image.get('ext') or 'jpg'}", caption=f"⬇️ Midjourney оригинал #{index + 1}")
                except Exception as e:
                    await tg_send_message(chat_id, f"Не удалось отправить оригинал файлом. Открой ссылку: {image_url}\nОшибка: {str(e)[:300]}")
                return {"ok": True}

            if action == "download_all":
                images = session.get("images") or []
                await tg_send_message(chat_id, "Отправляю 4 оригинала файлами без сжатия.")
                for idx, image in enumerate(images[:4], start=1):
                    image_url = str(image.get("url") or "").strip()
                    try:
                        file_bytes = await http_download_bytes(image_url, timeout=180)
                        await tg_send_document_bytes(chat_id, file_bytes, filename=f"midjourney_{idx}.{image.get('ext') or 'jpg'}", caption=f"⬇️ Midjourney оригинал #{idx}")
                    except Exception as e:
                        await tg_send_message(chat_id, f"Не удалось отправить оригинал #{idx}. Ссылка: {image_url}\nОшибка: {str(e)[:300]}")
                return {"ok": True}

            if action == "prompt":
                await _midjourney_send_prompt_details(chat_id, session)
                return {"ok": True}

            if action == "reroll":
                cost = int(_midjourney_user_cost(
                    user_id,
                    str(session.get("model") or "midjourney-v7"),
                    str(session.get("speed_mode") or "fast"),
                    str(session.get("resolution") or "2K"),
                ))
                if cost > 0:
                    reroll_text = f"Reroll создаст новую подборку из 4 изображений и спишет {cost} токен.\n\nПродолжить?"
                else:
                    reroll_text = "Reroll создаст новую подборку из 4 изображений бесплатно по тарифу Pulse/Nexus.\n\nПродолжить?"
                await tg_send_message(
                    chat_id,
                    reroll_text,
                    reply_markup=_midjourney_reroll_confirm_kb(token),
                )
                return {"ok": True}

            if action == "reroll_no":
                await tg_send_message(chat_id, "Ок, Reroll отменён.")
                return {"ok": True}

            if action == "reroll_yes":
                source_task_id = str(session.get("provider_task_id") or "").strip()
                if not source_task_id:
                    await tg_send_message(chat_id, "❌ Не найден исходный Midjourney task для Reroll.")
                    return {"ok": True}
                dedupe_key = _midjourney_dedupe_key("reroll", user_id, token, source_task_id)
                if not _midjourney_mark_action_once(session, dedupe_key):
                    session["ts"] = _now()
                    await tg_send_message(chat_id, "⏳ Reroll уже принят. Повторное нажатие не списывает токены.", reply_markup=_photo_future_menu_keyboard())
                    return {"ok": True}
                ok, message_text = await _midjourney_charge_and_enqueue(
                    chat_id=chat_id,
                    user_id=user_id,
                    action="reroll",
                    model=str(session.get("model") or "midjourney-v7"),
                    prompt=str(session.get("prompt") or ""),
                    run_prompt=str(session.get("run_prompt") or ""),
                    aspect_ratio=str(session.get("aspect_ratio") or "1:1"),
                    speed_mode=str(session.get("speed_mode") or "fast"),
                    settings=session.get("settings") if isinstance(session.get("settings"), dict) else {},
                    source_task_id=source_task_id,
                )
                if not ok:
                    _midjourney_clear_action_mark(session, dedupe_key)
                session["ts"] = _now()
                await tg_send_message(chat_id, message_text, reply_markup=_midjourney_submit_reply_markup(ok, message_text))
                return {"ok": True}

            if action == "vary":
                try:
                    index = max(0, min(3, int(parts[3] if len(parts) > 3 else session.get("active_index") or 0)))
                except Exception:
                    index = int(session.get("active_index") or 0)
                variation_type = str(parts[4] if len(parts) > 4 else "subtle").strip().lower()
                if variation_type not in {"subtle", "strong"}:
                    variation_type = "subtle"
                source_task_id = str(session.get("provider_task_id") or "").strip()
                if not source_task_id:
                    await tg_send_message(chat_id, "❌ Не найден исходный Midjourney task для Remix.")
                    return {"ok": True}
                dedupe_key = _midjourney_dedupe_key("variation", user_id, token, source_task_id, index, variation_type)
                if not _midjourney_mark_action_once(session, dedupe_key):
                    session["ts"] = _now()
                    await tg_send_message(chat_id, "⏳ Remix уже принят. Повторное нажатие не списывает токены.", reply_markup=_photo_future_menu_keyboard())
                    return {"ok": True}
                ok, message_text = await _midjourney_charge_and_enqueue(
                    chat_id=chat_id,
                    user_id=user_id,
                    action="variation",
                    model=str(session.get("model") or "midjourney-v7"),
                    prompt=str(session.get("prompt") or ""),
                    run_prompt=str(session.get("run_prompt") or ""),
                    aspect_ratio=str(session.get("aspect_ratio") or "1:1"),
                    speed_mode=str(session.get("speed_mode") or "fast"),
                    settings=session.get("settings") if isinstance(session.get("settings"), dict) else {},
                    source_task_id=source_task_id,
                    selected_image_no=index,
                    variation_type=variation_type,
                )
                if not ok:
                    _midjourney_clear_action_mark(session, dedupe_key)
                session["ts"] = _now()
                await tg_send_message(chat_id, message_text, reply_markup=_midjourney_submit_reply_markup(ok, message_text))
                return {"ok": True}

            if action == "new":
                st = _ensure_state(chat_id, user_id)
                base_settings = session.get("settings") if isinstance(session.get("settings"), dict) else {}
                mj = _midjourney_state({"midjourney": base_settings})
                mj["prompt"] = ""
                mj["step"] = "need_prompt"
                st["mode"] = "midjourney"
                st["midjourney"] = mj
                st["ts"] = _now()
                await tg_send_message(chat_id, "Ок, пришли новый prompt для Midjourney с текущими настройками.", reply_markup=_midjourney_settings_kb(mj, user_id=user_id))
                return {"ok": True}

            return {"ok": True}

        if chat_id and user_id and data.startswith("seedance_prompt:"):
            st = _ensure_state(chat_id, user_id)
            action = data.split(":", 1)[1].strip()
            mode_now = str(st.get("mode") or "").strip()

            if mode_now not in ("seedance_t2v", "seedance_i2v", "seedance_omni"):
                await tg_send_message(chat_id, "Сейчас я не жду Seedance-промпт. Открой настройки Seedance заново.", reply_markup=_main_menu_for(user_id))
                return {"ok": True}

            if action == "cancel":
                _set_mode(chat_id, user_id, "chat")
                st.pop("seedance_t2v", None)
                st.pop("seedance_i2v", None)
                st.pop("seedance_omni", None)
                st.pop("seedance_settings", None)
                st["ts"] = _now()
                try:
                    sb_clear_user_state(user_id)
                except Exception:
                    pass
                await tg_send_message(chat_id, "Ок, Seedance отменил. Главное меню.", reply_markup=_main_menu_for(user_id))
                return {"ok": True}

            if action == "clear":
                _seedance_prompt_clear_state(st)
                await tg_send_message(
                    chat_id,
                    "Промпт очищен. Пришли текст одной или несколькими частями, затем нажми «✅ Запустить».",
                    reply_markup=_seedance_prompt_collect_kb(mode_now),
                )
                return {"ok": True}

            if action == "done":
                prompt = _seedance_prompt_text_from_state(st)
                return await _seedance_start_generation_from_prompt(chat_id, user_id, st, prompt)

            return {"ok": True}

        if chat_id and user_id and data.startswith("seedance_refs:"):
            st = _ensure_state(chat_id, user_id)
            action = data.split(":", 1)[1].strip()
            mode_now = str(st.get("mode") or "").strip()

            if action == "cancel":
                _set_mode(chat_id, user_id, "chat")
                st.pop("seedance_t2v", None)
                st.pop("seedance_i2v", None)
                st.pop("seedance_omni", None)
                st.pop("seedance_settings", None)
                st["ts"] = _now()
                try:
                    sb_clear_user_state(user_id)
                except Exception:
                    pass
                await tg_send_message(chat_id, "Ок, Seedance отменил. Главное меню.", reply_markup=_main_menu_for(user_id))
                return {"ok": True}

            if mode_now not in ("seedance_i2v", "seedance_omni"):
                await tg_send_message(chat_id, "Сейчас я не собираю refs для Seedance. Открой настройки Seedance заново.", reply_markup=_main_menu_for(user_id))
                return {"ok": True}

            settings = st.get("seedance_settings") or {}

            if action == "back":
                if mode_now == "seedance_i2v":
                    si = st.get("seedance_i2v") or {}
                    si["step"] = "need_images"
                    st["seedance_i2v"] = si
                else:
                    so = st.get("seedance_omni") or {}
                    so["step"] = "collect_refs"
                    st["seedance_omni"] = so
                st["ts"] = _now()
                await tg_send_message(
                    chat_id,
                    "Ок, вернулся к загрузке refs. " + _seedance_collect_summary_text(mode_now, settings),
                    reply_markup=_seedance_refs_collect_kb(),
                )
                return {"ok": True}

            if action == "done":
                if mode_now == "seedance_i2v":
                    si = st.get("seedance_i2v") or {}
                    imgs = [x for x in (si.get("image_file_ids") or []) if str(x or "").strip()]
                    if not imgs:
                        await tg_send_message(
                            chat_id,
                            "Сначала пришли хотя бы 1 фото.",
                            reply_markup=_seedance_refs_collect_kb(),
                        )
                        return {"ok": True}
                    si["step"] = "need_prompt"
                    st["seedance_i2v"] = si
                    st["ts"] = _now()
                    await tg_send_message(
                        chat_id,
                        f"Фото принял ✅ Всего фото: {len(imgs)}.\nПришли промпт одним или несколькими сообщениями. Когда всё отправишь — нажми «✅ Запустить».",
                        reply_markup=_seedance_prompt_collect_kb("seedance_i2v"),
                    )
                    return {"ok": True}

                so = st.get("seedance_omni") or {}
                image_ids = [x for x in (so.get("image_file_ids") or []) if str(x or "").strip()]
                video_ids = [x for x in (so.get("video_file_ids") or []) if str(x or "").strip()]
                audio_ids = [x for x in (so.get("audio_file_ids") or []) if str(x or "").strip()]
                total_refs = len(image_ids) + len(video_ids) + len(audio_ids)
                if total_refs <= 0:
                    await tg_send_message(
                        chat_id,
                        "Сначала пришли хотя бы один image/video/audio reference.",
                        reply_markup=_seedance_refs_collect_kb(),
                    )
                    return {"ok": True}
                if audio_ids and not (image_ids or video_ids):
                    await tg_send_message(
                        chat_id,
                        "Для Omni Reference аудио нельзя отправлять отдельно. Добавь хотя бы фото или видео reference.",
                        reply_markup=_seedance_refs_collect_kb(),
                    )
                    return {"ok": True}

                so["step"] = "need_prompt"
                st["seedance_omni"] = so
                st["ts"] = _now()
                await tg_send_message(
                    chat_id,
                    f"Референсы принял ✅ Фото: {len(image_ids)}, видео: {len(video_ids)}, аудио: {len(audio_ids)}.\nПришли промпт одним или несколькими сообщениями. Когда всё отправишь — нажми «✅ Запустить».",
                    reply_markup=_seedance_prompt_collect_kb("seedance_omni"),
                )
                return {"ok": True}

            return {"ok": True}

        if chat_id and user_id and data.startswith("nbp:aspect:"):
            aspect_ratio = data.split(":", 2)[2].strip()
            if aspect_ratio not in ("1:1", "4:5", "9:16", "16:9"):
                return {"ok": True}

            st = _ensure_state(chat_id, user_id)
            nbp = st.get("nano_banana_pro") or {
                "step": "need_photo",
                "photo_bytes": None,
                "resolution": "2K",
                "aspect_ratio": "9:16",
            }
            nbp["aspect_ratio"] = aspect_ratio
            st["nano_banana_pro"] = nbp
            st["ts"] = _now()

            await tg_send_message(
                chat_id,
                f"✅ Формат Nano Banana Pro: {aspect_ratio}\nТеперь пришли фото или сразу напиши текст.",
                reply_markup=_nano_banana_pro_aspect_inline_kb(aspect_ratio),
            )
            return {"ok": True}

        if chat_id and user_id and data.startswith("nbpn:res:"):
            resolution = data.split(":", 2)[2].strip().upper()
            if resolution not in ("2K", "4K"):
                return {"ok": True}

            st = _ensure_state(chat_id, user_id)
            nbpn = st.get("nano_banana_pro_new") or {
                "step": "need_photo",
                "photo_bytes": None,
                "photo_file_id": None,
                "photo_file_ids": [],
                "resolution": "2K",
                "aspect_ratio": "9:16",
            }
            nbpn["resolution"] = resolution
            st["nano_banana_pro_new"] = nbpn
            st["ts"] = _now()

            refs_count = len([x for x in (nbpn.get("photo_file_ids") or []) if str(x or "").strip()])
            await tg_send_message(
                chat_id,
                f"✅ Nano Banana Pro - NEW: {resolution} • {nano_banana_pro_new_cost(resolution)} ток.\nФормат: {nbpn.get('aspect_ratio') or '9:16'}\nФото: {refs_count}/8\nМожно прислать до 8 фото. Когда закончишь — нажми «Готово» или просто отправь prompt текстом.",
                reply_markup=_nano_banana_pro_new_inline_kb(nbpn.get("aspect_ratio") or "9:16", resolution, refs_count),
            )
            return {"ok": True}

        if chat_id and user_id and data.startswith("nbpn:aspect:"):
            aspect_ratio = data.split(":", 2)[2].strip()
            if aspect_ratio not in ("1:1", "4:5", "9:16", "16:9"):
                return {"ok": True}

            st = _ensure_state(chat_id, user_id)
            nbpn = st.get("nano_banana_pro_new") or {
                "step": "need_photo",
                "photo_bytes": None,
                "photo_file_id": None,
                "photo_file_ids": [],
                "resolution": "2K",
                "aspect_ratio": "9:16",
            }
            nbpn["aspect_ratio"] = aspect_ratio
            st["nano_banana_pro_new"] = nbpn
            st["ts"] = _now()

            current_resolution = str(nbpn.get("resolution") or "2K").upper() or "2K"
            refs_count = len([x for x in (nbpn.get("photo_file_ids") or []) if str(x or "").strip()])
            await tg_send_message(
                chat_id,
                f"✅ Формат Nano Banana Pro - NEW: {aspect_ratio}\nResolution: {current_resolution} • {nano_banana_pro_new_cost(current_resolution)} ток.\nФото: {refs_count}/8\nМожно прислать до 8 фото. Когда закончишь — нажми «Готово» или просто отправь prompt текстом.",
                reply_markup=_nano_banana_pro_new_inline_kb(aspect_ratio, current_resolution, refs_count),
            )
            return {"ok": True}

        if chat_id and user_id and data == "nbpn:done":
            st = _ensure_state(chat_id, user_id)
            nbpn = st.get("nano_banana_pro_new") or {}
            refs = [x for x in (nbpn.get("photo_file_ids") or []) if str(x or "").strip()]
            current_resolution = str(nbpn.get("resolution") or "2K").upper() or "2K"
            current_aspect = nbpn.get("aspect_ratio") or "9:16"
            if not refs:
                await tg_send_message(
                    chat_id,
                    "Сначала пришли хотя бы одно фото или сразу текст для генерации без фото.",
                    reply_markup=_nano_banana_pro_new_inline_kb(current_aspect, current_resolution, 0),
                )
                return {"ok": True}
            nbpn["step"] = "need_prompt"
            st["nano_banana_pro_new"] = nbpn
            st["ts"] = _now()
            await tg_send_message(
                chat_id,
                f"✅ Фото зафиксированы: {len(refs)}/8\nТеперь пришли prompt одним сообщением. Цена: {nano_banana_pro_new_cost(current_resolution)} ток.",
                reply_markup=_nano_banana_pro_new_inline_kb(current_aspect, current_resolution, len(refs)),
            )
            return {"ok": True}

        if chat_id and user_id and data == "nbpn:clear":
            st = _ensure_state(chat_id, user_id)
            nbpn = st.get("nano_banana_pro_new") or {}
            current_resolution = str(nbpn.get("resolution") or "2K").upper() or "2K"
            current_aspect = nbpn.get("aspect_ratio") or "9:16"
            st["nano_banana_pro_new"] = {
                "step": "need_photo",
                "photo_bytes": None,
                "photo_file_id": None,
                "photo_file_ids": [],
                "resolution": current_resolution,
                "aspect_ratio": current_aspect,
            }
            st["ts"] = _now()
            await tg_send_message(
                chat_id,
                "🗑 Фото очищены. Можешь прислать новые фото, до 8 штук, или сразу текст для генерации без фото.",
                reply_markup=_nano_banana_pro_new_inline_kb(current_aspect, current_resolution, 0),
            )
            return {"ok": True}

        if chat_id and user_id and data.startswith("nb2:aspect:"):
            aspect_ratio = data.split(":", 2)[2].strip()
            if aspect_ratio not in ("1:1", "4:5", "9:16", "16:9"):
                return {"ok": True}

            st = _ensure_state(chat_id, user_id)
            nb2 = st.get("nano_banana_2") or {
                "step": "need_photo",
                "photo_bytes": None,
                "resolution": "2K",
                "aspect_ratio": "9:16",
            }
            nb2["aspect_ratio"] = aspect_ratio
            st["nano_banana_2"] = nb2
            st["ts"] = _now()

            await tg_send_message(
                chat_id,
                f"✅ Формат Nano Banana 2: {aspect_ratio}\nТеперь пришли фото или сразу напиши текст.\n{_nano_banana_basic_cost_hint_for_user(user_id, 'nano_banana_2', '2K')}",
                reply_markup=_nano_banana_2_aspect_inline_kb(aspect_ratio),
            )
            return {"ok": True}

        if chat_id and user_id and data.startswith("gi2k:"):
            parts = data.split(":")
            st = _ensure_state(chat_id, user_id)

            if data == "gi2k:done":
                gi2k = st.get("gpt_image_2_kie_i2i") or {}
                refs = [x for x in (gi2k.get("photo_file_ids") or []) if str(x or "").strip()]
                current_resolution = _gpt_image_2_kie_resolution(gi2k.get("resolution") or "2K")
                current_aspect = _gpt_image_2_kie_aspect(gi2k.get("aspect_ratio") or "16:9")
                if not refs:
                    await tg_send_message(
                        chat_id,
                        "Сначала пришли хотя бы одно фото или сразу текст для генерации без фото.",
                        reply_markup=_gpt_image_2_kie_inline_kb("i2i", current_aspect, current_resolution, 0),
                    )
                    return {"ok": True}
                gi2k["step"] = "need_prompt"
                st["gpt_image_2_kie_i2i"] = gi2k
                st["ts"] = _now()
                await tg_send_message(
                    chat_id,
                    f"✅ Фото зафиксированы: {len(refs)}/16\nТеперь пришли prompt одним сообщением. Цена: {_gpt_image_2_kie_user_cost(user_id, current_resolution)} ток.",
                    reply_markup=_gpt_image_2_kie_inline_kb("i2i", current_aspect, current_resolution, len(refs)),
                )
                return {"ok": True}

            if data == "gi2k:clear":
                gi2k = st.get("gpt_image_2_kie_i2i") or {}
                current_resolution = _gpt_image_2_kie_resolution(gi2k.get("resolution") or "2K")
                current_aspect = _gpt_image_2_kie_aspect(gi2k.get("aspect_ratio") or "16:9")
                st["gpt_image_2_kie_i2i"] = {
                    "step": "need_image",
                    "photo_file_id": None,
                    "photo_file_ids": [],
                    "photo_urls": [],
                    "resolution": current_resolution,
                    "aspect_ratio": current_aspect,
                }
                st["ts"] = _now()
                await tg_send_message(
                    chat_id,
                    "🗑 Фото очищены. Можешь прислать новые фото до 16 штук или выбрать Text→Image.",
                    reply_markup=_gpt_image_2_kie_inline_kb("i2i", current_aspect, current_resolution, 0),
                )
                return {"ok": True}

            mode_key = parts[1].strip() if len(parts) > 1 else ""
            action = parts[2].strip() if len(parts) > 2 else ""
            value = ":".join(parts[3:]).strip() if len(parts) > 3 else ""
            if mode_key not in ("t2i", "i2i") or action not in ("res", "aspect"):
                return {"ok": True}

            state_key = "gpt_image_2_kie_t2i" if mode_key == "t2i" else "gpt_image_2_kie_i2i"
            mode_name = "gpt_image_2_kie_t2i" if mode_key == "t2i" else "gpt_image_2_kie_i2i"
            st["mode"] = mode_name
            gi2k = st.get(state_key) or {
                "step": "need_prompt" if mode_key == "t2i" else "need_image",
                "photo_file_id": None,
                "photo_file_ids": [],
                "photo_urls": [],
                "resolution": "2K",
                "aspect_ratio": "16:9",
            }
            if action == "res":
                gi2k["resolution"] = _gpt_image_2_kie_resolution(value)
            else:
                gi2k["aspect_ratio"] = _gpt_image_2_kie_aspect(value)
            current_resolution, current_aspect = _gpt_image_2_kie_options(gi2k.get("resolution") or "2K", gi2k.get("aspect_ratio") or "16:9")
            gi2k["resolution"] = current_resolution
            gi2k["aspect_ratio"] = current_aspect
            st[state_key] = gi2k
            st["ts"] = _now()
            refs_count = len([x for x in (gi2k.get("photo_file_ids") or []) if str(x or "").strip()])
            if mode_key == "t2i":
                await tg_send_message(
                    chat_id,
                    f"✅ Gpt Image 2: {current_resolution} • {_gpt_image_2_kie_user_cost(user_id, current_resolution)} ток.\nФормат: {current_aspect}\nТеперь пришли текст для генерации.",
                    reply_markup=_gpt_image_2_kie_inline_kb("t2i", current_aspect, current_resolution, 0),
                )
                return {"ok": True}

            await tg_send_message(
                chat_id,
                f"✅ Gpt Image 2: {current_resolution} • {_gpt_image_2_kie_user_cost(user_id, current_resolution)} ток.\nФормат: {current_aspect}\nФото: {refs_count}/16\nТеперь пришли фото или prompt, если фото уже загружены.",
                reply_markup=_gpt_image_2_kie_inline_kb("i2i", current_aspect, current_resolution, refs_count),
            )
            return {"ok": True}

        if chat_id and user_id and data.startswith("gi2:"):
            parts = data.split(":")
            mode_key = parts[1].strip() if len(parts) > 1 else ""
            legacy_aspect = ":".join(parts[3:]).strip() if len(parts) > 3 else ""
            if mode_key not in ("t2i", "i2i"):
                return {"ok": True}

            st = _ensure_state(chat_id, user_id)
            resolution, aspect_ratio = _gpt_image_2_kie_options("2K", _legacy_gpt_image_2_aspect_to_kie(legacy_aspect))

            if mode_key == "t2i":
                st["mode"] = "gpt_image_2_kie_t2i"
                st["gpt_image_2_kie_t2i"] = {"step": "need_prompt", "aspect_ratio": aspect_ratio, "resolution": resolution}
                st.pop("gpt_image_2_t2i", None)
                st["ts"] = _now()
                await tg_send_message(
                    chat_id,
                    f"✅ Gpt Image 2: {resolution} • {_gpt_image_2_kie_user_cost(user_id, resolution)} ток.\nФормат: {aspect_ratio}\nТеперь пришли текст для генерации.",
                    reply_markup=_gpt_image_2_kie_inline_kb("t2i", aspect_ratio, resolution, 0),
                )
                return {"ok": True}

            gi2_old = st.get("gpt_image_2_i2i") or {}
            photo_file_ids = [str(x or "").strip() for x in (gi2_old.get("photo_file_ids") or []) if str(x or "").strip()]
            if not photo_file_ids and str(gi2_old.get("photo_file_id") or "").strip():
                photo_file_ids = [str(gi2_old.get("photo_file_id") or "").strip()]
            photo_urls = [str(x or "").strip() for x in (gi2_old.get("photo_urls") or []) if str(x or "").strip()]
            st["mode"] = "gpt_image_2_kie_i2i"
            gi2k = {
                "step": "need_prompt" if (photo_file_ids or photo_urls) else "need_image",
                "photo_file_id": photo_file_ids[0] if photo_file_ids else None,
                "photo_file_ids": photo_file_ids[:16],
                "photo_urls": photo_urls[:16],
                "aspect_ratio": aspect_ratio,
                "resolution": resolution,
            }
            st["gpt_image_2_kie_i2i"] = gi2k
            st.pop("gpt_image_2_i2i", None)
            st["ts"] = _now()
            refs_count = len(photo_file_ids[:16] or photo_urls[:16])
            await tg_send_message(
                chat_id,
                f"✅ Gpt Image 2: {resolution} • {_gpt_image_2_kie_user_cost(user_id, resolution)} ток.\nФормат: {aspect_ratio}\nФото: {refs_count}/16\nТеперь пришли фото или prompt, если фото уже загружены.",
                reply_markup=_gpt_image_2_kie_inline_kb("i2i", aspect_ratio, resolution, refs_count),
            )
            return {"ok": True}

        if chat_id and user_id and data.startswith("sd45:"):
            parts = data.split(":")
            mode_key = parts[1].strip() if len(parts) > 1 else ""
            aspect_ratio = ":".join(parts[3:]).strip() if len(parts) > 3 else ""
            if mode_key not in ("t2i", "i2i", "single"):
                return {"ok": True}
            if aspect_ratio not in ("1:1", "4:5", "9:16", "16:9"):
                return {"ok": True}

            st = _ensure_state(chat_id, user_id)
            if mode_key == "t2i":
                _set_mode(chat_id, user_id, "t2i")
                t2i = st.get("t2i") or {"step": "need_prompt", "model": "seedream_45"}
                t2i["aspect_ratio"] = aspect_ratio
                t2i["model"] = "seedream_45"
                st["t2i"] = t2i
                st["ts"] = _now()
                await tg_send_message(
                    chat_id,
                    f"✅ Формат Seedream 4.5: {aspect_ratio}\nТеперь пришли текст.",
                    reply_markup=_seedream_aspect_inline_kb("t2i", aspect_ratio),
                )
                return {"ok": True}

            if mode_key == "single":
                _set_mode(chat_id, user_id, "seedream_single")
                sd = st.get("seedream_single") or {
                    "step": "need_photo",
                    "photo_bytes": None,
                    "photo_file_id": None,
                    "model": "seedream_45",
                }
                sd["aspect_ratio"] = aspect_ratio
                sd["model"] = "seedream_45"
                st["seedream_single"] = sd
                st["ts"] = _now()
                await tg_send_message(
                    chat_id,
                    f"✅ Формат Seedream 4.5: {aspect_ratio}\nТеперь пришли фото.",
                    reply_markup=_seedream_aspect_inline_kb("single", aspect_ratio),
                )
                return {"ok": True}

            _set_mode(chat_id, user_id, "two_photos")
            tp = st.get("two_photos") or {
                "step": "need_photo_1",
                "photo1_bytes": None,
                "photo1_file_id": None,
                "photo2_bytes": None,
                "photo2_file_id": None,
                "model": "seedream_45",
            }
            tp["aspect_ratio"] = aspect_ratio
            tp["model"] = "seedream_45"
            st["two_photos"] = tp
            st["ts"] = _now()
            await tg_send_message(
                chat_id,
                f"✅ Формат Seedream 4.5: {aspect_ratio}\nТеперь пришли Фото 1.",
                reply_markup=_seedream_aspect_inline_kb("i2i", aspect_ratio),
            )
            return {"ok": True}

        if chat_id and user_id and data.startswith("aichat:"):
            st = _ensure_state(chat_id, user_id)
            parts = data.split(":")

            if data == "aichat:mode:menu":
                _set_mode(chat_id, user_id, "chat")
                st["ai_chat_mode"] = "menu"
                st["ai_prompt"] = _new_ai_prompt_state()
                st["ts"] = _now()
                await tg_send_message(
                    chat_id,
                    "🤖 ИИ помощник. Выбери модель чата или генератор промтов:",
                    reply_markup=_ai_chat_mode_inline_kb(),
                )
                return {"ok": True}

            if data == "aichat:new_chat":
                _set_mode(chat_id, user_id, "chat")
                err = await _reset_tg_ai_chat_dialogue(chat_id, user_id, st)
                if err:
                    await tg_send_message(
                        chat_id,
                        f"❌ Не смог очистить историю ИИ-чата: {err}",
                        reply_markup=_ai_chat_mode_inline_kb(),
                    )
                    return {"ok": True}

                model = st.get("ai_chat_model")
                if model:
                    st["ai_chat_mode"] = "chat"
                    msg = (
                        "✅ New Chat создан. История очищена.\n"
                        f"Текущая модель: {_ai_chat_model_title(model)}.\n\n"
                        "Можешь писать новую идею — старый диалог больше не будет подмешиваться."
                    )
                else:
                    st["ai_chat_mode"] = "menu"
                    msg = (
                        "✅ New Chat создан. История очищена.\n\n"
                        "Выбери модель чата: Claude Sonnet, Claude Opus 4.7, Claude Fable 5 или ChatGPT."
                    )
                st["ts"] = _now()
                await tg_send_message(chat_id, msg, reply_markup=_ai_chat_mode_inline_kb(_tg_fable_thinking_enabled(st), selected_model=model))
                return {"ok": True}

            if data == "aichat:fable_thinking:toggle":
                _set_mode(chat_id, user_id, "chat")
                st["ai_fable_thinking"] = not bool(st.get("ai_fable_thinking"))
                st["ai_chat_mode"] = "chat"
                st["ai_chat_model"] = "claude_fable"
                st["ts"] = _now()
                cost = _tg_fable_chat_cost_tokens(st, has_files=False)
                await tg_send_message(
                    chat_id,
                    f"{KIE_CLAUDE_FABLE_DISPLAY_NAME}: углублённое мышление {'включено' if st.get('ai_fable_thinking') else 'выключено'}.\nСтоимость обычного запроса: {cost} токенов.",
                    reply_markup=_ai_chat_mode_inline_kb(_tg_fable_thinking_enabled(st), selected_model="claude_fable"),
                )
                return {"ok": True}

            if data in ("aichat:model:claude", "aichat:model:opus", "aichat:model:fable", "aichat:model:openai", "aichat:mode:chat"):
                _set_mode(chat_id, user_id, "chat")
                model = "openai" if data == "aichat:model:openai" else ("claude_opus" if data == "aichat:model:opus" else ("claude_fable" if data == "aichat:model:fable" else "claude"))
                st["ai_chat_mode"] = "chat"
                st["ai_chat_model"] = model
                st["ai_prompt"] = _new_ai_prompt_state()
                st["ts"] = _now()
                if model == "claude_fable":
                    msg = (
                        f"💬 Режим чата включён: {_ai_chat_model_title(model)}.\n"
                        f"Стоимость: {_tg_fable_chat_cost_tokens(st, has_files=False)} ток. обычный запрос, "
                        f"{_tg_fable_chat_cost_tokens(st, has_files=True)} ток. с файлом.\n"
                        "Можешь писать вопрос или прислать файл."
                    )
                else:
                    msg = f"💬 Режим чата включён: {_ai_chat_model_title(model)}.\nМожешь писать вопрос или прислать фото для анализа."
                await tg_send_message(
                    chat_id,
                    msg,
                    reply_markup=_ai_chat_mode_inline_kb(_tg_fable_thinking_enabled(st), selected_model=model),
                )
                return {"ok": True}

            if data == "aichat:mode:prompt":
                _set_mode(chat_id, user_id, "chat")
                st["ai_chat_mode"] = "prompt"
                st.pop("ai_chat_model", None)
                st["ai_prompt"] = _new_ai_prompt_state()
                st["ts"] = _now()
                await tg_send_message(
                    chat_id,
                    "🪄 Режим промтов включён. Выбери, для чего нужен промт:",
                    reply_markup=_ai_prompt_root_inline_kb(),
                )
                return {"ok": True}

            if data == "aichat:prompt_reset":
                st["ai_chat_mode"] = "prompt"
                st["ai_prompt"] = _new_ai_prompt_state()
                st["ts"] = _now()
                await tg_send_message(
                    chat_id,
                    "Выбери раздел для промта:",
                    reply_markup=_ai_prompt_root_inline_kb(),
                )
                return {"ok": True}

            if data == "aichat:prompt_clear":
                pb = st.get("ai_prompt") or _new_ai_prompt_state()
                pb["images"] = []
                st["ai_chat_mode"] = "prompt"
                st["ai_prompt"] = pb
                st["ts"] = _now()
                await tg_send_message(
                    chat_id,
                    f"Фото очищены.\n\n{_ai_prompt_waiting_text(pb)}",
                    reply_markup=_ai_prompt_tools_inline_kb(pb),
                )
                return {"ok": True}

            if len(parts) == 3 and parts[1] == "prompt_root":
                root = parts[2].strip().lower()
                if root in ("photo", "video", "music", "universal"):
                    pb = st.get("ai_prompt") or _new_ai_prompt_state()
                    pb["root"] = root
                    pb["provider"] = None
                    pb["images"] = list(pb.get("images") or [])[:PROMPT_BUILDER_MAX_IMAGES]
                    st["ai_chat_mode"] = "prompt"
                    if root == "video":
                        pb["step"] = "choose_video_target"
                        st["ai_prompt"] = pb
                        st["ts"] = _now()
                        await tg_send_message(
                            chat_id,
                            "Выбери видеомодель, под которую собрать промт:",
                            reply_markup=_ai_prompt_video_inline_kb(),
                        )
                        return {"ok": True}
                    pb["step"] = "await_input"
                    st["ai_prompt"] = pb
                    st["ts"] = _now()
                    await tg_send_message(
                        chat_id,
                        _ai_prompt_waiting_text(pb),
                        reply_markup=_ai_prompt_tools_inline_kb(pb),
                    )
                    return {"ok": True}

            if len(parts) == 3 and parts[1] == "prompt_video":
                provider = parts[2].strip().lower()
                if provider in ("veo", "kling", "seedance", "sora", "universal"):
                    pb = st.get("ai_prompt") or _new_ai_prompt_state()
                    pb["root"] = "video"
                    pb["provider"] = provider
                    pb["step"] = "await_input"
                    st["ai_chat_mode"] = "prompt"
                    st["ai_prompt"] = pb
                    st["ts"] = _now()
                    await tg_send_message(
                        chat_id,
                        _ai_prompt_waiting_text(pb),
                        reply_markup=_ai_prompt_tools_inline_kb(pb),
                    )
                    return {"ok": True}

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
                await tg_send_document_bytes(chat_id, b, filename=f"original.{meta.get('ext','png')}", caption="⬇️ Оригинал (без сжатия)")
            except Exception:
                await tg_send_message(chat_id, "Не смог отправить оригинал файлом. Попробуй ещё раз.")
            return {"ok": True}
            
        if chat_id and user_id and data.startswith("seedance_extend:"):
            prev_task_id = data.split(":", 1)[1].strip()
            if not prev_task_id:
                await tg_answer_callback_query(str(cq_id), text="Не найден task_id для продолжения.", show_alert=True)
                return {"ok": True}

            st = _ensure_state(chat_id, user_id)
            se = st.get("seedance_extend") or {}
            se["extend_from_task_id"] = prev_task_id
            st["seedance_extend"] = se
            st["ts"] = _now()

            se = st.get("seedance_extend") or {}
            task_type = str(se.get("task_type") or "seedance-2-preview")
            if task_type == "seedance-2-mini":
                await tg_send_message(chat_id, "Seedance 2.0 Mini сейчас без продолжения сцены. Запусти новую генерацию.", reply_markup=_help_menu_for(user_id))
                return {"ok": True}

            # Legacy PiAPI continuation pricing.
            preview_price_map = {5: 6, 10: 12, 15: 18} if task_type == "seedance-2-fast-preview" else {5: 12, 10: 24, 15: 33}
            cost_5 = int(preview_price_map[5])
            cost_10 = int(preview_price_map[10])
            cost_15 = int(preview_price_map[15])

            await tg_send_message(
                chat_id,
                "Выберите длительность продолжения:",
                reply_markup={
                    "inline_keyboard": [
                        [{"text": f"5 сек • {cost_5} токенов", "callback_data": "seedance_extend_dur:5"}],
                        [{"text": f"10 сек • {cost_10} токенов", "callback_data": "seedance_extend_dur:10"}],
                        [{"text": f"15 сек • {cost_15} токенов", "callback_data": "seedance_extend_dur:15"}],
                    ]
                },
            )
            return {"ok": True}
            
        if chat_id and user_id and data.startswith("seedance_extend_dur:"):
            try:
                duration = int(data.split(":", 1)[1].strip())
            except Exception:
                await tg_answer_callback_query(str(cq_id), text="Некорректная длительность.", show_alert=True)
                return {"ok": True}

            if duration not in (5, 10, 15):
                await tg_answer_callback_query(str(cq_id), text="Некорректная длительность.", show_alert=True)
                return {"ok": True}

            st = _ensure_state(chat_id, user_id)
            se = st.get("seedance_extend") or {}
            se["duration"] = duration

            seedance_settings = st.get("seedance_settings") or {}
            task_type = str((st.get("seedance_extend") or {}).get("task_type") or seedance_settings.get("task_type") or "seedance-2-preview")

            # Legacy PiAPI continuation pricing.
            preview_price_map = {5: 6, 10: 12, 15: 18} if task_type == "seedance-2-fast-preview" else {5: 12, 10: 24, 15: 33}
            cost_tokens = int(preview_price_map.get(int(duration), preview_price_map[5]))

            se["cost_tokens"] = cost_tokens
            st["seedance_extend"] = se
            st["ts"] = _now()

            _set_mode(chat_id, user_id, "seedance_extend_prompt")

            await tg_send_message(
                chat_id,
                f"Введите промпт для продолжения сцены.\n"
                f"Будет списано: {cost_tokens} токенов.",
                reply_markup=_help_menu_for(user_id),
            )
            return {"ok": True}
            
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
                amount_rub = int(pack.get("rub") or 0)
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
        payload = (pre.get("invoice_payload") or "").strip()
        pre_user = pre.get("from") or {}
        pre_user_id = int(pre_user.get("id") or 0)

        ok = True
        err = ""

        # Admin-only Stars invoice: accept only exact 200-token admin payload
        # and only from ADMIN_IDS. Client topups keep the old behavior.
        admin_payload = _parse_admin_stars_200_payload(payload)
        if payload.startswith("admin_stars_200"):
            if not admin_payload:
                ok = False
                err = "Некорректный админский Stars-платёж."
            elif not _is_admin(pre_user_id):
                ok = False
                err = "Этот Stars-платёж доступен только админу."
            elif int(admin_payload.get("admin_user_id") or 0) != pre_user_id:
                ok = False
                err = "Платёж создан для другого администратора."

        if cq_id:
            await tg_answer_pre_checkout_query(str(cq_id), ok=ok, error_message=err)
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
    
    # --- Queue test: /qtest ---
    incoming_text = (message.get("text") or "").strip()
    if incoming_text in ("/qtest", "qtest"):
        job_id = uuid4().hex
        await enqueue_job({"job_id": job_id, "type": "qtest", "chat_id": chat_id, "user_id": user_id}, queue_name="gen")
        await tg_send_message(chat_id, f"🧪 Задача поставлена в очередь. job_id={job_id}")
        return {"ok": True}

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

        # Admin-only package: +200 tokens for ADMIN_IDS, paid via Telegram Stars.
        admin_payload = _parse_admin_stars_200_payload(payload)
        if payload.startswith("admin_stars_200"):
            if not admin_payload:
                await tg_send_message(chat_id, "Оплата прошла, но админский payload некорректный. Напиши админу.", reply_markup=_main_menu_for(user_id))
                return {"ok": True}

            if not _is_admin(user_id):
                if ADMIN_IDS:
                    try:
                        admin_id = next(iter(ADMIN_IDS))
                        await tg_send_message(admin_id, f"⚠️ Не-админ оплатил admin_stars_200: user={user_id} payload={payload}")
                    except Exception:
                        pass
                await tg_send_message(chat_id, "Оплата прошла, но этот пакет доступен только админу. Напиши админу.", reply_markup=_main_menu_for(user_id))
                return {"ok": True}

            if int(admin_payload.get("admin_user_id") or 0) != user_id:
                await tg_send_message(chat_id, "Оплата прошла, но user_id админского платежа не совпал. Напиши админу.", reply_markup=_main_menu_for(user_id))
                return {"ok": True}

            tokens = int(admin_payload["tokens"])
            ref_id = _payment_ledger_ref(provider=ADMIN_STARS_200_PROVIDER, charge_id=tg_charge_id, payload=payload)

            try:
                if not ledger_ref_exists(reason=ADMIN_STARS_200_REASON, ref_id=ref_id):
                    ensure_user_row(user_id)
                    add_tokens(
                        user_id,
                        tokens,
                        reason=ADMIN_STARS_200_REASON,
                        ref_id=ref_id,
                        meta={
                            "tokens": tokens,
                            "stars": total_amount,
                            "currency": "XTR",
                            "payload": payload,
                            "charge_id": tg_charge_id,
                            "provider": ADMIN_STARS_200_PROVIDER,
                            "admin_only": True,
                        },
                    )
                    # Не отправляем admin-only покупку в партнёрскую/реферальную аналитику:
                    # это служебное пополнение через Stars, а не клиентская оплата.

                bal = int(get_balance(user_id) or 0)
                await tg_send_message(
                    chat_id,
                    f"✅ Админский Stars-платёж прошёл!\nНачислено: +{tokens} токенов\nБаланс: {bal}",
                    reply_markup=_main_menu_for(user_id),
                )
            except Exception as e:
                if ADMIN_IDS:
                    try:
                        admin_id = next(iter(ADMIN_IDS))
                        await tg_send_message(admin_id, f"❌ Admin Stars начисление упало: {e}\nuser={user_id} payload={payload}")
                    except Exception:
                        pass
                await tg_send_message(chat_id, f"Оплата прошла, но не смог начислить токены: {e}", reply_markup=_main_menu_for(user_id))
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
            try:
                pack_for_partner = _find_pack_by_tokens(tokens) or {}
                amount_rub_for_partner = float(pack_for_partner.get("rub") or 0)
            except Exception:
                amount_rub_for_partner = 0.0
            await _enqueue_partner_topup_event(
                user_id=user_id,
                payment_id=f"stars:{tg_charge_id or payload}",
                amount_rub=amount_rub_for_partner,
                tokens=tokens,
                provider="telegram_stars",
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

    # Красивая reply-кнопка должна вести себя как реальный /reset
    if incoming_text == "🔄 Сбросить генерацию":
        incoming_text = "/reset"

    # /reset — сбросить текущий режим/зависшие состояния (должен срабатывать даже во время busy-lock)
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
        try:
            await reset_tg_chat_memory(chat_id, user_id)
        except Exception:
            pass
        await tg_send_message(chat_id, "✅ Сброс выполнен. Возвращаю в главное меню.", reply_markup=_main_menu_for(user_id))
        return {"ok": True}

    # ---------------- Голосовые сообщения в режиме ИИ-чата ----------------
    voice = message.get("voice") or {}
    if voice and st.get("mode") == "chat":
        if not AI_CHAT_VOICE_ENABLED:
            await tg_send_message(chat_id, "Голосовой ввод в ИИ-чате сейчас отключён.", reply_markup=_main_menu_for(user_id))
            return {"ok": True}

        if st.get("ai_chat_mode") != "chat":
            await tg_send_message(
                chat_id,
                "Голосовое получил, но сначала выбери модель чата: Claude Sonnet, Claude Opus 4.7, Claude Fable 5 или ChatGPT.",
                reply_markup=_ai_chat_mode_inline_kb(),
            )
            return {"ok": True}

        file_id = str(voice.get("file_id") or "").strip()
        duration = int(voice.get("duration") or 0)
        size_bytes = int(voice.get("file_size") or 0)

        if not file_id:
            await tg_send_message(chat_id, "Не смог прочитать file_id голосового. Отправь голосовое ещё раз.", reply_markup=_main_menu_for(user_id))
            return {"ok": True}

        if AI_CHAT_VOICE_MAX_SECONDS > 0 and duration > AI_CHAT_VOICE_MAX_SECONDS:
            await tg_send_message(
                chat_id,
                f"Голосовое слишком длинное. Сейчас лимит для ИИ-чата: до {AI_CHAT_VOICE_MAX_SECONDS} сек.",
                reply_markup=_main_menu_for(user_id),
            )
            return {"ok": True}

        if size_bytes > AI_CHAT_VOICE_MAX_BYTES:
            mb = max(1, AI_CHAT_VOICE_MAX_BYTES // (1024 * 1024))
            await tg_send_message(chat_id, f"Голосовое слишком большое. Лимит: до {mb} МБ.", reply_markup=_main_menu_for(user_id))
            return {"ok": True}

        try:
            await tg_send_chat_action(chat_id, "typing")
        except Exception:
            pass

        model_key = _ai_chat_model_key(st)
        is_fable = _is_fable_chat_model_key(model_key)
        thinking = _tg_fable_thinking_enabled(st) if is_fable else True
        charge_ref_id = ""
        charge_tokens = 0
        free_chat_consumed = False

        # Резервируем оплату/Free-лимит до постановки STT в очередь.
        # Если STT или постановка chat-job упадёт, worker_redactor.py сделает refund/release.
        if is_fable:
            charge_tokens = _tg_fable_chat_cost_tokens(st, has_files=False)
            charge_ref_id = await _tg_charge_fable_chat_or_notify(chat_id=chat_id, user_id=user_id, st=st, has_files=False)
            if not charge_ref_id:
                st["ts"] = _now()
                return {"ok": True}
        else:
            if not await _tg_consume_free_chat_or_notify(chat_id, user_id):
                st["ts"] = _now()
                return {"ok": True}
            free_chat_consumed = True

        st["ts"] = _now()
        try:
            await enqueue_job(
                {
                    "job_id": f"tg_stt:{user_id}:{message_id or int(_now() * 1000)}:{uuid4().hex}",
                    "kind": "tg_stt_voice",
                    "chat_id": int(chat_id),
                    "user_id": int(user_id),
                    "file_id": file_id,
                    "duration": int(duration or 0),
                    "size_bytes": int(size_bytes or 0),
                    "original_message_id": int(message_id or 0),
                    "original_update_id": update.get("update_id"),
                    "model_key": model_key,
                    "model": _ai_chat_model_actual(model_key),
                    "system_prompt": _ai_chat_system_prompt(model_key),
                    "thinking": bool(thinking),
                    "charge_tokens": int(charge_tokens or 0),
                    "charge_ref_id": str(charge_ref_id or ""),
                    "refund_reason": "claude_fable_chat_refund" if is_fable else "",
                    "free_chat_consumed": bool(free_chat_consumed),
                    "reply_markup": _ai_chat_mode_inline_kb(bool(thinking) if is_fable else False, selected_model=model_key),
                    "received_ts": _now(),
                    "source": "main.py:voice_chat",
                },
                queue_name=TG_STT_QUEUE_NAME,
            )
        except Exception as e:
            if charge_ref_id:
                try:
                    add_tokens(user_id, int(charge_tokens), reason="claude_fable_chat_refund", ref_id=charge_ref_id, meta={"stage": "stt_enqueue_failed", "source": "telegram_voice", "error": str(e)[:300]})
                except Exception:
                    pass
            elif free_chat_consumed:
                try:
                    release_free_usage(user_id, FEATURE_CHAT)
                except Exception:
                    pass
            await tg_send_message(
                chat_id,
                f"❌ Не удалось поставить голосовое в очередь распознавания. Проверь REDIS_URL и worker_redactor.py.\n{e}",
                reply_markup=_main_menu_for(user_id),
            )
            return {"ok": True}

        await tg_send_message(
            chat_id,
            "🎙 Голосовое принято. Распознаю текст и передам его в ИИ-чат.",
            reply_markup=_main_menu_for(user_id),
        )
        return {"ok": True}

    # ---------------- Audio (message.audio) для Seedance Omni ----------------
    audio_msg = message.get("audio") or {}
    if audio_msg and st.get("mode") == "seedance_omni":
        so = st.get("seedance_omni") or {}
        step = (so.get("step") or "collect_refs")
        if step != "collect_refs":
            await tg_send_message(chat_id, "Аудио refs уже собраны ✅ Теперь жду промпт текстом. Если хочешь добавить refs — нажми «⬅️ Вернуться к refs».", reply_markup=_seedance_prompt_back_kb())
            return {"ok": True}
        file_id = str(audio_msg.get("file_id") or "").strip()
        mime_type = str(audio_msg.get("mime_type") or "").lower()
        duration_sec = int(audio_msg.get("duration") or 0)
        if not file_id:
            await tg_send_message(chat_id, "Не смог прочитать audio file_id. Отправь аудио ещё раз.", reply_markup=_seedance_refs_collect_kb())
            return {"ok": True}
        if duration_sec and duration_sec > 15:
            await tg_send_message(chat_id, "Audio reference слишком длинный. Для Seedance Omni максимум 15 секунд.", reply_markup=_seedance_refs_collect_kb())
            return {"ok": True}
        try:
            size_bytes = int(audio_msg.get("file_size") or 0)
        except Exception:
            size_bytes = 0
        if size_bytes and size_bytes > SEEDANCE_AUDIO_MAX_UPLOAD_BYTES:
            await tg_send_message(chat_id, f"Audio reference слишком большой. Лимит: до {SEEDANCE_AUDIO_MAX_UPLOAD_MB} МБ.", reply_markup=_seedance_refs_collect_kb())
            return {"ok": True}
        settings = st.get("seedance_settings") or {}
        audio_limit = int(settings.get("max_audios") or 3)
        total_limit = int(settings.get("max_total_refs") or 12)
        image_ids = list(so.get("image_file_ids") or [])
        video_ids = list(so.get("video_file_ids") or [])
        audio_ids = list(so.get("audio_file_ids") or [])
        if len(audio_ids) >= audio_limit:
            await tg_send_message(chat_id, f"Аудио refs уже {audio_limit}/{audio_limit}. Пришли другие refs или нажми «✅ Готово».", reply_markup=_seedance_refs_collect_kb())
            return {"ok": True}
        if len(image_ids) + len(video_ids) + len(audio_ids) >= total_limit:
            await tg_send_message(chat_id, f"Всего refs уже {total_limit}/{total_limit}. Нажми «✅ Готово», чтобы перейти к промпту.", reply_markup=_seedance_refs_collect_kb())
            return {"ok": True}
        if file_id not in audio_ids:
            audio_ids.append(file_id)
        so["audio_file_ids"] = audio_ids[:audio_limit]
        st["seedance_omni"] = so
        st["ts"] = _now()
        await tg_send_message(chat_id, f"Аудио reference #{len(so['audio_file_ids'])} получил ✅\nПришли ещё refs или нажми «✅ Готово».", reply_markup=_seedance_refs_collect_kb())
        return {"ok": True}

    # ---------------- Voice для Seedance Omni: принимаем как audio ref и конвертируем в MP3 в worker ----------------
    if voice and st.get("mode") == "seedance_omni":
        so = st.get("seedance_omni") or {}
        step = (so.get("step") or "collect_refs")
        if step != "collect_refs":
            await tg_send_message(chat_id, "Аудио refs уже собраны ✅ Теперь жду промпт текстом. Если хочешь добавить refs — нажми «⬅️ Вернуться к refs».", reply_markup=_seedance_prompt_back_kb())
            return {"ok": True}
        file_id = str(voice.get("file_id") or "").strip()
        duration_sec = int(voice.get("duration") or 0)
        if not file_id:
            await tg_send_message(chat_id, "Не смог прочитать file_id голосового. Запиши голосовое ещё раз.", reply_markup=_seedance_refs_collect_kb())
            return {"ok": True}
        if duration_sec and duration_sec > 15:
            await tg_send_message(chat_id, "Голосовой audio reference слишком длинный. Для Seedance Omni максимум 15 секунд.", reply_markup=_seedance_refs_collect_kb())
            return {"ok": True}
        try:
            size_bytes = int(voice.get("file_size") or 0)
        except Exception:
            size_bytes = 0
        if size_bytes and size_bytes > SEEDANCE_AUDIO_MAX_UPLOAD_BYTES:
            await tg_send_message(chat_id, f"Голосовой audio reference слишком большой. Лимит: до {SEEDANCE_AUDIO_MAX_UPLOAD_MB} МБ.", reply_markup=_seedance_refs_collect_kb())
            return {"ok": True}
        settings = st.get("seedance_settings") or {}
        audio_limit = int(settings.get("max_audios") or 3)
        total_limit = int(settings.get("max_total_refs") or 12)
        image_ids = list(so.get("image_file_ids") or [])
        video_ids = list(so.get("video_file_ids") or [])
        audio_ids = list(so.get("audio_file_ids") or [])
        if len(audio_ids) >= audio_limit:
            await tg_send_message(chat_id, f"Аудио refs уже {audio_limit}/{audio_limit}. Пришли другие refs или нажми «✅ Готово».", reply_markup=_seedance_refs_collect_kb())
            return {"ok": True}
        if len(image_ids) + len(video_ids) + len(audio_ids) >= total_limit:
            await tg_send_message(chat_id, f"Всего refs уже {total_limit}/{total_limit}. Нажми «✅ Готово», чтобы перейти к промпту.", reply_markup=_seedance_refs_collect_kb())
            return {"ok": True}
        if file_id not in audio_ids:
            audio_ids.append(file_id)
        so["audio_file_ids"] = audio_ids[:audio_limit]
        st["seedance_omni"] = so
        st["ts"] = _now()
        await tg_send_message(chat_id, f"Голосовой audio reference #{len(so['audio_file_ids'])} получил ✅\nКонвертирую в MP3 при запуске. Пришли ещё refs или нажми «✅ Готово».", reply_markup=_seedance_refs_collect_kb())
        return {"ok": True}


    # ---------------- Документы в режиме ИИ-чата ----------------
    document = message.get("document") or {}
    if document and st.get("mode") == "chat":
        filename = str(document.get("file_name") or "file").strip() or "file"
        file_id = str(document.get("file_id") or "").strip()
        mime_type = str(document.get("mime_type") or "application/octet-stream").strip()
        size_bytes = int(document.get("file_size") or 0)

        if not file_id:
            await tg_send_message(chat_id, "Не смог прочитать file_id файла. Отправь файл ещё раз.", reply_markup=_main_menu_for(user_id))
            return {"ok": True}
        if size_bytes > AI_CHAT_FILE_MAX_BYTES:
            await tg_send_message(chat_id, "Файл больше 10 МБ. Для бесплатного ИИ-чата можно отправлять файлы до 10 МБ.", reply_markup=_main_menu_for(user_id))
            return {"ok": True}

        if st.get("ai_chat_mode") != "chat":
            await tg_send_message(
                chat_id,
                "Файл получил, но сначала выбери модель чата: Claude Sonnet, Claude Opus 4.7, Claude Fable 5 или ChatGPT.",
                reply_markup=_ai_chat_mode_inline_kb(),
            )
            return {"ok": True}

        model_key = _ai_chat_model_key(st)
        charge_ref_id = ""
        charge_tokens = 0
        if _is_fable_chat_model_key(model_key):
            charge_tokens = _tg_fable_chat_cost_tokens(st, has_files=True)
            charge_ref_id = await _tg_charge_fable_chat_or_notify(chat_id=chat_id, user_id=user_id, st=st, has_files=True)
            if not charge_ref_id:
                st["ts"] = _now()
                return {"ok": True}
        else:
            if not await _tg_consume_free_chat_or_notify(chat_id, user_id):
                st["ts"] = _now()
                return {"ok": True}

        queued = await _enqueue_tg_ai_chat_job(
            chat_id=chat_id,
            user_id=user_id,
            text=incoming_text or "Проанализируй приложенный файл и дай краткий полезный вывод.",
            model_key=model_key,
            file_meta={
                "filename": filename,
                "file_id": file_id,
                "mime_type": mime_type,
                "size_bytes": size_bytes,
            },
            thinking=_tg_fable_thinking_enabled(st) if _is_fable_chat_model_key(model_key) else True,
            charge_tokens=charge_tokens,
            charge_ref_id=charge_ref_id,
        )
        if queued:
            return {"ok": True}

        if charge_ref_id:
            try:
                add_tokens(user_id, int(charge_tokens), reason="claude_fable_chat_refund", ref_id=charge_ref_id, meta={"stage": "enqueue_failed", "source": "telegram"})
            except Exception:
                pass
        else:
            release_free_usage(user_id, FEATURE_CHAT)
        await tg_send_message(chat_id, "❌ Не удалось поставить чат в очередь. Проверь REDIS_URL и worker_chat.py.", reply_markup=_main_menu_for(user_id))
        return {"ok": True}

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

            ai_choice = _normalize_music_ai_choice(settings)

            # генерация стартует — ожидание текста больше не нужно
            sb_clear_user_state(user_id)

            # ---- BILLING: Suno fixed price ----
            suno_cost_tokens = 2
            suno_charged = False
            if ai_choice == "suno":
                try:
                    ensure_user_row(user_id)
                    bal = int(get_balance(user_id) or 0)
                except Exception:
                    bal = 0

                if bal < suno_cost_tokens:
                    try:
                        sb_set_user_state(user_id, "music_wait_text", settings)
                    except Exception:
                        pass

                    await tg_send_message(
                        chat_id,
                        f"❌ Недостаточно токенов для Suno\nНужно: {suno_cost_tokens}\nБаланс: {bal}",
                        reply_markup=_topup_balance_inline_kb(),
                    )
                    return {"ok": True}

                try:
                    add_tokens(
                        user_id,
                        -suno_cost_tokens,
                        reason="suno_music",
                        meta={
                            "ai": ai_choice,
                            "provider": str(settings.get("provider") or ""),
                            "cost_tokens": suno_cost_tokens,
                        },
                    )
                    suno_charged = True
                except TypeError:
                    add_tokens(
                        user_id,
                        -int(suno_cost_tokens),
                        reason="suno_music",
                        meta={
                            "ai": ai_choice,
                            "provider": str(settings.get("provider") or ""),
                            "cost_tokens": int(suno_cost_tokens),
                        },
                    )
                    suno_charged = True

            try:
                await _enqueue_music_job(
                    chat_id=int(chat_id),
                    user_id=int(user_id),
                    settings=settings,
                    charge_tokens=(suno_cost_tokens if suno_charged else 0),
                )
            except Exception as e:
                try:
                    if suno_charged:
                        try:
                            add_tokens(user_id, suno_cost_tokens, reason="suno_music_refund")
                        except TypeError:
                            add_tokens(user_id, int(suno_cost_tokens), reason="suno_music_refund")
                except Exception:
                    pass
                try:
                    sb_set_user_state(user_id, "music_wait_text", settings)
                except Exception:
                    pass
                await tg_send_message(chat_id, f"❌ Не удалось поставить музыку в очередь: {e}", reply_markup=_main_menu_for(user_id))
                return {"ok": True}

            _clear_music_ctx(st, chat_id, user_id)
            await tg_send_message(
                chat_id,
                "⏳ Музыка: Начинаю генерацию. Как будет готово — пришлю трек.",
                reply_markup=_help_menu_for(user_id),
            )
            return {"ok": True}

        

                # ----- WebApp data (Seedance 2.0 / Preview settings) -----
        # Expected mini: {type:"seedance_settings", provider:"seedance", seedance_model:"mini", flow:"text|image|omni", ...}
        # Expected regular KIE: {type:"seedance_settings", provider:"seedance_kie", seedance_model:"seedance-kie-480p|seedance-kie-720p|seedance-kie-1080p", flow:"text|image|omni", ...}
        seedance_provider_raw = str(payload.get("provider") or provider_raw or "").lower().strip()
        seedance_type_raw = str(payload.get("type") or "").lower().strip()
        seedance_model_raw = str(payload.get("seedance_model") or payload.get("model") or payload.get("preset") or "").lower().strip()
        seedance_variant_raw = str(payload.get("seedance_variant") or payload.get("variant") or "").lower().strip()
        seedance_task_type_raw = str(payload.get("task_type") or payload.get("taskType") or "").lower().strip()

        kie_seedance_models = (
            "seedance-kie-480p", "seedance-kie-720p", "seedance-kie-1080p",
            "seedance-kie", "seedance-kie-fast", "seedance-2", "seedance-2-fast",
        )
        is_seedance = (
            seedance_type_raw in ("seedance_settings", "seedance2_settings", "seedance_2_settings")
            or seedance_provider_raw in ("seedance", "seedance2", "seedance_2", "seedance_kie", "seedance-kie")
            or seedance_model_raw in ("mini", "seedance-mini", "seedance-2-mini", "preview", "fast", "seedance-2-preview", "seedance-2-fast-preview", *kie_seedance_models)
            or seedance_task_type_raw in ("seedance-2-mini", "seedance-2-preview", "seedance-2-fast-preview", "seedance-2", "seedance-2-fast")
        )

        if is_seedance:
            provider_kind = "seedance"
            if (
                seedance_provider_raw in ("seedance_kie", "seedance-kie")
                or seedance_variant_raw == "kie"
                or seedance_model_raw in kie_seedance_models
                or seedance_task_type_raw in ("seedance-2", "seedance-2-fast")
            ):
                provider_kind = "seedance_kie"

            flow = str(payload.get("flow") or payload.get("gen_mode") or payload.get("mode") or "text").lower().strip()
            if flow not in ("text", "image", "omni"):
                flow = "text"

            try:
                duration = int(payload.get("duration") or 5)
            except Exception:
                duration = 5
            if duration not in (5, 10, 15):
                duration = 5

            aspect_ratio = str(payload.get("aspect_ratio") or "16:9").strip()
            if provider_kind == "seedance_kie":
                if aspect_ratio not in ("16:9", "9:16", "1:1"):
                    aspect_ratio = "16:9"
                model_map = {
                    "seedance-kie-fast": "seedance-kie-480p",
                    "seedance-2-fast": "seedance-kie-480p",
                    "seedance-kie": "seedance-kie-720p",
                    "seedance-2": "seedance-kie-720p",
                }
                seedance_model = model_map.get(seedance_model_raw, seedance_model_raw or "seedance-kie-480p")
                if seedance_model not in ("seedance-kie-480p", "seedance-kie-720p", "seedance-kie-1080p"):
                    seedance_model = "seedance-kie-480p"
                task_type = "seedance-2-fast" if seedance_model == "seedance-kie-480p" else "seedance-2"
                max_images = 2 if flow == "image" else 7
                max_videos = 3
                max_audios = 3
                max_total_refs = 12
            else:
                if aspect_ratio not in ("16:9", "9:16", "1:1"):
                    aspect_ratio = "16:9"
                # Seedance 2.0 Mini stays a separate UI model, but now runs through KIE mini backend.
                seedance_model = "seedance-kie-mini"
                task_type = "seedance-2-mini"
                if flow == "omni":
                    max_images = 7
                    max_videos = 3
                    max_audios = 3
                    max_total_refs = 12
                else:
                    max_images = 2 if flow == "image" else 0
                    max_videos = 0
                    max_audios = 0
                    max_total_refs = 2 if flow == "image" else 0

            st["seedance_settings"] = {
                "provider_kind": provider_kind,
                "seedance_model": seedance_model,
                "task_type": task_type,
                "flow": flow,
                "duration": duration,
                "aspect_ratio": aspect_ratio,
                "max_images": max_images,
                "max_videos": max_videos,
                "max_audios": max_audios,
                "max_total_refs": max_total_refs,
            }
            st["ts"] = _now()

            if flow == "text":
                _set_mode(chat_id, user_id, "seedance_t2v")
                st["seedance_t2v"] = {"step": "need_prompt"}
                st["ts"] = _now()
                await tg_send_message(
                    chat_id,
                    ("✅ Настройки Seedance 2.0 сохранены.\n\nПришли промпт одним или несколькими сообщениями. Когда всё отправишь — нажми «✅ Запустить»."
                     if provider_kind == "seedance_kie" else
                     "✅ Настройки Seedance 2.0 Mini сохранены.\n\nПришли промпт одним или несколькими сообщениями. Когда всё отправишь — нажми «✅ Запустить»."),
                    reply_markup=_seedance_prompt_collect_kb("seedance_t2v"),
                )
                return {"ok": True}

            if flow == "omni":
                _set_mode(chat_id, user_id, "seedance_omni")
                st["seedance_omni"] = {
                    "step": "collect_refs",
                    "image_file_ids": [],
                    "video_file_ids": [],
                    "video_durations_sec": [],
                    "audio_file_ids": [],
                    "prompt": None,
                }
                st["ts"] = _now()
                await tg_send_message(
                    chat_id,
                    ("✅ Настройки Seedance 2.0 Omni сохранены.\n\n"
                     if provider_kind == "seedance_kie" else
                     "✅ Настройки Seedance 2.0 Mini Omni сохранены.\n\n")
                    + "Теперь пришли референсы: можно только фото, либо фото/видео/аудио вместе.\n"
                    + "Аудио можно файлом или голосовым сообщением — я конвертирую в MP3. Audio-only нельзя. Когда закончишь — нажми «✅ Готово».",
                    reply_markup=_seedance_refs_collect_kb(),
                )
                return {"ok": True}

            _set_mode(chat_id, user_id, "seedance_i2v")
            st["seedance_i2v"] = {
                "step": "need_images",
                "image_file_ids": [],
                "prompt": None,
            }
            st["ts"] = _now()
            if provider_kind == "seedance_kie":
                msg = (
                    "✅ Настройки Seedance 2.0 Image → Video сохранены.\n\nТеперь пришли 1–2 ФОТО. "
                    "Если пришлёшь 2 фото — второе будет last frame. После фото нажми «✅ Готово», затем пришли промпт частями и нажми «✅ Запустить»."
                )
            else:
                msg = (
                    "✅ Настройки Seedance 2.0 Mini Image → Video сохранены.\n\nТеперь пришли 1–2 ФОТО. "
                    "Первое фото будет first frame, второе — optional last frame. После фото нажми «✅ Готово», затем пришли промпт частями и нажми «✅ Запустить»."
                )
            await tg_send_message(chat_id, msg, reply_markup=_seedance_refs_collect_kb())
            return {"ok": True}

# ----- WebApp data (Sora 2 settings) -----
        # Expected: {type:"sora_settings", provider:"sora", duration:4|8|12, aspect_ratio:"16:9|9:16"}
        is_sora = (
            (str(payload.get("type") or "").lower().strip() == "sora_settings")
            or (provider_raw == "sora")
        ) and (str(payload.get("provider") or provider_raw or "").lower().strip() == "sora")

        if is_sora:
            try:
                duration = int(payload.get("duration") or 4)
            except Exception:
                duration = 4
            if duration not in (4, 8, 12):
                duration = 4

            aspect_ratio = str(payload.get("aspect_ratio") or "16:9").strip()
            if aspect_ratio not in ("16:9", "9:16"):
                aspect_ratio = "16:9"

            st["sora_settings"] = {
                "model": "sora-2",
                "flow": "text",
                "duration": duration,
                "aspect_ratio": aspect_ratio,
            }
            st["ts"] = _now()

            _set_mode(chat_id, user_id, "sora_t2v")
            st["sora_t2v"] = {"step": "need_prompt"}
            st["ts"] = _now()
            await tg_send_message(
                chat_id,
                "✅ Настройки Sora 2 сохранены.\n\nТеперь пришли ТЕКСТ (промпт), что должно быть в видео.",
                reply_markup=_help_menu_for(user_id),
            )
            return {"ok": True}

# ----- WebApp data (Veo settings) -----
        # Expected (from our WebApp): {type:"veo_settings", provider:"veo", veo_model:"fast|pro|fast_relax", flow:"text|image",
        # duration, aspect_ratio, resolution, use_last_frame, use_reference_images, generate_audio only for fast/pro}
        is_veo = (
            (str(payload.get("type") or "").lower().strip() == "veo_settings")
            or (provider_raw == "veo")
            or (feature_raw in ("video_future", "video"))
        ) and (str(payload.get("provider") or provider_raw or "").lower().strip() == "veo")

        if is_veo:
            veo_model = str(payload.get("veo_model") or payload.get("model") or "fast").lower().strip()
            if veo_model in ("veo-3.1", "3.1"):
                veo_model = "pro"
            elif veo_model in ("fast_relax", "relax", "veo-3.1-fast-relax", "veo_fast_relax", "veo31_fast_relax"):
                veo_model = "fast_relax"
            elif veo_model not in ("fast", "pro"):
                veo_model = "fast"

            flow = str(payload.get("flow") or "text").lower().strip()
            if flow not in ("text", "image"):
                flow = "text"

            try:
                duration = int(payload.get("duration") or 8)
            except Exception:
                duration = 8
            if veo_model == "fast_relax":
                duration = normalize_veo31_fast_relax_duration(duration)
            elif duration not in (4, 6, 8):
                duration = 8

            aspect_ratio = str(payload.get("aspect_ratio") or "16:9").strip()
            if veo_model == "fast_relax":
                aspect_ratio = normalize_veo31_fast_relax_aspect_ratio(aspect_ratio)
            elif aspect_ratio not in ("16:9", "9:16"):
                aspect_ratio = "16:9"

            generate_audio = None if veo_model == "fast_relax" else bool(payload.get("generate_audio"))

            resolution = str(payload.get("resolution") or ("1080p" if veo_model in ("pro", "fast_relax") else "720p")).lower().strip()
            if veo_model == "pro":
                resolution = "1080p"
            elif veo_model == "fast_relax":
                resolution = normalize_veo31_fast_relax_resolution(resolution)
            else:
                resolution = "720p"

            use_last_frame = bool(payload.get("use_last_frame")) if (flow == "image" and veo_model in ("pro", "fast_relax")) else False
            use_reference_images = bool(payload.get("use_reference_images")) if (
                veo_model == "pro" and flow == "image" and aspect_ratio == "16:9" and duration == 8
            ) else False

            # PiAPI / Veo 3.1 refs: только Image→Video, 16:9, 8s, 1-3 refs.
            # Если refs включены, tail/last frame игнорируется — делаем это явным и на нашей стороне.
            if use_reference_images:
                use_last_frame = False
            if veo_model == "fast_relax":
                use_reference_images = False

            model_slug = "veo-3.1-fast-relax" if veo_model == "fast_relax" else ("veo-3.1" if veo_model == "pro" else "veo-3-fast")
            display_name = VEO31_FAST_RELAX_DISPLAY_NAME if veo_model == "fast_relax" else ("Veo 3.1" if veo_model == "pro" else "Veo Fast")

            veo_settings = {
                "model": model_slug,
                "veo_model": veo_model,
                "flow": flow,
                "duration": duration,
                "aspect_ratio": aspect_ratio,
                "resolution": resolution,
                "use_last_frame": use_last_frame,
                "use_reference_images": use_reference_images,
                "display_name": display_name,
            }
            if veo_model != "fast_relax":
                veo_settings["generate_audio"] = bool(generate_audio)
            st["veo_settings"] = veo_settings
            st["ts"] = _now()

            if flow == "text":
                _set_mode(chat_id, user_id, "veo_t2v")
                st["veo_t2v"] = {"step": "need_prompt"}
                st["ts"] = _now()
                await tg_send_message(
                    chat_id,
                    f"✅ Настройки {display_name} сохранены.\n\nДлительность: {duration} сек.\nКачество: {resolution}.\n\nТеперь пришли ТЕКСТ (промпт), что должно быть в видео.\nПример: «Кот в скафандре идёт по Марсу, кинематографично».",
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
                    extra.append("• референсы (до 3)")
                extra_txt = ("\nДополнительно понадобится: " + ", ".join(extra)) if extra else ""
                refs_note = ""
                if use_reference_images:
                    refs_note = "\n\nℹ️ Refs работают только в Veo 3.1 Pro + Image → Video + 16:9 + 8 сек. Максимум: 3 фото. При refs last frame не используется."
                elif use_last_frame:
                    refs_note = "\n\nℹ️ Last frame работает как финальный кадр перехода."
                await tg_send_message(
                    chat_id,
                    f"✅ Настройки {display_name} сохранены (Image → Video).\n\nДлительность: {duration} сек.\nКачество: {resolution}.\n\nШаг 1) Пришли СТАРТОВОЕ фото (кадр 1)." + extra_txt + refs_note,
                    reply_markup=_help_menu_for(user_id),
                )
                return {"ok": True}


            ai_choice = _normalize_music_ai_choice(settings)

            # генерация стартует — ожидание текста больше не нужно
            sb_clear_user_state(user_id)

            # ---- BILLING: Suno fixed price ----
            suno_cost_tokens = 2
            suno_charged = False
            if ai_choice == "suno":
                try:
                    ensure_user_row(user_id)
                    bal = int(get_balance(user_id) or 0)
                except Exception:
                    bal = 0

                if bal < suno_cost_tokens:
                    try:
                        sb_set_user_state(user_id, "music_wait_text", settings)
                    except Exception:
                        pass

                    await tg_send_message(
                        chat_id,
                        f"❌ Недостаточно токенов для Suno\nНужно: {suno_cost_tokens}\nБаланс: {bal}",
                        reply_markup=_topup_balance_inline_kb(),
                    )
                    return {"ok": True}

                try:
                    add_tokens(
                        user_id,
                        -suno_cost_tokens,
                        reason="suno_music",
                        meta={
                            "ai": ai_choice,
                            "provider": str(settings.get("provider") or ""),
                            "cost_tokens": suno_cost_tokens,
                        },
                    )
                    suno_charged = True
                except TypeError:
                    add_tokens(
                        user_id,
                        -int(suno_cost_tokens),
                        reason="suno_music",
                        meta={
                            "ai": ai_choice,
                            "provider": str(settings.get("provider") or ""),
                            "cost_tokens": int(suno_cost_tokens),
                        },
                    )
                    suno_charged = True

            try:
                await _enqueue_music_job(
                    chat_id=int(chat_id),
                    user_id=int(user_id),
                    settings=settings,
                    charge_tokens=(suno_cost_tokens if suno_charged else 0),
                )
            except Exception as e:
                try:
                    if suno_charged:
                        try:
                            add_tokens(user_id, suno_cost_tokens, reason="suno_music_refund")
                        except TypeError:
                            add_tokens(user_id, int(suno_cost_tokens), reason="suno_music_refund")
                except Exception:
                    pass
                try:
                    sb_set_user_state(user_id, "music_wait_text", settings)
                except Exception:
                    pass
                await tg_send_message(chat_id, f"❌ Не удалось поставить музыку в очередь: {e}", reply_markup=_main_menu_for(user_id))
                return {"ok": True}

            _clear_music_ctx(st, chat_id, user_id)
            await tg_send_message(
                chat_id,
                "⏳ Музыка: поставил в очередь. Как будет готово — пришлю трек.",
                reply_markup=_help_menu_for(user_id),
            )
            return {"ok": True}

        # ----- WebApp data (Google Omni Flash settings) -----
        is_omni_flash = (
            (str(payload.get("type") or "").lower().strip() in {"omni_flash_settings", "google_omni_flash_settings", "gemini_omni_settings"})
            or (str(payload.get("provider") or provider_raw or "").lower().strip() in {"google", "google_omni", "omni_flash"})
            or (str(payload.get("model") or "").lower().strip() == "gemini-omni-video")
        )

        if is_omni_flash:
            flow = str(payload.get("flow") or payload.get("mode") or "text").lower().strip()
            if flow in ("image", "i2v", "image_to_video", "image2video", "image->video"):
                flow = "image"
            elif flow in ("video", "video_edit", "video_to_video", "v2v", "video2video", "video->video", "edit_video"):
                flow = "video_edit"
            else:
                flow = "text"

            duration = normalize_gemini_omni_duration(payload.get("duration") or 8)
            resolution = normalize_gemini_omni_resolution(payload.get("resolution") or "1080p")
            aspect_ratio = normalize_gemini_omni_aspect_ratio(payload.get("aspect_ratio") or "16:9")

            st["omni_flash_settings"] = {
                "provider": "google",
                "model": "gemini-omni-video",
                "flow": flow,
                "duration": duration,
                "resolution": resolution,
                "aspect_ratio": aspect_ratio,
                "max_images": 5 if flow == "video_edit" else 7,
            }
            st["ts"] = _now()

            if flow == "text":
                _set_mode(chat_id, user_id, "omni_flash_t2v")
                st["omni_flash_t2v"] = {"step": "need_prompt"}
                await tg_send_message(
                    chat_id,
                    f"✅ Настройки Google Omni Flash сохранены: Text → Video • {duration} сек • {resolution} • {aspect_ratio}\n\nТеперь пришли промпт одним сообщением.",
                    reply_markup=_help_menu_for(user_id),
                )
            elif flow == "video_edit":
                _set_mode(chat_id, user_id, "omni_flash_video_edit")
                st["omni_flash_video_edit"] = {"step": "need_video", "image_urls": []}
                await tg_send_message(
                    chat_id,
                    f"✅ Настройки Google Omni Flash сохранены: Video Edit • {resolution} • {aspect_ratio}\n\nТеперь пришли исходное видео MP4/MOV. После видео можно добавить до 5 фото-референсов или сразу прислать промпт.",
                    reply_markup=_help_menu_for(user_id),
                )
            else:
                _set_mode(chat_id, user_id, "omni_flash_i2v")
                st["omni_flash_i2v"] = {"step": "need_images", "images": []}
                await tg_send_message(
                    chat_id,
                    f"✅ Настройки Google Omni Flash сохранены: Image → Video • {duration} сек • {resolution} • {aspect_ratio}\n\nТеперь пришли 1–7 фото. Когда все фото загрузишь — напиши «Готово», потом пришлёшь промпт.",
                    reply_markup=_help_menu_for(user_id),
                )
            return {"ok": True}

        # ----- WebApp data (Grok settings) -----
        is_grok = (
            (str(payload.get("type") or "").lower().strip() == "grok_settings")
            or (str(payload.get("provider") or provider_raw or "").lower().strip() == "grok")
        )

        if is_grok:
            model = normalize_grok_model(payload.get("model") or GROK_LEGACY_MODEL)
            flow = str(payload.get("flow") or payload.get("mode") or "text").lower().strip()
            if flow in ("image", "i2v", "image_to_video", "image2video", "image->video"):
                flow = "image"
            else:
                flow = "text"

            if is_grok15_model(model):
                flow = "image"
                duration = normalize_grok15_duration(payload.get("duration") or 5)
                resolution = normalize_grok15_resolution(payload.get("resolution") or "480p")
                aspect_ratio = normalize_grok15_aspect_ratio(payload.get("aspect_ratio") or "16:9")
                provider_mode = ""
                display_name = "Grok 1.5 Preview"
            else:
                duration = normalize_grok_duration(payload.get("duration") or 6)
                resolution = normalize_grok_resolution(payload.get("resolution") or "480p")
                aspect_ratio = normalize_grok_aspect_ratio(payload.get("aspect_ratio") or "16:9")
                provider_mode = normalize_grok_provider_mode(payload.get("provider_mode") or "normal")
                display_name = "Grok"

            st["grok_settings"] = {
                "provider": "grok",
                "model": model,
                "flow": flow,
                "duration": duration,
                "resolution": resolution,
                "aspect_ratio": aspect_ratio,
                "provider_mode": provider_mode,
            }
            st["ts"] = _now()

            if flow == "text":
                _set_mode(chat_id, user_id, "grok_t2v")
                st["grok_t2v"] = {"step": "need_prompt"}
                await tg_send_message(
                    chat_id,
                    f"✅ Настройки {display_name} сохранены: Text → Video • {duration} сек • {resolution} • {aspect_ratio} • Mode: {provider_mode}\n\nТеперь пришли промпт одним сообщением.",
                    reply_markup=_help_menu_for(user_id),
                )
            else:
                _set_mode(chat_id, user_id, "grok_i2v")
                st["grok_i2v"] = {"step": "need_image", "image_bytes": None, "image_name": None}
                mode_suffix = f" • Mode: {provider_mode}" if provider_mode else ""
                await tg_send_message(
                    chat_id,
                    f"✅ Настройки {display_name} сохранены: Image → Video • {duration} сек • {resolution} • {aspect_ratio}{mode_suffix}\n\nШаг 1) Пришли стартовое фото.\nШаг 2) Потом пришли текстом, что должно происходить в видео.",
                    reply_markup=_help_menu_for(user_id),
                )
            return {"ok": True}
            
        # ----- Kling 3.0 - New (KIE) -----
        if str(payload.get("type") or "").lower().strip() == "kling3_kie_settings":
            kie_mode = str(payload.get("mode") or payload.get("kie_mode") or payload.get("resolution") or "std").strip()
            if kie_mode.lower() in ("standard", "720", "720p"):
                kie_mode = "std"
            elif kie_mode.lower() in ("1080", "1080p"):
                kie_mode = "pro"
            elif kie_mode.lower() == "4k":
                kie_mode = "4K"
            elif kie_mode not in ("std", "pro", "4K"):
                kie_mode = "std"

            enable_audio = bool(payload.get("enable_audio", payload.get("sound", False)))
            try:
                duration = int(payload.get("duration") or 5)
            except Exception:
                duration = 5
            duration = max(3, min(15, duration))

            aspect_ratio = str(payload.get("aspect_ratio") or "16:9").strip()
            if aspect_ratio not in ("16:9", "9:16", "1:1"):
                aspect_ratio = "16:9"

            gen_mode = str(payload.get("gen_mode") or payload.get("mode_type") or "text_to_video").strip()
            mode_aliases = {
                "t2v": "text_to_video",
                "text": "text_to_video",
                "text_to_video": "text_to_video",
                "i2v": "image_to_video",
                "image": "image_to_video",
                "image_to_video": "image_to_video",
                "multishot": "multi_shot",
                "multi-shot": "multi_shot",
                "multi_shot": "multi_shot",
            }
            gen_mode = mode_aliases.get(gen_mode, "text_to_video")

            multi_shots = payload.get("multi_shots") or []
            if not isinstance(multi_shots, list):
                multi_shots = []
            clean_shots = []
            for item in multi_shots[:5]:
                if not isinstance(item, dict):
                    continue
                ptxt = str(item.get("prompt") or "").strip()
                if not ptxt:
                    continue
                try:
                    d = int(item.get("duration") or 3)
                except Exception:
                    d = 3
                clean_shots.append({"prompt": ptxt[:500], "duration": max(1, min(12, d))})

            elements = payload.get("kling_elements") or []
            if not isinstance(elements, list):
                elements = []

            prev = st.get("kling3_kie_settings") or {}
            st["kling3_kie_settings"] = {
                "kie_mode": kie_mode,
                "resolution": kie_mode,
                "enable_audio": enable_audio,
                "duration": duration,
                "aspect_ratio": aspect_ratio,
                "gen_mode": gen_mode,
                "multi_shots": clean_shots,
                "kling_elements": elements,
                "start_image_bytes": prev.get("start_image_bytes"),
                "end_image_bytes": prev.get("end_image_bytes"),
            }
            st["ts"] = _now()
            _set_mode(chat_id, user_id, "kling3_kie_wait_multishot_action" if gen_mode == "multi_shot" else "kling3_kie_wait_prompt")

            if gen_mode == "image_to_video":
                next_block = "Дальше:\n• Пришли стартовое фото\n• Можно прислать второй кадр как финальный\n• Затем пришли prompt"
            elif gen_mode == "multi_shot":
                next_block = "Дальше:\n• Основной prompt не нужен\n• Можно прислать общий стартовый кадр (необязательно)\n• Когда всё готово — отправь сообщением: СТАРТ\n• Элементы уже используются внутри shot prompt через @name"
            else:
                next_block = "Дальше:\n• Пришли текстовый prompt"

                next_block = "Дальше:\n• Пришли текстовый prompt"

            await tg_send_message(
                chat_id,
                "✅ Kling 3.0 - New настройки сохранены.\n"
                f"Режим: {gen_mode}\n"
                f"Качество: {kie_mode} • {duration} сек • {'Audio ON' if enable_audio else 'Audio OFF'}\n"
                f"Формат: {aspect_ratio}\n"
                f"Шотов: {len(clean_shots)} • Elements: {len(elements)}\n\n"
                f"{next_block}",
                reply_markup=_help_menu_for(user_id),
            )
            return {"ok": True}

        # ----- Kling 3.0 Turbo (KIE) -----
        if str(payload.get("type") or "").lower().strip() == "kling3_turbo_settings":
            gen_mode = normalize_kling3_turbo_mode(payload.get("gen_mode") or payload.get("mode_type") or payload.get("flow") or "text_to_video")
            resolution = normalize_kling3_turbo_resolution(payload.get("resolution") or "720p")
            duration = normalize_kling3_turbo_duration(payload.get("duration") or 5)
            aspect_ratio = normalize_kling3_turbo_aspect_ratio(payload.get("aspect_ratio") or "16:9")
            tokens = int(calculate_kling3_turbo_price(resolution, duration))

            prev = st.get("kling3_turbo_settings") or {}
            st["kling3_turbo_settings"] = {
                "gen_mode": gen_mode,
                "resolution": resolution,
                "duration": duration,
                "aspect_ratio": aspect_ratio,
                "start_image_bytes": prev.get("start_image_bytes"),
                "start_image_url": prev.get("start_image_url"),
            }
            st["ts"] = _now()
            _set_mode(chat_id, user_id, "kling3_turbo_wait_prompt")

            if gen_mode == "image_to_video":
                next_block = "Дальше:\n• Пришли стартовое фото\n• Затем пришли текстовый prompt"
                mode_label = "Image → Video"
            else:
                next_block = "Дальше:\n• Пришли текстовый prompt"
                mode_label = "Text → Video"

            await tg_send_message(
                chat_id,
                "✅ Kling 3.0 Turbo настройки сохранены.\n"
                f"Режим: {mode_label}\n"
                f"Качество: {resolution} • {duration} сек\n"
                f"Формат: {aspect_ratio if gen_mode == 'text_to_video' else 'по стартовому кадру'}\n"
                f"Цена: {tokens} ток.\n\n"
                f"{next_block}",
                reply_markup=_help_menu_for(user_id),
            )
            return {"ok": True}

        # ----- Legacy Kling PRO 3.0 / PiAPI disabled -----
        if str(payload.get("type") or "").lower().strip() == "kling3_settings":
            st.pop("kling3_settings", None)
            if st.get("mode") == "kling3_wait_prompt":
                _set_mode(chat_id, user_id, "chat")
            await tg_send_message(
                chat_id,
                "⚠️ Старый Kling/PiAPI Kling 3.0 отключён. Используй Kling 3.0 - New.",
                reply_markup=_help_menu_for(user_id),
            )
            return {"ok": True}

        # из WebApp может прилетать Kling 1.6 / 2.5 / 3.0
        kling_version = (payload.get("kling_version") or "1_6").lower().strip()
        flow = (payload.get("flow") or payload.get("gen_type") or payload.get("genType") or "").lower().strip()
        quality = (payload.get("mode") or payload.get("quality") or "std").lower().strip()

        if kling_version == "2_5":
            if flow in ("i2v", "image_to_video", "image2video", "image->video"):
                flow = "i2v"
            elif flow in ("t2v", "text", "text_to_video", "text2video", "text->video"):
                flow = "t2v"
            else:
                flow = "t2v"

            try:
                duration = int(payload.get("duration") or payload.get("seconds") or payload.get("sec") or 5)
            except Exception:
                duration = 5
            if duration not in (5, 10):
                duration = 5

            aspect_ratio = str(payload.get("aspect_ratio") or "16:9").strip()
            if aspect_ratio not in ("16:9", "9:16", "1:1"):
                aspect_ratio = "16:9"

            st["kling_settings"] = {
                "kling_version": "2_5",
                "flow": flow,
                "quality": "turbo_pro",
                "duration": duration,
                "aspect_ratio": aspect_ratio,
                "model_slug": str(payload.get("model_slug") or "kwaivgi/kling-v2.5-turbo-pro").strip(),
                "product": "kling_2_5_turbo_pro",
            }
            st["ts"] = _now()

            if flow == "t2v":
                _set_mode(chat_id, user_id, "kling_t2v")
                st["kling_t2v"] = {"step": "need_prompt", "duration": duration, "aspect_ratio": aspect_ratio}
                await tg_send_message(
                    chat_id,
                    f"✅ Настройки сохранены: Kling 2.5 Turbo Pro • Text → Video • {duration} сек • {aspect_ratio}\n\n"
                    "Теперь просто напиши промпт одним сообщением.\n"
                    "Пример: «Кинематографичный пролёт камеры над неоновым городом ночью, дождь, реализм».\n"
                    "Можно просто: Старт",
                    reply_markup=_help_menu_for(user_id),
                )
            else:
                _set_mode(chat_id, user_id, "kling_i2v")
                st["kling_i2v"] = {"step": "need_image", "image_bytes": None, "duration": duration}
                await tg_send_message(
                    chat_id,
                    f"✅ Настройки сохранены: Kling 2.5 Turbo Pro • Image → Video • {duration} сек\n\n"
                    "Шаг 1) Пришли СТАРТОВОЕ ФОТО.\n"
                    "Шаг 2) Потом текстом опиши, что должно происходить (или просто: Старт).",
                    reply_markup=_help_menu_for(user_id),
                )
            return {"ok": True}

        # Motion Control container. Старый Kling 1.6 больше не показываем в UI:
        # flow=motion -> Kling Motion Control 2.6 через Replicate;
        # flow=i2v    -> Kling 3.0 Motion Control через KIE.
        motion_version = str(payload.get("motion_version") or "").lower().strip()
        if flow in ("motion", "motion_control", "mc", "2_6", "2.6"):
            flow = "motion"
        elif flow in ("i2v", "image_to_video", "image2video", "image->video", "motion_3_0", "motion3", "3_0", "3.0", "kling3_motion") or motion_version in ("3_0", "3.0"):
            flow = "motion_3_0"
        else:
            flow = "motion"

        quality = "pro" if quality in ("pro", "professional", "1080", "1080p") else "std"
        resolution_label = "1080p" if quality == "pro" else "720p"
        motion_label = "Kling 3.0 Motion Control" if flow == "motion_3_0" else "Kling 2.6 Motion Control"

        st["kling_settings"] = {
            "kling_version": "motion_control",
            "flow": flow,
            "quality": quality,
            "motion_version": "3_0" if flow == "motion_3_0" else "2_6",
            "resolution": resolution_label if flow == "motion_3_0" else quality,
        }
        st["ts"] = _now()

        _set_mode(chat_id, user_id, "kling_mc")
        st["kling_mc"] = {"step": "need_avatar", "avatar_bytes": None, "video_bytes": None, "video_duration": None}

        price_line = "720p = 2 ток/сек, 1080p = 3 ток/сек" if flow == "motion_3_0" else "Standard = 1 ток/сек, Pro = 2 ток/сек"
        await tg_send_message(
            chat_id,
            f"✅ Настройки сохранены: {motion_label} • {resolution_label if flow == 'motion_3_0' else quality.upper()}\n"
            f"Цена: {price_line}\n\n"
            "Шаг 1) Пришли ФОТО аватара (кого анимируем).\n"
            "Шаг 2) Потом пришли ВИДЕО с движением (3–30 сек).\n"
            "Шаг 3) Потом текстом напиши, что должно происходить (или просто: Старт).",
            reply_markup=_help_menu_for(user_id),
        )

        return {"ok": True}
        
    # ----- Admin-only Stars topup -----
    if incoming_text == ADMIN_STARS_200_BUTTON_TEXT:
        if not _is_admin(user_id):
            await tg_send_message(chat_id, "Нет доступа.", reply_markup=_main_menu_for(user_id))
            return {"ok": True}

        tokens = int(ADMIN_STARS_200_TOKENS)
        stars = int(ADMIN_STARS_200_AMOUNT)
        title = f"Админ-пополнение: {tokens} токенов"
        description = f"Только для администратора • {tokens} токенов • {stars}⭐"
        payload = _admin_stars_200_payload(user_id)

        try:
            await tg_send_stars_invoice(chat_id, title, description, payload, stars)
        except Exception as e:
            await tg_send_message(
                chat_id,
                f"❌ Не смог создать Stars-инвойс: {e}",
                reply_markup=_main_menu_for(user_id),
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
        
    # ----- Admin broadcast (start) -----
    if incoming_text == "📣 Рассылка":
        if not _is_admin(user_id):
            await tg_send_message(chat_id, "Нет доступа.", reply_markup=_main_menu_for(user_id))
            return {"ok": True}

        st = _ensure_state(chat_id, user_id)
        st["mode"] = "admin_broadcast_wait_text"
        st["ts"] = _now()

        await tg_send_message(
            chat_id,
            "📣 Рассылка\n\nПришли одним сообщением текст для рассылки.\n\nЧтобы отменить — напиши: отмена",
            reply_markup=_main_menu_for(user_id),
        )
        return {"ok": True}

    # ----- Pro menu -----
    if incoming_text == "Для Pro":
        if not _is_admin(user_id):
            await tg_send_message(
                chat_id,
                "Нет доступа.",
                reply_markup=_main_menu_for(user_id),
            )
            return {"ok": True}

        await tg_send_message(
            chat_id,
            "Раздел Pro. Открывай Top Analizator: В РАЗРАБОТКЕ",
            reply_markup=_pro_menu_for(user_id),
        )
        return {"ok": True}

    if incoming_text == "⬅️ Назад":
        submenu = str(st.get("photo_submenu") or "").strip().lower()
        if submenu in ("seedream", "upscale", "gpt_image_2", "gpt_image_2_kie"):
            st.pop("photo_submenu", None)
            st["ts"] = _now()
            await tg_send_message(chat_id, "📸 Фото будущего — выбери режим:", reply_markup=_photo_future_menu_keyboard())
            return {"ok": True}
        await tg_send_message(chat_id, "Главное меню.", reply_markup=_main_menu_for(user_id))
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
        partner_ref_code = _extract_partner_ref_from_start(incoming_text)
        if partner_ref_code:
            await _enqueue_partner_bind_referral(
                user_id,
                partner_ref_code,
                source="telegram_start",
                meta={"chat_id": chat_id, "text": incoming_text[:200]},
            )
        # 🎁 Welcome bonus: 3 tokens only once (after first /start)
        try:
            granted = grant_welcome_bonus_once(user_id)
        except Exception:
            granted = False

        await tg_send_message(
            chat_id,
            "Привет!\n"
            "Режимы:\n"
            "• «ИИ (чат)» — вопросы/анализ фото/решение задач.\n"
            "• «Фото будущего» — фото-режимы (GPT Image 2.0 / Нейро фотосессии / Seedream / Nano Banana).\n",
            reply_markup=_help_menu_for(user_id),
        )
        return {"ok": True}



    if incoming_text in ("⬅ Назад", "Назад"):
        # Возврат в главное меню из любого режима
        _set_mode(chat_id, user_id, "chat")
        await tg_send_message(chat_id, "Главное меню.", reply_markup=_main_menu_for(user_id))
        return {"ok": True}

    # ---- Admin broadcast: waiting for text ----
    if st.get("mode") == "admin_broadcast_wait_text":
        # защита: только админ может выполнять рассылку
        if not _is_admin(user_id):
            st["mode"] = ""
            st["ts"] = _now()
            await tg_send_message(chat_id, "Нет доступа.", reply_markup=_main_menu_for(user_id))
            return {"ok": True}
        # отмена
        if (incoming_text or "").strip().lower() in ("отмена", "/cancel"):
            st["mode"] = ""
            st["ts"] = _now()
            await tg_send_message(chat_id, "✅ Рассылка отменена.", reply_markup=_main_menu_for(user_id))
            return {"ok": True}

        text_to_send = (incoming_text or "").strip()
        if not text_to_send:
            await tg_send_message(chat_id, "Пришли текст одним сообщением (или напиши «отмена»).", reply_markup=_main_menu_for(user_id))
            return {"ok": True}

        # выходим из режима, чтобы повторно не сработало
        st["mode"] = ""
        st["ts"] = _now()

        await tg_send_message(chat_id, "⏳ Начал рассылку...", reply_markup=_main_menu_for(user_id))

        async def _run_broadcast():
            ok, fail = await _admin_broadcast_send(chat_id, text_to_send)
            await tg_send_message(chat_id, f"📣 Рассылка завершена.\n✅ Отправлено: {ok}\n❌ Ошибок: {fail}", reply_markup=_main_menu_for(user_id))

        asyncio.create_task(_run_broadcast())
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

        ai_choice = _normalize_music_ai_choice(settings)

        # ---- BILLING: Suno fixed price ----
        suno_cost_tokens = 2
        suno_charged = False
        try:
            ensure_user_row(user_id)
            bal = int(get_balance(user_id) or 0)
        except Exception:
            bal = 0

        if bal < suno_cost_tokens:
            try:
                sb_set_user_state(user_id, "music_wait_text", settings)
            except Exception:
                pass

            await tg_send_message(
                chat_id,
                f"❌ Недостаточно токенов для Suno\nНужно: {suno_cost_tokens}\nБаланс: {bal}",
                reply_markup=_topup_balance_inline_kb(),
            )
            return {"ok": True}

        try:
            add_tokens(
                user_id,
                -suno_cost_tokens,
                reason="suno_music",
                meta={
                    "ai": ai_choice,
                    "provider": str(settings.get("provider") or ""),
                    "cost_tokens": suno_cost_tokens,
                },
            )
            suno_charged = True
        except TypeError:
            add_tokens(
                user_id,
                -int(suno_cost_tokens),
                reason="suno_music",
                meta={
                    "ai": ai_choice,
                    "provider": str(settings.get("provider") or ""),
                    "cost_tokens": int(suno_cost_tokens),
                },
            )
            suno_charged = True

        try:
            await _enqueue_music_job(
                chat_id=int(chat_id),
                user_id=int(user_id),
                settings=settings,
                charge_tokens=(suno_cost_tokens if suno_charged else 0),
            )
        except Exception as e:
            try:
                if suno_charged:
                    try:
                        add_tokens(user_id, suno_cost_tokens, reason="suno_music_refund")
                    except TypeError:
                        add_tokens(user_id, int(suno_cost_tokens), reason="suno_music_refund")
            except Exception:
                pass
            try:
                sb_set_user_state(user_id, "music_wait_text", settings)
            except Exception:
                pass
            await tg_send_message(chat_id, f"❌ Не удалось поставить музыку в очередь: {e}", reply_markup=_main_menu_for(user_id))
            return {"ok": True}

        _clear_music_ctx(st, chat_id, user_id)
        await tg_send_message(
            chat_id,
            "⏳ Музыка: поставил в очередь. Как будет готово — пришлю трек.",
            reply_markup=_main_menu_for(user_id),
        )
        return {"ok": True}
    if incoming_text in ("💰 Баланс", "Баланс", "💰Баланс"):
        try:
            ensure_user_row(user_id)
            bal = int(get_balance(user_id) or 0)
        except Exception as e:
            await tg_send_message(chat_id, f"Не смог получить баланс: {e}", reply_markup=_main_menu_for(user_id))
            return {"ok": True}

        caption = f"💰 Баланс: {bal} токенов\n\nРасход токенов зависит от режима генерации (выбирается в WebApp)."
        banner_bytes = _read_balance_banner_bytes()

        if banner_bytes:
            try:
                await tg_send_photo_bytes(
                    chat_id,
                    banner_bytes,
                    caption=caption,
                    reply_markup=_topup_balance_inline_kb(),
                )
            except Exception:
                await tg_send_message(
                    chat_id,
                    caption,
                    reply_markup=_topup_balance_inline_kb(),
                )
        else:
            await tg_send_message(
                chat_id,
                caption,
                reply_markup=_topup_balance_inline_kb(),
            )
        return {"ok": True}

    if incoming_text in ("🔊 Озвучить текст", "Озвучить текст"):
        await tg_send_message(
            chat_id,
            "🔊 Озвучка текста\n\nВыбери группу голосов:",
            reply_markup=_tts_gender_keyboard(),
        )
        return {"ok": True}
    # ---- TTS: choose gender ----
    if incoming_text == "👨 Мужские голоса":
        st["mode"] = "tts_choose_voice"
        st["tts"] = {"gender": "male"}
        st["ts"] = _now()
        await tg_send_message(
            chat_id,
            "Выбери мужской голос:",
            reply_markup=_tts_voices_keyboard("male"),
        )
        return {"ok": True}

    if incoming_text == "👩 Женские голоса":
        st["mode"] = "tts_choose_voice"
        st["tts"] = {"gender": "female"}
        st["ts"] = _now()
        await tg_send_message(
            chat_id,
            "Выбери женский голос:",
            reply_markup=_tts_voices_keyboard("female"),
        )
        return {"ok": True}

    # ---- TTS: choose concrete voice ----
    if incoming_text in _TTS_BY_BTN:
        v = _TTS_BY_BTN[incoming_text]
        st["mode"] = "tts_wait_text"
        st["tts"] = {"voice_id": v["voice_id"], "name": v["name"]}
        st["ts"] = _now()
        await tg_send_message(
            chat_id,
            f"✅ Голос выбран: {v['name']}\n\nТеперь пришли текст — я озвучу его.",
            reply_markup=_help_menu_for(user_id),
        )
        return {"ok": True}
        
    if incoming_text in ("Фото будущего", "📸 Фото будущего"):
        # Подменю: объединённая точка входа в фото-режимы
        st.pop("photo_submenu", None)
        st["ts"] = _now()
        await tg_send_message(
            chat_id,
            "📸 Фото будущего — выбери режим:",
            reply_markup=_photo_future_menu_keyboard(),
        )
        return {"ok": True}

    if incoming_text == "Gpt Image 2":
        st["photo_submenu"] = "gpt_image_2_kie"
        st["ts"] = _now()
        await tg_send_message(
            chat_id,
            "✨ Gpt Image 2 — цена: 2K = 1 токен, 4K = 2 токена\n• Текст→Картинка\n• Картинка→Картинка",
            reply_markup=_photo_gpt_image_2_kie_menu_keyboard(),
        )
        return {"ok": True}

    if incoming_text in ("GPT Image 2.0", "Фото/Афиши"):
        # Legacy cached buttons now open the KIE-based GPT Image 2 flow.
        st["photo_submenu"] = "gpt_image_2_kie"
        st["ts"] = _now()
        await tg_send_message(
            chat_id,
            "✨ Gpt Image 2 — цена: 2K = 1 токен, 4K = 2 токена\n• Текст→Картинка\n• Картинка→Картинка",
            reply_markup=_photo_gpt_image_2_kie_menu_keyboard(),
        )
        return {"ok": True}

    if incoming_text == "Seedream":
        st["photo_submenu"] = "seedream"
        st["ts"] = _now()
        await tg_send_message(
            chat_id,
            "✨ Seedream — выбери режим:\n• Seedream 4.5 = 1 фото + чистый промпт\n• Текст→Картинка\n• Картинка+Картинка",
            reply_markup=_photo_seedream_menu_keyboard(),
        )
        return {"ok": True}

    if incoming_text in ("Midjourney", "Миджорни"):
        _set_mode(chat_id, user_id, "midjourney")
        await tg_send_message(
            chat_id,
            "Midjourney — генерация 4 изображений за один запуск.\n\nВыбери модель:",
            reply_markup=_midjourney_model_kb(),
        )
        return {"ok": True}

    if incoming_text == "Апскейл":
        st["photo_submenu"] = "upscale"
        st["ts"] = _now()
        await tg_send_message(
            chat_id,
            "🖼 Апскейл — выбери режим:",
            reply_markup=_photo_upscale_menu_keyboard(),
        )
        return {"ok": True}

    if incoming_text in ("ИИ (чат)", "🧠 ИИ (чат)", "🧠 ИИ чат"):
        _set_mode(chat_id, user_id, "chat")
        st["ai_chat_mode"] = "menu"
        st["ai_prompt"] = _new_ai_prompt_state()
        st["ts"] = _now()
        await tg_send_message(
            chat_id,
            "🤖 ИИ помощник включён.\n\nВыбери модель чата или генератор промтов.",
            reply_markup=_ai_chat_mode_inline_kb(),
        )
        return {"ok": True}


    if st.get("mode") == "chat" and st.get("ai_chat_mode") == "menu" and incoming_text and not _is_nav_or_menu_text(incoming_text):
        await tg_send_message(
            chat_id,
            "Сначала выбери модель чата: Claude Sonnet, Claude Opus 4.7, Claude Fable 5 или ChatGPT. Либо выбери 🪄 Промт.",
            reply_markup=_ai_chat_mode_inline_kb(),
        )
        return {"ok": True}


    if st.get("mode") == "chat" and st.get("ai_chat_mode") == "prompt" and incoming_text and not _is_nav_or_menu_text(incoming_text):
        pb = st.get("ai_prompt") or _new_ai_prompt_state()
        step = str(pb.get("step") or "choose_root")
        if step == "choose_root":
            await tg_send_message(
                chat_id,
                "Сначала выбери раздел для промта ниже.",
                reply_markup=_ai_prompt_root_inline_kb(),
            )
            return {"ok": True}
        if step == "choose_video_target":
            await tg_send_message(
                chat_id,
                "Сначала выбери видеомодель ниже, а потом присылай идею или фото.",
                reply_markup=_ai_prompt_video_inline_kb(),
            )
            return {"ok": True}

        if not await _tg_consume_free_chat_or_notify(chat_id, user_id):
            st["ts"] = _now()
            return {"ok": True}

        result = await _ai_prompt_generate(pb, incoming_text)
        pb["last_prompt"] = result
        st["ai_prompt"] = pb
        st["ts"] = _now()
        await tg_send_message(chat_id, result, reply_markup=_ai_prompt_tools_inline_kb(pb))
        return {"ok": True}

    if incoming_text == "Нейро фотосессии":
        _set_mode(chat_id, user_id, "photosession")
        await tg_send_message(
            chat_id,
            "Режим «Нейро фотосессии» 💎 ЦЕНА 1 ТОКЕН.\n"
            "1) Пришли фото.\n"
            "2) Потом одним сообщением напиши задачу: локация/стиль/одежда/детали.\n"
            "Я постараюсь сохранить человека максимально 1к1 и сделать фото как профессиональную фотосессию.",
            reply_markup=_help_menu_for(user_id),
        )
        return {"ok": True}
    handled = False

    if incoming_text:

        handled = await handle_kling3_turbo_wait_prompt(
            chat_id=chat_id,
            user_id=user_id,
            incoming_text=incoming_text,
            st=st,
            deps={
                "tg_send_message": tg_send_message,
                "_main_menu_for": _main_menu_for,
                "_is_nav_or_menu_text": _is_nav_or_menu_text,
                "_set_mode": _set_mode,
                "_now": _now,
                "sb_clear_user_state": sb_clear_user_state,
                "queue_name": WORKSPACE_MEDIA_QUEUE_NAME,
            },
        )
        if handled:
            return {"ok": True}

        handled = await handle_kling3_kie_wait_prompt(
            chat_id=chat_id,
            user_id=user_id,
            incoming_text=incoming_text,
            st=st,
            deps={
                "tg_send_message": tg_send_message,
                "_main_menu_for": _main_menu_for,
                "_is_nav_or_menu_text": _is_nav_or_menu_text,
                "_set_mode": _set_mode,
                "_now": _now,
                "sb_clear_user_state": sb_clear_user_state,
                "queue_name": os.getenv("KLING3_KIE_QUEUE_NAME", "kling3_kie"),
            },
        )
        if handled:
            return {"ok": True}

        if st.get("mode") == "kling3_wait_prompt":
            st.pop("kling3_settings", None)
            _set_mode(chat_id, user_id, "chat")
            await tg_send_message(
                chat_id,
                "⚠️ Старый Kling/PiAPI Kling 3.0 отключён. Используй Kling 3.0 - New.",
                reply_markup=_main_menu_for(user_id),
            )
            return {"ok": True}

        handled = await handle_kling3_wait_prompt(
        chat_id=chat_id,
        user_id=user_id,
        incoming_text=incoming_text,
        st=st,
        deps={
            "tg_send_message": tg_send_message,
            "_main_menu_for": _main_menu_for,
            "_is_nav_or_menu_text": _is_nav_or_menu_text,
            "_set_mode": _set_mode,
            "_now": _now,
            "sb_clear_user_state": sb_clear_user_state,
            "poll_interval_sec": 2.0,
            "timeout_sec": 3600,
        },
    )
    if handled:
        return {"ok": True}

    if st.get("mode") == "seedance_extend_prompt" and incoming_text:
        if _is_nav_or_menu_text(incoming_text):
            st.pop("seedance_extend", None)
            st["ts"] = _now()
            _set_mode(chat_id, user_id, "chat")
            await tg_send_message(chat_id, "Ок. Вышел из продолжения Seedance. Главное меню.", reply_markup=_main_menu_for(user_id))
            return {"ok": True}

        se = st.get("seedance_extend") or {}
        extend_from_task_id = str(se.get("extend_from_task_id") or "").strip()
        duration = int(se.get("duration") or 0)
        cost_tokens = int(se.get("cost_tokens") or 0)
        prompt = incoming_text.strip()

        if not extend_from_task_id:
            await tg_send_message(chat_id, "Не найден task_id для продолжения. Запусти продолжение заново.", reply_markup=_main_menu_for(user_id))
            st.pop("seedance_extend", None)
            st["ts"] = _now()
            _set_mode(chat_id, user_id, "chat")
            return {"ok": True}

        if duration not in (5, 10, 15):
            await tg_send_message(chat_id, "Не найдена длительность продолжения. Запусти продолжение заново.", reply_markup=_main_menu_for(user_id))
            st.pop("seedance_extend", None)
            st["ts"] = _now()
            _set_mode(chat_id, user_id, "chat")
            return {"ok": True}

        if not prompt:
            await tg_send_message(chat_id, "Промпт пустой. Пришли текстом, что должно происходить дальше.", reply_markup=_help_menu_for(user_id))
            return {"ok": True}

        _busy_start(int(user_id), "Seedance Extend")
        seedance_charged = False
        try:
            try:
                ensure_user_row(user_id)
                bal = int(get_balance(user_id) or 0)
            except Exception:
                bal = 0

            if bal < cost_tokens:
                await tg_send_message(
                    chat_id,
                    f"❌ Недостаточно токенов.\nНужно: {cost_tokens}\nБаланс: {bal}",
                    reply_markup=_topup_balance_inline_kb(),
                )
                return {"ok": True}

            try:
                add_tokens(
                    user_id,
                    -cost_tokens,
                    reason="seedance_extend",
                    meta={
                        "extend_from_task_id": extend_from_task_id,
                        "duration": int(duration),
                        "cost_tokens": int(cost_tokens),
                    },
                )
            except TypeError:
                add_tokens(
                    user_id,
                    -int(cost_tokens),
                    reason="seedance_extend",
                    meta={
                        "extend_from_task_id": extend_from_task_id,
                        "duration": int(duration),
                        "cost_tokens": int(cost_tokens),
                    },
                )
            seedance_charged = True

            job_id = uuid4().hex
            job = {
                "job_id": job_id,
                "type": "seedance_extend",
                "chat_id": int(chat_id),
                "user_id": int(user_id),
                "extend_from_task_id": extend_from_task_id,
                "prompt": prompt,
                "duration": int(duration),
                "charge_tokens": int(cost_tokens),
            }

            st.pop("seedance_extend", None)
            st["ts"] = _now()
            _set_mode(chat_id, user_id, "chat")

            await tg_send_message(
                chat_id,
                "⏳ Seedance: продолжение поставил в очередь. Как будет готово — пришлю видео.",
                reply_markup=_help_menu_for(user_id),
            )

            await enqueue_job(job, queue_name="gen")
            return {"ok": True}

        except Exception as e:
            try:
                if seedance_charged:
                    try:
                        add_tokens(user_id, int(cost_tokens), reason="seedance_extend_refund", meta={"stage": "main_exception"})
                    except TypeError:
                        add_tokens(user_id, int(cost_tokens), reason="seedance_extend_refund")
            except Exception:
                pass

            await tg_send_message(chat_id, f"❌ Ошибка продолжения Seedance: {e}", reply_markup=_main_menu_for(user_id))
            return {"ok": True}
        finally:
            _busy_end(int(user_id))

    # ---- SEEDANCE 2 Text/Image/Omni → Video: ждём промпт ----
    if st.get("mode") in ("seedance_t2v", "seedance_i2v", "seedance_omni") and incoming_text:
        # Навигация/кнопки меню не считаем промптом
        if _is_nav_or_menu_text(incoming_text):
            _set_mode(chat_id, user_id, "chat")
            st.pop("seedance_t2v", None)
            st.pop("seedance_i2v", None)
            st.pop("seedance_omni", None)
            st.pop("seedance_settings", None)
            st["ts"] = _now()
            sb_clear_user_state(user_id)
            await tg_send_message(chat_id, "Ок. Вышел из Seedance. Главное меню.", reply_markup=_main_menu_for(user_id))
            return {"ok": True}

        # Защита от двойного запуска
        if _busy_is_active(int(user_id)):
            kind = _busy_kind(int(user_id)) or "генерация"
            await tg_send_message(
                chat_id,
                f"⏳ Сейчас выполняется: {kind}. Дождись завершения (или /reset).",
                reply_markup=_help_menu_for(user_id),
            )
            return {"ok": True}

        settings = st.get("seedance_settings") or {}
        provider_kind = str(settings.get("provider_kind") or "seedance").strip() or "seedance"
        seedance_model = str(settings.get("seedance_model") or ("seedance-kie-480p" if provider_kind == "seedance_kie" else "preview")).strip()
        task_type = str(settings.get("task_type") or ("seedance-2-fast" if seedance_model == "seedance-kie-480p" else ("seedance-2" if provider_kind == "seedance_kie" else "seedance-2-preview"))).strip()
        if seedance_model.lower() in {"seedance-kie-mini", "seedance-2-mini", "seedance-mini", "mini"} or task_type.lower() == "seedance-2-mini":
            seedance_model = "seedance-kie-mini"
            task_type = "seedance-2-mini"
        duration = int(settings.get("duration") or 5)
        aspect_ratio = str(settings.get("aspect_ratio") or "16:9").strip()
        max_images = int(settings.get("max_images") or (7 if _seedance_uses_kie_backend(settings) else 2))
        max_videos = int(settings.get("max_videos") or 0)
        max_audios = int(settings.get("max_audios") or 0)
        max_total_refs = int(settings.get("max_total_refs") or max_images)

        # Если это i2v, но мы ещё собираем фото — обрабатываем «Готово»
        if st.get("mode") == "seedance_i2v":
            si = st.get("seedance_i2v") or {}
            step = (si.get("step") or "need_images")
            if step == "need_images":
                if incoming_text.lower() in ("готово", "готов", "done", "ok", "ок"):
                    imgs = si.get("image_file_ids") or []
                    if not imgs:
                        await tg_send_message(chat_id, "Сначала пришли хотя бы 1 фото.", reply_markup=_seedance_refs_collect_kb())
                        return {"ok": True}
                    si["step"] = "need_prompt"
                    st["seedance_i2v"] = si
                    st["ts"] = _now()
                    await tg_send_message(
                        chat_id,
                        "Фото принял ✅ Пришли промпт одним или несколькими сообщениями. Когда всё отправишь — нажми «✅ Запустить».",
                        reply_markup=_seedance_prompt_collect_kb("seedance_i2v"),
                    )
                    return {"ok": True}

                await tg_send_message(
                    chat_id,
                    f"Я сейчас жду фото (1–{max_images}).\nОтправь фото или нажми «✅ Готово», когда закончил.",
                    reply_markup=_seedance_refs_collect_kb(),
                )
                return {"ok": True}

        # Если это omni, но мы ещё собираем refs — обрабатываем «Готово»
        if st.get("mode") == "seedance_omni":
            so = st.get("seedance_omni") or {}
            step = (so.get("step") or "collect_refs")
            if step == "collect_refs":
                if incoming_text.lower() in ("готово", "готов", "done", "ok", "ок"):
                    image_ids = list(so.get("image_file_ids") or [])
                    video_ids = list(so.get("video_file_ids") or [])
                    audio_ids = list(so.get("audio_file_ids") or [])
                    total_refs = len(image_ids) + len(video_ids) + len(audio_ids)
                    if total_refs <= 0:
                        await tg_send_message(chat_id, "Сначала пришли хотя бы один image/video/audio reference.", reply_markup=_seedance_refs_collect_kb())
                        return {"ok": True}
                    if audio_ids and not (image_ids or video_ids):
                        await tg_send_message(chat_id, "Для Omni Reference аудио нельзя отправлять отдельно. Добавь хотя бы фото или видео reference.", reply_markup=_seedance_refs_collect_kb())
                        return {"ok": True}
                    so["step"] = "need_prompt"
                    st["seedance_omni"] = so
                    st["ts"] = _now()
                    await tg_send_message(
                        chat_id,
                        "Референсы принял ✅ Пришли промпт одним или несколькими сообщениями. Когда всё отправишь — нажми «✅ Запустить».",
                        reply_markup=_seedance_prompt_collect_kb("seedance_omni"),
                    )
                    return {"ok": True}

                await tg_send_message(
                    chat_id,
                    f"Я сейчас жду refs: фото до {max_images}, видео до {max_videos}, аудио до {max_audios}, всего до {max_total_refs}.\n"
                    "Отправь файлы или нажми «✅ Готово», когда закончил.",
                    reply_markup=_seedance_refs_collect_kb(),
                )
                return {"ok": True}

        prompt_limit = _seedance_prompt_limit_from_settings(settings)
        prompt_part = incoming_text.strip()
        done_words = {"готово", "готов", "запустить", "старт", "done", "start", "go"}

        if prompt_part.lower() in done_words:
            prompt = _seedance_prompt_text_from_state(st)
            return await _seedance_start_generation_from_prompt(chat_id, user_id, st, prompt)

        ok, err, parts_count, chars_count = _seedance_prompt_append_part(st, prompt_part, prompt_limit)
        if not ok:
            await tg_send_message(
                chat_id,
                err or "Не смог добавить часть промпта. Пришли текст ещё раз.",
                reply_markup=_seedance_prompt_collect_kb(st.get("mode")),
            )
            return {"ok": True}

        await tg_send_message(
            chat_id,
            f"✅ Часть промпта добавлена. Сейчас: {parts_count} част(и), {chars_count}/{prompt_limit} символов.\n"
            "Можешь прислать следующую часть или нажать «✅ Запустить», когда промпт полностью готов.",
            reply_markup=_seedance_prompt_collect_kb(st.get("mode")),
        )
        return {"ok": True}

    # ---- Sora 2 (OpenAI) Text→Video: ждём промпт ----
    if st.get("mode") == "sora_t2v" and incoming_text:
        if _is_nav_or_menu_text(incoming_text):
            _set_mode(chat_id, user_id, "chat")
            st.pop("sora_t2v", None)
            st.pop("sora_settings", None)
            st["ts"] = _now()
            sb_clear_user_state(user_id)
            await tg_send_message(chat_id, "Ок. Вышел из Sora 2. Главное меню.", reply_markup=_main_menu_for(user_id))
            return {"ok": True}

        if _busy_is_active(int(user_id)):
            kind = _busy_kind(int(user_id)) or "генерация"
            await tg_send_message(
                chat_id,
                f"⏳ Сейчас выполняется: {kind}. Дождись завершения (или /reset).",
                reply_markup=_help_menu_for(user_id),
            )
            return {"ok": True}

        prompt = incoming_text.strip()
        if not prompt:
            await tg_send_message(chat_id, "Промпт пустой. Пришли текстом, что должно быть в видео.", reply_markup=_seedance_prompt_back_kb() if st.get("mode") in ("seedance_i2v", "seedance_omni") else _help_menu_for(user_id))
            return {"ok": True}

        settings = st.get("sora_settings") or {}
        duration = int(settings.get("duration") or 4)
        aspect_ratio = str(settings.get("aspect_ratio") or "16:9").strip()
        model_slug = str(settings.get("model") or "sora-2").strip() or "sora-2"

        cost_map = {4: 5, 8: 10, 12: 15}
        cost_tokens = int(cost_map.get(duration, 5))

        _busy_start(int(user_id), "Sora 2 видео")
        sora_charged = False
        try:
            try:
                ensure_user_row(user_id)
                bal = int(get_balance(user_id) or 0)
            except Exception:
                bal = 0

            if bal < cost_tokens:
                await tg_send_message(
                    chat_id,
                    f"❌ Недостаточно токенов.\nНужно: {cost_tokens}\nБаланс: {bal}",
                    reply_markup=_topup_balance_inline_kb(),
                )
                return {"ok": True}

            try:
                add_tokens(
                    user_id,
                    -cost_tokens,
                    reason="sora_video",
                    meta={
                        "model": model_slug,
                        "duration": int(duration),
                        "aspect_ratio": aspect_ratio,
                        "cost_tokens": int(cost_tokens),
                    },
                )
            except TypeError:
                add_tokens(
                    user_id,
                    -int(cost_tokens),
                    reason="sora_video",
                    meta={
                        "model": model_slug,
                        "duration": int(duration),
                        "aspect_ratio": aspect_ratio,
                        "cost_tokens": int(cost_tokens),
                    },
                )
            sora_charged = True

            job_id = uuid4().hex
            job: Dict[str, Any] = {
                "job_id": job_id,
                "type": "sora_video",
                "chat_id": int(chat_id),
                "user_id": int(user_id),
                "model": model_slug,
                "prompt": prompt,
                "duration": int(duration),
                "aspect_ratio": aspect_ratio,
                "charge_tokens": int(cost_tokens),
            }

            st.pop("sora_t2v", None)
            st.pop("sora_settings", None)
            st["ts"] = _now()
            sb_clear_user_state(user_id)
            _set_mode(chat_id, user_id, "chat")

            await tg_send_message(
                chat_id,
                f"⏳ Sora 2: Начинаю генерацию ({duration} сек • {aspect_ratio}). Как будет готово — пришлю видео.",
                reply_markup=_help_menu_for(user_id),
            )

            await enqueue_job(job, queue_name=SORA_QUEUE_NAME)
            return {"ok": True}

        except Exception as e:
            try:
                if sora_charged:
                    try:
                        add_tokens(user_id, int(cost_tokens), reason="sora_video_refund", meta={"stage": "main_exception"})
                    except TypeError:
                        add_tokens(user_id, int(cost_tokens), reason="sora_video_refund")
            except Exception:
                pass
            await tg_send_message(chat_id, f"❌ Ошибка Sora 2: {e}", reply_markup=_main_menu_for(user_id))
            return {"ok": True}
        finally:
            _busy_end(int(user_id))

    # ---- GROK Text→Video / Image→Video: ставим в worker_workspace_media ----
    if st.get("mode") in ("grok_t2v", "grok_i2v") and incoming_text:
        if _is_nav_or_menu_text(incoming_text):
            _set_mode(chat_id, user_id, "chat")
            st.pop("grok_t2v", None)
            st.pop("grok_i2v", None)
            st.pop("grok_settings", None)
            st["ts"] = _now()
            await tg_send_message(chat_id, "Ок. Вышел из Grok. Главное меню.", reply_markup=_main_menu_for(user_id))
            return {"ok": True}

        settings = st.get("grok_settings") or {}
        model = normalize_grok_model(settings.get("model") or GROK_LEGACY_MODEL)
        if is_grok15_model(model):
            duration = normalize_grok15_duration(settings.get("duration") or 5)
            resolution = normalize_grok15_resolution(settings.get("resolution") or "480p")
            aspect_ratio = normalize_grok15_aspect_ratio(settings.get("aspect_ratio") or "16:9")
            provider_mode = ""
            cost_tokens = int(grok15_tokens_for_duration(duration, resolution))
            display_name = "Grok 1.5 Preview"
        else:
            duration = normalize_grok_duration(settings.get("duration") or 6)
            resolution = normalize_grok_resolution(settings.get("resolution") or "480p")
            aspect_ratio = normalize_grok_aspect_ratio(settings.get("aspect_ratio") or "16:9")
            provider_mode = normalize_grok_provider_mode(settings.get("provider_mode") or "normal")
            cost_tokens = int(grok_tokens_for_duration(duration, resolution))
            display_name = "Grok"

        try:
            ensure_user_row(user_id)
            bal = int(get_balance(user_id) or 0)
        except Exception:
            bal = 0

        if bal < cost_tokens:
            await tg_send_message(
                chat_id,
                f"❌ Недостаточно токенов.\nНужно: {cost_tokens}\nБаланс: {bal}",
                reply_markup=_topup_balance_inline_kb(),
            )
            return {"ok": True}

        charge_ref_id = uuid4().hex
        try:
            add_tokens(
                user_id,
                -cost_tokens,
                reason="grok_video",
                ref_id=charge_ref_id,
                meta={
                    "provider": "grok",
                    "model": model,
                    "duration": duration,
                    "resolution": resolution,
                    "aspect_ratio": aspect_ratio,
                    "flow": ("i2v" if st.get("mode") == "grok_i2v" else "t2v"),
                    "pricing": "grok15_fixed_duration_map" if is_grok15_model(model) else "legacy_fixed_duration_map",
                },
            )
        except TypeError:
            add_tokens(user_id, -int(cost_tokens), reason="grok_video")

        try:
            if st.get("mode") == "grok_i2v":
                gi = st.get("grok_i2v") or {}
                if (gi.get("step") or "need_image") != "need_prompt":
                    await tg_send_message(chat_id, "Сначала пришли стартовое фото для Grok Image → Video.", reply_markup=_help_menu_for(user_id))
                    try:
                        add_tokens(user_id, int(cost_tokens), reason="grok_video_refund", ref_id=uuid4().hex, meta={"stage": "need_image"})
                    except TypeError:
                        add_tokens(user_id, int(cost_tokens), reason="grok_video_refund")
                    return {"ok": True}
                image_bytes = gi.get("image_bytes")
                if not image_bytes:
                    await tg_send_message(chat_id, "Не хватает стартового фото. Пришли фото и повтори промпт.", reply_markup=_help_menu_for(user_id))
                    try:
                        add_tokens(user_id, int(cost_tokens), reason="grok_video_refund", ref_id=uuid4().hex, meta={"stage": "missing_image"})
                    except TypeError:
                        add_tokens(user_id, int(cost_tokens), reason="grok_video_refund")
                    return {"ok": True}
                await _enqueue_tg_grok_job(
                    chat_id=int(chat_id),
                    user_id=int(user_id),
                    mode="image_to_video",
                    prompt=incoming_text.strip(),
                    settings=settings,
                    image_bytes=image_bytes,
                    image_name=gi.get("image_name"),
                    charge_tokens=cost_tokens,
                    charge_ref_id=charge_ref_id,
                )
                await tg_send_message(chat_id, f"⏳ {display_name} - Генерация началась: Image → Video • {duration} сек • {resolution} • {aspect_ratio}" + (f" • Mode: {provider_mode}" if provider_mode else ""), reply_markup=_help_menu_for(user_id))
            else:
                await _enqueue_tg_grok_job(
                    chat_id=int(chat_id),
                    user_id=int(user_id),
                    mode="text_to_video",
                    prompt=incoming_text.strip(),
                    settings=settings,
                    charge_tokens=cost_tokens,
                    charge_ref_id=charge_ref_id,
                )
                await tg_send_message(chat_id, f"⏳ {display_name} - Генерация началась: Text → Video • {duration} сек • {resolution} • {aspect_ratio}" + (f" • Mode: {provider_mode}" if provider_mode else ""), reply_markup=_help_menu_for(user_id))
        except Exception as e:
            try:
                add_tokens(user_id, int(cost_tokens), reason="grok_video_refund", ref_id=uuid4().hex, meta={"stage": "enqueue_failed", "error": str(e)[:300]})
            except TypeError:
                add_tokens(user_id, int(cost_tokens), reason="grok_video_refund")
            await tg_send_message(chat_id, f"❌ Не удалось поставить Grok в очередь: {e}", reply_markup=_main_menu_for(user_id))
            return {"ok": True}

        _set_mode(chat_id, user_id, "chat")
        st.pop("grok_t2v", None)
        st.pop("grok_i2v", None)
        st.pop("grok_settings", None)
        st.pop("omni_flash_settings", None)
        st["ts"] = _now()
        return {"ok": True}

    # ---- GOOGLE OMNI FLASH Text→Video / Image→Video / Video Edit ----
    if st.get("mode") in ("omni_flash_t2v", "omni_flash_i2v", "omni_flash_video_edit") and incoming_text:
        if _is_nav_or_menu_text(incoming_text):
            _set_mode(chat_id, user_id, "chat")
            st.pop("omni_flash_t2v", None)
            st.pop("omni_flash_i2v", None)
            st.pop("omni_flash_video_edit", None)
            st.pop("omni_flash_settings", None)
            st["ts"] = _now()
            await tg_send_message(chat_id, "Ок. Вышел из Google Omni Flash. Главное меню.", reply_markup=_main_menu_for(user_id))
            return {"ok": True}

        if st.get("mode") == "omni_flash_i2v":
            oi = st.get("omni_flash_i2v") or {}
            step = (oi.get("step") or "need_images")
            if step == "need_images" and incoming_text.strip().lower() in {"готово", "готов", "старт"}:
                images = [str(url or "").strip() for url in (oi.get("image_urls") or []) if str(url or "").strip()]
                if not images:
                    await tg_send_message(chat_id, "Сначала пришли хотя бы одно фото-референс.", reply_markup=_help_menu_for(user_id))
                    return {"ok": True}
                oi["image_urls"] = images[:7]
                oi.pop("images", None)
                oi["step"] = "need_prompt"
                st["omni_flash_i2v"] = oi
                st["ts"] = _now()
                await tg_send_message(chat_id, "Фото собраны ✅ Теперь пришли промпт текстом.", reply_markup=_help_menu_for(user_id))
                return {"ok": True}

        if st.get("mode") == "omni_flash_video_edit":
            ov = st.get("omni_flash_video_edit") or {}
            step = (ov.get("step") or "need_video")
            if step == "need_video":
                await tg_send_message(chat_id, f"Сначала пришли исходное видео до {KIE_OMNI_VIDEO_EDIT_MAX_DURATION_SEC} секунд.", reply_markup=_help_menu_for(user_id))
                return {"ok": True}
            if step == "need_images" and incoming_text.strip().lower() in {"готово", "готов", "старт"}:
                ov["step"] = "need_prompt"
                st["omni_flash_video_edit"] = ov
                st["ts"] = _now()
                await tg_send_message(chat_id, "Ок ✅ Теперь пришли промпт: что изменить в видео.", reply_markup=_help_menu_for(user_id))
                return {"ok": True}

        settings = st.get("omni_flash_settings") or {}
        duration = normalize_gemini_omni_duration(settings.get("duration") or 8)
        resolution = normalize_gemini_omni_resolution(settings.get("resolution") or "1080p")
        aspect_ratio = normalize_gemini_omni_aspect_ratio(settings.get("aspect_ratio") or "16:9")
        current_mode = "video_edit" if st.get("mode") == "omni_flash_video_edit" else ("image_to_video" if st.get("mode") == "omni_flash_i2v" else "text_to_video")
        cost_tokens = int(gemini_omni_tokens_for_run(current_mode, duration, resolution))

        try:
            ensure_user_row(user_id)
            bal = int(get_balance(user_id) or 0)
        except Exception:
            bal = 0

        if bal < cost_tokens:
            await tg_send_message(chat_id, f"❌ Недостаточно токенов.\nНужно: {cost_tokens}\nБаланс: {bal}", reply_markup=_topup_balance_inline_kb())
            return {"ok": True}

        charge_ref_id = uuid4().hex
        try:
            add_tokens(
                user_id,
                -cost_tokens,
                reason="gemini_omni_video",
                ref_id=charge_ref_id,
                meta={
                    "provider": "google",
                    "model": "gemini-omni-video",
                    "duration": duration,
                    "resolution": resolution,
                    "aspect_ratio": aspect_ratio,
                    "flow": ("video_edit" if st.get("mode") == "omni_flash_video_edit" else ("i2v" if st.get("mode") == "omni_flash_i2v" else "t2v")),
                    "pricing": "fixed_per_video" if st.get("mode") == "omni_flash_video_edit" else "duration_based",
                },
            )
        except TypeError:
            add_tokens(user_id, -int(cost_tokens), reason="gemini_omni_video")

        try:
            if st.get("mode") == "omni_flash_i2v":
                oi = st.get("omni_flash_i2v") or {}
                if (oi.get("step") or "need_images") != "need_prompt":
                    await tg_send_message(chat_id, "Сначала пришли 1–7 фото для Google Omni Flash Image → Video.", reply_markup=_help_menu_for(user_id))
                    add_tokens(user_id, int(cost_tokens), reason="gemini_omni_video_refund", ref_id=uuid4().hex, meta={"stage": "need_images"})
                    return {"ok": True}
                image_urls = [str(url or "").strip() for url in (oi.get("image_urls") or []) if str(url or "").strip()]
                if not image_urls:
                    await tg_send_message(chat_id, "Не хватает фото-референсов. Пришли фото и повтори промпт.", reply_markup=_help_menu_for(user_id))
                    add_tokens(user_id, int(cost_tokens), reason="gemini_omni_video_refund", ref_id=uuid4().hex, meta={"stage": "missing_images"})
                    return {"ok": True}
                await _enqueue_tg_omni_flash_job(
                    chat_id=int(chat_id),
                    user_id=int(user_id),
                    mode="image_to_video",
                    prompt=incoming_text.strip(),
                    settings=settings,
                    reference_image_urls=image_urls,
                    charge_tokens=cost_tokens,
                    charge_ref_id=charge_ref_id,
                )
                await tg_send_message(chat_id, f"⏳ Google Omni Flash — генерация началась: Image → Video • {duration} сек • {resolution} • {aspect_ratio}", reply_markup=_help_menu_for(user_id))
            elif st.get("mode") == "omni_flash_video_edit":
                ov = st.get("omni_flash_video_edit") or {}
                source_video_upload_id = str(ov.get("source_video_upload_id") or "").strip()
                if not source_video_upload_id:
                    await tg_send_message(chat_id, f"Сначала пришли исходное видео до {KIE_OMNI_VIDEO_EDIT_MAX_DURATION_SEC} секунд.", reply_markup=_help_menu_for(user_id))
                    add_tokens(user_id, int(cost_tokens), reason="gemini_omni_video_refund", ref_id=uuid4().hex, meta={"stage": "missing_video"})
                    return {"ok": True}
                image_urls = [str(url or "").strip() for url in (ov.get("image_urls") or []) if str(url or "").strip()][:5]
                await _enqueue_tg_omni_flash_job(
                    chat_id=int(chat_id),
                    user_id=int(user_id),
                    mode="video_edit",
                    prompt=incoming_text.strip(),
                    settings=settings,
                    reference_image_urls=image_urls,
                    source_video_upload_id=source_video_upload_id,
                    source_video_end=int(max(1, min(float(KIE_OMNI_VIDEO_EDIT_MAX_DURATION_SEC), float(ov.get("source_video_duration") or KIE_OMNI_VIDEO_EDIT_MAX_DURATION_SEC)))),
                    charge_tokens=cost_tokens,
                    charge_ref_id=charge_ref_id,
                )
                await tg_send_message(chat_id, f"⏳ Google Omni Flash — Video Edit запущен • {resolution} • {aspect_ratio} • фикс {cost_tokens} ток.", reply_markup=_help_menu_for(user_id))
            else:
                await _enqueue_tg_omni_flash_job(
                    chat_id=int(chat_id),
                    user_id=int(user_id),
                    mode="text_to_video",
                    prompt=incoming_text.strip(),
                    settings=settings,
                    charge_tokens=cost_tokens,
                    charge_ref_id=charge_ref_id,
                )
                await tg_send_message(chat_id, f"⏳ Google Omni Flash — генерация началась: Text → Video • {duration} сек • {resolution} • {aspect_ratio}", reply_markup=_help_menu_for(user_id))
        except Exception as e:
            try:
                add_tokens(user_id, int(cost_tokens), reason="gemini_omni_video_refund", ref_id=uuid4().hex, meta={"stage": "enqueue_failed", "error": str(e)[:300]})
            except TypeError:
                add_tokens(user_id, int(cost_tokens), reason="gemini_omni_video_refund")
            await tg_send_message(chat_id, f"❌ Не удалось поставить Google Omni Flash в очередь: {e}", reply_markup=_main_menu_for(user_id))
            return {"ok": True}

        _set_mode(chat_id, user_id, "chat")
        st.pop("omni_flash_t2v", None)
        st.pop("omni_flash_i2v", None)
        st.pop("omni_flash_video_edit", None)
        st.pop("omni_flash_settings", None)
        st["ts"] = _now()
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

        if veo_model == "fast_relax":
            duration = normalize_veo31_fast_relax_duration(duration)
            resolution = normalize_veo31_fast_relax_resolution(resolution)
            aspect_ratio = normalize_veo31_fast_relax_aspect_ratio(aspect_ratio)
            cost_tokens = 0 if _veo31_fast_relax_is_included_for_user(user_id) else int(veo31_fast_relax_tokens_for_run())
            delay_sec = _veo31_fast_relax_delay_sec_for_user(user_id, cost_tokens)
            try:
                ensure_user_row(user_id)
                bal = int(get_balance(user_id) or 0)
            except Exception:
                bal = 0
            if cost_tokens > 0 and bal < cost_tokens:
                await tg_send_message(chat_id, f"❌ Недостаточно токенов.\nНужно: {cost_tokens}\nБаланс: {bal}", reply_markup=_topup_balance_inline_kb())
                return {"ok": True}
            charge_ref_id = uuid4().hex if cost_tokens > 0 else ""
            if cost_tokens > 0:
                try:
                    add_tokens(
                        user_id,
                        -cost_tokens,
                        reason="veo31_fast_relax_video",
                        ref_id=charge_ref_id,
                        meta={"provider": "veo", "model": VEO31_FAST_RELAX_DISPLAY_NAME, "duration": duration, "resolution": resolution, "aspect_ratio": aspect_ratio, "flow": "t2v", "pricing": "fixed_per_video"},
                    )
                except TypeError:
                    add_tokens(user_id, -int(cost_tokens), reason="veo31_fast_relax_video")
            try:
                await _enqueue_tg_veo_relax_job(
                    chat_id=int(chat_id),
                    user_id=int(user_id),
                    mode="text_to_video",
                    prompt=incoming_text.strip(),
                    settings={"duration": duration, "resolution": resolution, "aspect_ratio": aspect_ratio},
                    charge_tokens=cost_tokens,
                    charge_ref_id=charge_ref_id,
                    delay_sec=delay_sec,
                )
                queue_note = _veo31_fast_relax_queue_note(delay_sec)
                await tg_send_message(chat_id, f"⏳ {VEO31_FAST_RELAX_DISPLAY_NAME} {queue_note}: Text → Video • {duration} сек • {resolution} • {aspect_ratio} • {cost_tokens} ток.", reply_markup=_help_menu_for(user_id))
            except Exception as e:
                if int(cost_tokens or 0) > 0:
                    try:
                        add_tokens(user_id, int(cost_tokens), reason="veo31_fast_relax_video_refund", ref_id=charge_ref_id, meta={"stage": "enqueue_failed", "error": str(e)[:300]})
                    except TypeError:
                        add_tokens(user_id, int(cost_tokens), reason="veo31_fast_relax_video_refund")
                await tg_send_message(chat_id, f"❌ Не удалось поставить {VEO31_FAST_RELAX_DISPLAY_NAME} в очередь: {e}", reply_markup=_main_menu_for(user_id))
                return {"ok": True}
            _set_mode(chat_id, user_id, "chat")
            st.pop("veo_t2v", None)
            st.pop("veo_settings", None)
            st["ts"] = _now()
            sb_clear_user_state(user_id)
            return {"ok": True}

        # ---- VEO BILLING (Text→Video) ----
        _busy_start(int(user_id), "Veo видео")
        veo_charged = False
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
            veo_charged = True

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
                if not video_url:
                    raise RuntimeError("empty_video_url")
            except Exception as e:
                try:
                    if veo_charged:
                        try:
                            add_tokens(
                                user_id,
                                int(ch.total_tokens),
                                reason="veo_video_refund",
                                meta={
                                    "stage": "generation_failed",
                                    "flow": "t2v",
                                    "total_tokens": int(ch.total_tokens),
                                    "error": str(e)[:300],
                                },
                            )
                        except TypeError:
                            add_tokens(user_id, int(ch.total_tokens), reason="veo_video_refund")
                except Exception:
                    pass
                await tg_send_message(chat_id, "⚠️ Veo временно недоступен. Токены возвращены. Попробуй через минуту", reply_markup=_help_menu_for(user_id))
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

            if veo_model == "fast_relax":
                duration = normalize_veo31_fast_relax_duration(duration)
                resolution = normalize_veo31_fast_relax_resolution(resolution)
                aspect_ratio = normalize_veo31_fast_relax_aspect_ratio(aspect_ratio)
                cost_tokens = 0 if _veo31_fast_relax_is_included_for_user(user_id) else int(veo31_fast_relax_tokens_for_run())
                delay_sec = _veo31_fast_relax_delay_sec_for_user(user_id, cost_tokens)
                try:
                    ensure_user_row(user_id)
                    bal = int(get_balance(user_id) or 0)
                except Exception:
                    bal = 0
                if cost_tokens > 0 and bal < cost_tokens:
                    await tg_send_message(chat_id, f"❌ Недостаточно токенов.\nНужно: {cost_tokens}\nБаланс: {bal}", reply_markup=_topup_balance_inline_kb())
                    return {"ok": True}
                charge_ref_id = uuid4().hex if cost_tokens > 0 else ""
                if cost_tokens > 0:
                    try:
                        add_tokens(
                            user_id,
                            -cost_tokens,
                            reason="veo31_fast_relax_video",
                            ref_id=charge_ref_id,
                            meta={"provider": "veo", "model": VEO31_FAST_RELAX_DISPLAY_NAME, "duration": duration, "resolution": resolution, "aspect_ratio": aspect_ratio, "flow": "i2v", "pricing": "fixed_per_video", "last_frame": bool(last_frame_bytes)},
                        )
                    except TypeError:
                        add_tokens(user_id, -int(cost_tokens), reason="veo31_fast_relax_video")
                try:
                    await _enqueue_tg_veo_relax_job(
                        chat_id=int(chat_id),
                        user_id=int(user_id),
                        mode="image_to_video",
                        prompt=incoming_text.strip(),
                        settings={"duration": duration, "resolution": resolution, "aspect_ratio": aspect_ratio},
                        image_bytes=image_bytes,
                        image_name="start_frame.jpg",
                        last_frame_bytes=last_frame_bytes,
                        last_frame_name="last_frame.jpg",
                        charge_tokens=cost_tokens,
                        charge_ref_id=charge_ref_id,
                        delay_sec=delay_sec,
                    )
                    frame_note = "первый + последний кадр" if last_frame_bytes else "первый кадр"
                    queue_note = _veo31_fast_relax_queue_note(delay_sec)
                    await tg_send_message(chat_id, f"⏳ {VEO31_FAST_RELAX_DISPLAY_NAME} {queue_note}: Image → Video • {frame_note} • {duration} сек • {resolution} • {aspect_ratio} • {cost_tokens} ток.", reply_markup=_help_menu_for(user_id))
                except Exception as e:
                    if int(cost_tokens or 0) > 0:
                        try:
                            add_tokens(user_id, int(cost_tokens), reason="veo31_fast_relax_video_refund", ref_id=charge_ref_id, meta={"stage": "enqueue_failed", "error": str(e)[:300]})
                        except TypeError:
                            add_tokens(user_id, int(cost_tokens), reason="veo31_fast_relax_video_refund")
                    await tg_send_message(chat_id, f"❌ Не удалось поставить {VEO31_FAST_RELAX_DISPLAY_NAME} в очередь: {e}", reply_markup=_main_menu_for(user_id))
                    return {"ok": True}
                _set_mode(chat_id, user_id, "chat")
                st.pop("veo_i2v", None)
                st.pop("veo_settings", None)
                st["ts"] = _now()
                sb_clear_user_state(user_id)
                return {"ok": True}

            # ---- VEO BILLING (Image→Video) ----
            _busy_start(int(user_id), "Veo видео")
            veo_charged = False
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
                veo_charged = True

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
                    if not video_url:
                        raise RuntimeError("empty_video_url")
                except Exception as e:
                    try:
                        if veo_charged:
                            try:
                                add_tokens(
                                    user_id,
                                    int(ch.total_tokens),
                                    reason="veo_video_refund",
                                    meta={
                                        "stage": "generation_failed",
                                        "flow": "i2v",
                                        "total_tokens": int(ch.total_tokens),
                                        "error": str(e)[:300],
                                    },
                                )
                            except TypeError:
                                add_tokens(user_id, int(ch.total_tokens), reason="veo_video_refund")
                    except Exception:
                        pass
                    await tg_send_message(chat_id, "⚠️ Veo временно недоступен. Токены возвращены. Попробуй через минуту", reply_markup=_help_menu_for(user_id))
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
            "🍌 Nano Banana — редактирование фото.\n\n"
            "1) Пришли фото.\n"
            "2) Потом одним сообщением напиши что изменить (стиль/фон/детали).\n\n"
            f"{_nano_banana_basic_cost_hint_for_user(user_id, 'nano_banana', '2K')}",
            reply_markup=_photo_future_menu_keyboard(),
        )
        return {"ok": True}
        
    if incoming_text in ("🍌 Nano Banana 2", "Nano Banana 2"):
        _set_mode(chat_id, user_id, "nano_banana_2")
        await tg_send_message(
            chat_id,
            "🍌 Nano Banana 2 — генерация и редактирование (2K).\n\n"
            "Вариант A (фото→фото):\n"
            "1) Пришли фото.\n"
            "2) Затем напиши что изменить.\n\n"
            "Вариант B (текст→картинка):\n"
            "• Просто пришли текст без фото — сгенерирую картинку.\n\n"
            f"{_nano_banana_basic_cost_hint_for_user(user_id, 'nano_banana_2', '2K')}",
            reply_markup=_photo_future_menu_keyboard(),
        )
        await tg_send_message(
            chat_id,
            "Выбери формат Nano Banana 2:",
            reply_markup=_nano_banana_2_aspect_inline_kb("9:16"),
        )
        return {"ok": True}

    if incoming_text in ("🍌 Nano Banana Pro", "Nano Banana Pro"):
        _set_mode(chat_id, user_id, "nano_banana_pro")
        await tg_send_message(
            chat_id,
            "🍌 Nano Banana Pro — продвинутое редактирование 2 Токена.\n\n"
            "Вариант A (фото→фото):\n"
            "1) Пришли фото.\n"
            "2) Затем напиши что изменить.\n\n"
            "Вариант B (текст→картинка):\n"
            "• Просто пришли текст без фото — сгенерирую картинку.\n\n"
            "Стоимость: 2 токена за результат.",
            reply_markup=_photo_future_menu_keyboard(),
        )
        await tg_send_message(
            chat_id,
            "Выбери формат Nano Banana Pro:",
            reply_markup=_nano_banana_pro_aspect_inline_kb("9:16"),
        )
        return {"ok": True}


    if incoming_text in ("🍌 Nano Banana Pro - NEW", "Nano Banana Pro - NEW", "Nano Banana Pro NEW"):
        _set_mode(chat_id, user_id, "nano_banana_pro_new")
        await tg_send_message(
            chat_id,
            "🍌 Nano Banana Pro - NEW — новая ветка.\n\n"
            "Вариант A (фото→фото):\n"
            "• Можно добавить до 8 фото-референсов.\n"
            "• Когда закончил — нажми «Готово» или просто отправь prompt текстом.\n\n"
            "Вариант B (текст→картинка):\n"
            "• Просто пришли текст без фото — сгенерирую картинку.\n\n"
            "Цена зависит от resolution:\n"
            "• 2K = 1 токен\n"
            "• 4K = 2 токена",
            reply_markup=_photo_future_menu_keyboard(),
        )
        await tg_send_message(
            chat_id,
            "Выбери resolution и формат Nano Banana Pro - NEW:",
            reply_markup=_nano_banana_pro_new_inline_kb("9:16", "2K", 0),
        )
        return {"ok": True}

    if incoming_text in ("🖼 Апскейл фото", "Апскейл фото"):
        _set_mode(chat_id, user_id, "topaz_photo")
        await tg_send_message(
            chat_id,
            "🖼 Topaz Upscale Фото.\n\n"
            "Выбери пресет ниже, затем пришли фото.\n"
            "• Standard — 2 токена\n"
            "• Detail — 3 токена\n"
            "• Max — 4 токена",
            reply_markup=_topaz_photo_presets_keyboard(),
        )
        return {"ok": True}

    if incoming_text == "Topaz Фото • Standard • 2 токена":
        _set_mode(chat_id, user_id, "topaz_photo")
        st["topaz_photo"] = {"step": "need_photo", "preset_slug": "standard"}
        await tg_send_message(
            chat_id,
            "🖼 Topaz Фото • Standard выбран.\nТеперь пришли фото. Стоимость: 2 токена.",
            reply_markup=_topaz_photo_presets_keyboard(),
        )
        return {"ok": True}

    if incoming_text == "Topaz Фото • Detail • 3 токена":
        _set_mode(chat_id, user_id, "topaz_photo")
        st["topaz_photo"] = {"step": "need_photo", "preset_slug": "detail"}
        await tg_send_message(
            chat_id,
            "🖼 Topaz Фото • Detail выбран.\nТеперь пришли фото. Стоимость: 3 токена.",
            reply_markup=_topaz_photo_presets_keyboard(),
        )
        return {"ok": True}

    if incoming_text == "Topaz Фото • Max • 4 токена":
        _set_mode(chat_id, user_id, "topaz_photo")
        st["topaz_photo"] = {"step": "need_photo", "preset_slug": "max"}
        await tg_send_message(
            chat_id,
            "🖼 Topaz Фото • Max выбран.\nТеперь пришли фото. Стоимость: 4 токена.",
            reply_markup=_topaz_photo_presets_keyboard(),
        )
        return {"ok": True}

    if incoming_text in ("🎬 Апскейл видео", "Апскейл видео"):
        _set_mode(chat_id, user_id, "topaz_video")
        await tg_send_message(
            chat_id,
            "🎬 Topaz Upscale Видео.\n\n"
            "Выбери пресет ниже, затем пришли видео как обычное видео Telegram (не документ).\n"
            "Цена считается по длительности ролика.",
            reply_markup=_topaz_video_presets_keyboard(),
        )
        return {"ok": True}

    if incoming_text == "Topaz Видео • HD Smooth • 1 токен / 5 сек":
        _set_mode(chat_id, user_id, "topaz_video")
        st["topaz_video"] = {"step": "need_video", "preset_slug": "hd_smooth"}
        await tg_send_message(
            chat_id,
            "🎬 Topaz Видео • HD Smooth выбран.\nПришли видео как обычное видео Telegram.\nСтоимость: 1 токен за каждые 5 секунд.",
            reply_markup=_topaz_video_presets_keyboard(),
        )
        return {"ok": True}

    if incoming_text == "Topaz Видео • Full HD • 2 токена / 5 сек":
        _set_mode(chat_id, user_id, "topaz_video")
        st["topaz_video"] = {"step": "need_video", "preset_slug": "full_hd"}
        await tg_send_message(
            chat_id,
            "🎬 Topaz Видео • Full HD выбран.\nПришли видео как обычное видео Telegram.\nСтоимость: 2 токена за каждые 5 секунд.",
            reply_markup=_topaz_video_presets_keyboard(),
        )
        return {"ok": True}

    if incoming_text == "Topaz Видео • Full HD Smooth • 3 токена / 5 сек":
        _set_mode(chat_id, user_id, "topaz_video")
        st["topaz_video"] = {"step": "need_video", "preset_slug": "full_hd_smooth"}
        await tg_send_message(
            chat_id,
            "🎬 Topaz Видео • Full HD Smooth выбран.\nПришли видео как обычное видео Telegram.\nСтоимость: 3 токена за каждые 5 секунд.",
            reply_markup=_topaz_video_presets_keyboard(),
        )
        return {"ok": True}

    if incoming_text == "Seedream 4.5":
        _set_mode(chat_id, user_id, "seedream_single")
        st.pop("photo_submenu", None)
        st["seedream_single"] = {
            "step": "need_photo",
            "photo_bytes": None,
            "photo_file_id": None,
            "aspect_ratio": "9:16",
            "model": "seedream_45",
        }
        await tg_send_message(
            chat_id,
            "Seedream 4.5 • режим «1 фото + промпт».\n"
            "1) Пришли одно фото.\n"
            "2) Потом одним сообщением напиши, что сделать.\n"
            "Промпт уйдёт как есть, без внутренней обвязки.\n\n"
            "Стоимость: 1 токен.",
            reply_markup=_photo_future_menu_keyboard(),
        )
        await tg_send_message(
            chat_id,
            "Выбери формат Seedream 4.5:",
            reply_markup=_seedream_aspect_inline_kb("single", "9:16"),
        )
        return {"ok": True}

    if incoming_text in ("2 фото", "Картинка+Картинка"):
        _set_mode(chat_id, user_id, "two_photos")
        st.pop("photo_submenu", None)
        st["two_photos"] = {
            "step": "need_photo_1",
            "photo1_bytes": None,
            "photo1_file_id": None,
            "photo2_bytes": None,
            "photo2_file_id": None,
            "aspect_ratio": "9:16",
            "model": "seedream_45",
        }
        await tg_send_message(
            chat_id,
            "Seedream 4.5 • режим «Картинка+Картинка».\n"
            "1) Пришли Фото 1 — это ОСНОВА.\n"
            "2) Потом пришли Фото 2 — это референс.\n"
            "3) Потом одним сообщением напиши, что сделать. Промпт уйдёт как есть, без внутренней обвязки.\n\n"
            "Стоимость: 1 токен.",
            reply_markup=_photo_future_menu_keyboard(),
        )
        await tg_send_message(
            chat_id,
            "Выбери формат Seedream 4.5:",
            reply_markup=_seedream_aspect_inline_kb("i2i", "9:16"),
        )
        return {"ok": True}

    if incoming_text == "Текст→Картинка":
        if str(st.get("photo_submenu") or "").strip().lower() in {"gpt_image_2_kie", "gpt_image_2"}:
            _set_mode(chat_id, user_id, "gpt_image_2_kie_t2i")
            st.pop("photo_submenu", None)
            st["gpt_image_2_kie_t2i"] = {"step": "need_prompt", "aspect_ratio": "16:9", "resolution": "2K"}
            await tg_send_message(
                chat_id,
                "Gpt Image 2 • режим «Текст→Картинка».\nВыбери качество/формат и пришли текст одним сообщением.",
                reply_markup=_photo_future_menu_keyboard(),
            )
            await tg_send_message(
                chat_id,
                "Выбери качество и формат Gpt Image 2:",
                reply_markup=_gpt_image_2_kie_inline_kb("t2i", "16:9", "2K", 0),
            )
            return {"ok": True}


        # Text-to-image mode (no input photo required)
        _set_mode(chat_id, user_id, "t2i")
        st.pop("photo_submenu", None)
        st["t2i"] = {"step": "need_prompt", "aspect_ratio": "9:16", "model": "seedream_45"}
        await tg_send_message(
            chat_id,
            "Seedream 4.5 • режим «Текст→Картинка».\n"
            "Напиши одним сообщением, что нужно сгенерировать.\n"
            "Промпт уйдёт как есть, без внутренней обвязки.\n\n"
            "Стоимость: Free — 1 токен. Spark/Pulse/Nexus — бесплатно.",
            reply_markup=_photo_future_menu_keyboard(),
        )
        await tg_send_message(
            chat_id,
            "Выбери формат Seedream 4.5:",
            reply_markup=_seedream_aspect_inline_kb("t2i", "9:16"),
        )
        return {"ok": True}

    if incoming_text == "Картинка→Картинка":
        if str(st.get("photo_submenu") or "").strip().lower() in {"gpt_image_2_kie", "gpt_image_2"}:
            _set_mode(chat_id, user_id, "gpt_image_2_kie_i2i")
            st.pop("photo_submenu", None)
            st["gpt_image_2_kie_i2i"] = {"step": "need_image", "photo_file_id": None, "photo_file_ids": [], "photo_urls": [], "aspect_ratio": "16:9", "resolution": "2K"}
            await tg_send_message(
                chat_id,
                "Gpt Image 2 • режим «Картинка→Картинка».\n1) Пришли от 1 до 16 фото.\n2) Можно отправить несколько сообщений с фото.\n3) Потом одним сообщением напиши, что нужно изменить.",
                reply_markup=_photo_future_menu_keyboard(),
            )
            await tg_send_message(
                chat_id,
                "Выбери качество и формат результата Gpt Image 2:",
                reply_markup=_gpt_image_2_kie_inline_kb("i2i", "16:9", "2K", 0),
            )
            return {"ok": True}

        # Fallback for old cached keyboards: route Image→Image to the KIE provider too.
        _set_mode(chat_id, user_id, "gpt_image_2_kie_i2i")
        st.pop("photo_submenu", None)
        st["gpt_image_2_kie_i2i"] = {"step": "need_image", "photo_file_id": None, "photo_file_ids": [], "photo_urls": [], "aspect_ratio": "16:9", "resolution": "2K"}
        await tg_send_message(
            chat_id,
            "Gpt Image 2 • режим «Картинка→Картинка».\n1) Пришли от 1 до 16 фото.\n2) Можно отправить несколько сообщений с фото.\n3) Потом одним сообщением напиши, что нужно изменить.",
            reply_markup=_photo_future_menu_keyboard(),
        )
        await tg_send_message(
            chat_id,
            "Выбери качество и формат результата Gpt Image 2:",
            reply_markup=_gpt_image_2_kie_inline_kb("i2i", "16:9", "2K", 0),
        )
        return {"ok": True}
    if incoming_text == "Помощь":
        await tg_send_message(
            chat_id,
            "• 🤖 NABEX.RU - Наш Сайт.\n"
            "• 🖼 Фото будущего: редактирование, афиши, нейро-фотосессии.\n"
            "  — фото → потом текст\n"
            "  — нужен текст/цена/надпись → сделаю афишу GPT Image2\n"
            "  — 'без текста' / описание сцены → фото-редактирование\n"
            "• 🎬 Видео будущего: текст → видео или фото → видео.\n"
            "• 🎵 Музыка будущего: генерация треков (Suno / Udio).\n"
            "• 🔊 Озвучить текст: профессиональная AI-озвучка.\n"
            "• 🍌 Nano Banana 2: генерация или редактирование изображений через PiAPI.\n"
            "• 🍌 Nano Banana Pro: продвинутый AI-редактор изображений.\n"
            "• 🍌 Nano Banana Pro - NEW: новая ветка с выбором 2K / 4K.\n"
            "• 💰 Баланс: токены, пополнение, история операций.\n"
            "• 🔄 Сбросить генерацию — если зациклилась/зависла\n• /reset — сбросить текущий режим\n",
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

        if st.get("mode") == "midjourney":
            mj = _midjourney_state(st)
            step = str(mj.get("step") or "need_prompt")
            if step not in {"need_style_ref", "need_omni_ref", "need_image_prompt_ref"}:
                await tg_send_message(chat_id, "Фото получил, но сейчас Midjourney ждёт prompt или выбор reference-кнопки.", reply_markup=_midjourney_settings_kb(mj, user_id=user_id))
                return {"ok": True}
            try:
                file_url = _midjourney_upload_reference_bytes(user_id=user_id, image_bytes=img_bytes, filename="telegram_photo.jpg", content_type="image/jpeg")
            except Exception as e:
                logging.exception("Midjourney reference upload failed")
                await tg_send_message(chat_id, f"❌ Не удалось загрузить reference для Midjourney: {e}", reply_markup=_midjourney_settings_kb(mj, user_id=user_id))
                return {"ok": True}
            if step == "need_style_ref":
                mj["style_ref_url"] = file_url
                mj["style_ref_file_id"] = str(file_id or "")
                mj["step"] = "need_prompt"
                st["midjourney"] = mj
                st["ts"] = _now()
                await tg_send_message(chat_id, _midjourney_settings_text(mj, user_id=user_id), reply_markup=_midjourney_settings_kb(mj, user_id=user_id))
                return {"ok": True}
            if step == "need_omni_ref":
                mj["omni_ref_url"] = file_url
                mj["omni_ref_file_id"] = str(file_id or "")
                mj["step"] = "need_prompt"
                st["midjourney"] = mj
                st["ts"] = _now()
                await tg_send_message(chat_id, _midjourney_settings_text(mj, user_id=user_id), reply_markup=_midjourney_settings_kb(mj, user_id=user_id))
                return {"ok": True}
            if step == "need_image_prompt_ref":
                urls = [str(x or "").strip() for x in (mj.get("image_prompt_urls") or []) if str(x or "").strip()]
                ids = [str(x or "").strip() for x in (mj.get("image_prompt_file_ids") or []) if str(x or "").strip()]
                if len(urls) >= 4:
                    await tg_send_message(chat_id, "У Midjourney V8.1 можно добавить максимум 4 image refs. Теперь пришли prompt или нажми запуск.", reply_markup=_midjourney_settings_kb(mj, user_id=user_id))
                    return {"ok": True}
                urls.append(file_url)
                ids.append(str(file_id or ""))
                mj["image_prompt_urls"] = urls[:4]
                mj["image_prompt_file_ids"] = ids[:4]
                mj["step"] = "need_prompt"
                st["midjourney"] = mj
                st["ts"] = _now()
                await tg_send_message(chat_id, _midjourney_settings_text(mj, user_id=user_id), reply_markup=_midjourney_settings_kb(mj, user_id=user_id))
                return {"ok": True}
            await tg_send_message(chat_id, "Фото получил, но сейчас Midjourney ждёт prompt или выбор reference-кнопки.", reply_markup=_midjourney_settings_kb(mj, user_id=user_id))
            return {"ok": True}

        if st.get("mode") == "gpt_image_2_kie_i2i":
            gi2k = st.get("gpt_image_2_kie_i2i") or {}
            photo_ids = [str(item or "").strip() for item in (gi2k.get("photo_file_ids") or []) if str(item or "").strip()]
            photo_urls = [str(item or "").strip() for item in (gi2k.get("photo_urls") or []) if str(item or "").strip()]
            if len(photo_ids) >= 16:
                await tg_send_message(
                    chat_id,
                    "У Gpt Image 2 можно использовать максимум 16 reference images. Уже набрано 16/16 ✅ Теперь просто напиши prompt.",
                    reply_markup=_gpt_image_2_kie_inline_kb("i2i", str(gi2k.get("aspect_ratio") or "16:9"), str(gi2k.get("resolution") or "2K"), 16),
                )
                return {"ok": True}

            try:
                ext, mime = validate_gpt_image_2_kie_reference_bytes(
                    img_bytes,
                    filename=f"telegram_photo_{len(photo_ids) + 1}.jpg",
                    content_type="image/jpeg",
                    source_label="reference image",
                )
            except Exception as e:
                await tg_send_message(chat_id, f"❌ {e}", reply_markup=_gpt_image_2_kie_inline_kb("i2i", str(gi2k.get("aspect_ratio") or "16:9"), str(gi2k.get("resolution") or "2K"), len(photo_ids)))
                return {"ok": True}

            photo_ids.append(str(file_id))
            try:
                input_path = f"gpt_image2_kie_inputs/{int(user_id)}/{int(time.time())}_{uuid4().hex[:10]}_{len(photo_ids)}.{ext}"
                uploaded_url = upload_bytes_to_supabase(input_path, img_bytes, mime)
                if uploaded_url:
                    photo_urls.append(str(uploaded_url).strip())
            except Exception as e:
                logging.exception("Gpt Image 2 input upload failed")
                await tg_send_message(chat_id, f"❌ Не удалось загрузить reference image для Gpt Image 2: {e}", reply_markup=_gpt_image_2_kie_inline_kb("i2i", str(gi2k.get("aspect_ratio") or "16:9"), str(gi2k.get("resolution") or "2K"), len(photo_ids)))
                return {"ok": True}

            gi2k["photo_file_ids"] = photo_ids[:16]
            gi2k["photo_urls"] = photo_urls[:16]
            gi2k["photo_file_id"] = str(photo_ids[0]) if photo_ids else None
            gi2k["resolution"], gi2k["aspect_ratio"] = _gpt_image_2_kie_options(gi2k.get("resolution") or "2K", gi2k.get("aspect_ratio") or "16:9")
            gi2k["step"] = "need_prompt"
            st["gpt_image_2_kie_i2i"] = gi2k
            st["ts"] = _now()
            count_refs = len(gi2k.get("photo_file_ids") or [])
            current_aspect = str(gi2k.get("aspect_ratio") or "16:9")
            current_resolution = str(gi2k.get("resolution") or "2K")
            if count_refs >= 16:
                msg = f"Фото принято 16/16 ✅\nGpt Image 2: {current_resolution} • {_gpt_image_2_kie_user_cost(user_id, current_resolution)} ток.\nФормат: {current_aspect}\nТеперь напиши prompt одним сообщением."
            else:
                msg = f"Фото принято {count_refs}/16 ✅\nGpt Image 2: {current_resolution} • {_gpt_image_2_kie_user_cost(user_id, current_resolution)} ток.\nФормат: {current_aspect}\nМожешь отправить ещё фото или сразу написать prompt."
            await tg_send_message(
                chat_id,
                msg,
                reply_markup=_gpt_image_2_kie_inline_kb("i2i", current_aspect, current_resolution, count_refs),
            )
            return {"ok": True}

        if st.get("mode") == "gpt_image_2_i2i":
            gi2 = st.get("gpt_image_2_i2i") or {}
            photo_ids = [str(item or "").strip() for item in (gi2.get("photo_file_ids") or []) if str(item or "").strip()]
            if not photo_ids and str(gi2.get("photo_file_id") or "").strip():
                photo_ids = [str(gi2.get("photo_file_id") or "").strip()]
            photo_urls = [str(item or "").strip() for item in (gi2.get("photo_urls") or []) if str(item or "").strip()]
            if len(photo_ids) >= 4:
                await tg_send_message(
                    chat_id,
                    "У GPT Image 2.0 можно использовать максимум 4 фото в этом режиме. Уже набрано 4/4 ✅ Теперь просто напиши, что нужно изменить.",
                    reply_markup=_photo_future_menu_keyboard(),
                )
                return {"ok": True}

            photo_ids.append(str(file_id))
            try:
                ext, mime = _detect_image_type(img_bytes)
                input_path = f"gpt_image2_inputs/{int(user_id)}/{int(time.time())}_{uuid4().hex[:10]}_{len(photo_ids)}.{ext}"
                uploaded_url = upload_bytes_to_supabase(input_path, img_bytes, mime)
                if uploaded_url:
                    photo_urls.append(str(uploaded_url).strip())
            except Exception:
                logging.exception("GPT Image 2.0 input upload failed")

            gi2["photo_file_id"] = str(photo_ids[0]) if photo_ids else None
            gi2["photo_file_ids"] = photo_ids[:4]
            gi2["photo_urls"] = photo_urls[:4]
            gi2["aspect_ratio"] = str(gi2.get("aspect_ratio") or _gpt_image_2_aspect_for_size(gi2.get("size")) or "1:1")
            gi2["size"] = _gpt_image_2_size_for_aspect_ratio(gi2["aspect_ratio"])
            gi2["step"] = "need_prompt"
            st["gpt_image_2_i2i"] = gi2
            st["ts"] = _now()
            count_refs = len(gi2.get("photo_file_ids") or [])
            current_aspect = str(gi2.get("aspect_ratio") or "1:1")
            if count_refs >= 4:
                msg = f"Фото принято 4/4 ✅\nФормат результата: {current_aspect}\nТеперь напиши одним сообщением, что нужно изменить через GPT Image 2.0."
            else:
                msg = f"Фото принято {count_refs}/4 ✅\nФормат результата: {current_aspect}\nМожешь отправить ещё фото или сразу написать, что нужно изменить через GPT Image 2.0."
            await tg_send_message(
                chat_id,
                msg,
                reply_markup=_gpt_image_2_aspect_inline_kb("i2i", current_aspect),
            )
            return {"ok": True}

        if st.get("mode") == "seedream_single":
            sd = st.get("seedream_single") or {}
            step = (sd.get("step") or "need_photo")
            if step == "need_photo":
                sd["photo_bytes"] = img_bytes
                sd["photo_file_id"] = file_id
                sd["step"] = "need_prompt"
                st["seedream_single"] = sd
                st["ts"] = _now()
                current_aspect = (sd.get("aspect_ratio") or "9:16")
                await tg_send_message(
                    chat_id,
                    f"Фото принял ✅\nФормат: {current_aspect}\nТеперь напиши одним сообщением, что сделать. Промпт уйдёт как есть.\n\nСтоимость: 1 токен.",
                    reply_markup=_photo_future_menu_keyboard(),
                )
                return {"ok": True}

        if st.get("mode") == "topaz_photo":
            tpz = st.get("topaz_photo") or {}
            preset_slug = str(tpz.get("preset_slug") or "").strip().lower()
            if not preset_slug:
                await tg_send_message(
                    chat_id,
                    "Сначала выбери пресет для Topaz Фото.",
                    reply_markup=_topaz_photo_presets_keyboard(),
                )
                return {"ok": True}

            cost = int(get_photo_preset_tokens(preset_slug))
            ensure_user_row(user_id)
            try:
                bal = float(get_balance(user_id) or 0)
            except Exception:
                bal = 0

            if bal < cost:
                await tg_send_message(
                    chat_id,
                    f"Недостаточно токенов 😕\nНужно: {cost} токен(а) для Topaz Фото.",
                    reply_markup=_topup_packs_kb(),
                )
                return {"ok": True}

            job_id = uuid4().hex
            charged = False
            try:
                try:
                    add_tokens(user_id, -cost, reason="topaz_image_upscale")
                except TypeError:
                    add_tokens(user_id, -int(cost), reason="topaz_image_upscale")
                charged = True

                await tg_send_message(
                    chat_id,
                    f"🖼 Topaz Фото ({preset_slug}) — запускаю…",
                    reply_markup=_photo_future_menu_keyboard(),
                )

                await enqueue_job(
                    {
                        "job_id": job_id,
                        "type": "topaz_image_upscale",
                        "chat_id": int(chat_id),
                        "user_id": int(user_id),
                        "photo_file_id": str(file_id or ""),
                        "preset_slug": preset_slug,
                        "charge_tokens": int(cost),
                        "refund_reason": "topaz_image_upscale_refund",
                        "reply_markup": _photo_future_menu_keyboard(),
                    },
                    queue_name=TOPAZ_PHOTO_QUEUE_NAME,
                )
            except Exception as e:
                if charged:
                    try:
                        try:
                            add_tokens(user_id, int(cost), reason="topaz_image_upscale_refund")
                        except TypeError:
                            add_tokens(user_id, int(cost), reason="topaz_image_upscale_refund")
                    except Exception:
                        pass
                await tg_send_message(
                    chat_id,
                    f"❌ Не удалось запустить Topaz Фото: {e}\nТокены возвращены.",
                    reply_markup=_photo_future_menu_keyboard(),
                )
                return {"ok": True}

            st["topaz_photo"] = {"step": "choose_preset", "preset_slug": None}
            st["ts"] = _now()
            return {"ok": True}




        
        
        
        
        if st.get("mode") == "chat" and st.get("ai_chat_mode") == "prompt":
            pb = st.get("ai_prompt") or _new_ai_prompt_state()
            step = str(pb.get("step") or "choose_root")
            if step == "choose_root":
                await tg_send_message(
                    chat_id,
                    "Сначала выбери раздел для промта ниже, а потом присылай фото.",
                    reply_markup=_ai_prompt_root_inline_kb(),
                )
                return {"ok": True}
            if step == "choose_video_target":
                await tg_send_message(
                    chat_id,
                    "Сначала выбери видеомодель ниже, а потом присылай фото.",
                    reply_markup=_ai_prompt_video_inline_kb(),
                )
                return {"ok": True}

            imgs = list(pb.get("images") or [])
            if len(imgs) >= PROMPT_BUILDER_MAX_IMAGES:
                await tg_send_message(
                    chat_id,
                    f"Уже загружено максимум фото: {PROMPT_BUILDER_MAX_IMAGES}. Нажми «Очистить фото» или пришли идею текстом.",
                    reply_markup=_ai_prompt_tools_inline_kb(pb),
                )
                return {"ok": True}

            imgs.append(img_bytes)
            pb["images"] = imgs
            st["ai_prompt"] = pb
            st["ts"] = _now()

            caption_prompt = (incoming_text or "").strip()
            if caption_prompt and not _is_nav_or_menu_text(caption_prompt):
                if not await _tg_consume_free_chat_or_notify(chat_id, user_id):
                    st["ts"] = _now()
                    return {"ok": True}

                result = await _ai_prompt_generate(pb, caption_prompt)
                pb["last_prompt"] = result
                st["ai_prompt"] = pb
                st["ts"] = _now()
                await tg_send_message(chat_id, result, reply_markup=_ai_prompt_tools_inline_kb(pb))
                return {"ok": True}

            await tg_send_message(
                chat_id,
                f"Фото добавил ✅\n\n{_ai_prompt_waiting_text(pb)}",
                reply_markup=_ai_prompt_tools_inline_kb(pb),
            )
            return {"ok": True}

        # ---- NANO BANANA: ждём фото ----
        if st.get("mode") == "nano_banana":
            nb = st.get("nano_banana") or {}
            step = (nb.get("step") or "need_photo")
            if step == "need_photo":
                nb["photo_bytes"] = img_bytes
                nb["photo_file_id"] = file_id
                nb["step"] = "need_prompt"
                st["nano_banana"] = nb
                st["ts"] = _now()
                await tg_send_message(
                    chat_id,
                    f"Фото принял ✅\nТеперь напиши одним сообщением, что изменить (фон/стиль/детали).\n\n{_nano_banana_basic_cost_hint_for_user(user_id, 'nano_banana', '2K')}",
                    reply_markup=_photo_future_menu_keyboard(),
                )
                return {"ok": True}
                
        # ---- NANO BANANA 2 (PiAPI): ждём фото ----
        if st.get("mode") == "nano_banana_2":
            nb2 = st.get("nano_banana_2") or {}
            step = (nb2.get("step") or "need_photo")
            if step == "need_photo":
                nb2["photo_bytes"] = img_bytes
                nb2["photo_file_id"] = file_id
                nb2["step"] = "need_prompt"
                st["nano_banana_2"] = nb2
                st["ts"] = _now()
                current_aspect = (nb2.get("aspect_ratio") or "9:16")
                await tg_send_message(
                    chat_id,
                    f"Фото принял ✅\nФормат: {current_aspect}\nТеперь напиши одним сообщением, что изменить (фон/стиль/детали).\n\n{_nano_banana_basic_cost_hint_for_user(user_id, 'nano_banana_2', '2K')}",
                    reply_markup=_photo_future_menu_keyboard(),
                )
                return {"ok": True}

        # ---- NANO BANANA PRO (PiAPI): ждём фото ----
        if st.get("mode") == "nano_banana_pro":
            nbp = st.get("nano_banana_pro") or {}
            step = (nbp.get("step") or "need_photo")
            if step == "need_photo":
                nbp["photo_bytes"] = img_bytes
                nbp["photo_file_id"] = file_id
                nbp["step"] = "need_prompt"
                st["nano_banana_pro"] = nbp
                st["ts"] = _now()
                current_aspect = (nbp.get("aspect_ratio") or "9:16")
                await tg_send_message(
                    chat_id,
                    f"Фото принял ✅\nФормат: {current_aspect}\nТеперь напиши одним сообщением, что изменить (фон/стиль/детали).\n\nСтоимость: 2 токена.",
                    reply_markup=_photo_future_menu_keyboard(),
                )
                return {"ok": True}

        # ---- NANO BANANA PRO - NEW (KIE): ждём фото ----
        if st.get("mode") == "nano_banana_pro_new":
            nbpn = st.get("nano_banana_pro_new") or {}
            photo_ids = [str(item or "").strip() for item in (nbpn.get("photo_file_ids") or []) if str(item or "").strip()]
            if len(photo_ids) >= 8:
                current_aspect = (nbpn.get("aspect_ratio") or "9:16")
                current_resolution = str(nbpn.get("resolution") or "2K").upper() or "2K"
                await tg_send_message(
                    chat_id,
                    "Уже загружено 8/8 фото. Нажми «Готово», очисти фото или сразу отправь prompt текстом.",
                    reply_markup=_nano_banana_pro_new_inline_kb(current_aspect, current_resolution, len(photo_ids)),
                )
                return {"ok": True}

            photo_ids.append(file_id)
            nbpn["photo_bytes"] = img_bytes
            nbpn["photo_file_id"] = file_id
            nbpn["photo_file_ids"] = photo_ids
            nbpn["step"] = "collect_refs"
            st["nano_banana_pro_new"] = nbpn
            st["ts"] = _now()
            current_aspect = (nbpn.get("aspect_ratio") or "9:16")
            current_resolution = str(nbpn.get("resolution") or "2K").upper() or "2K"
            current_cost = nano_banana_pro_new_cost(current_resolution)
            await tg_send_message(
                chat_id,
                f"Фото #{len(photo_ids)} из 8 принял ✅\nResolution: {current_resolution}\nФормат: {current_aspect}\n\nМожешь прислать ещё фото, нажать «Готово» или сразу отправить prompt текстом.\nСтоимость: {current_cost} токен(а).",
                reply_markup=_nano_banana_pro_new_inline_kb(current_aspect, current_resolution, len(photo_ids)),
            )
            return {"ok": True}

        if st.get("mode") == "topaz_photo":
            tpz = st.get("topaz_photo") or {}
            preset_slug = str(tpz.get("preset_slug") or "").strip().lower()
            if not preset_slug:
                await tg_send_message(
                    chat_id,
                    "Сначала выбери пресет для Topaz Фото.",
                    reply_markup=_topaz_photo_presets_keyboard(),
                )
                return {"ok": True}

            cost = int(get_photo_preset_tokens(preset_slug))
            ensure_user_row(user_id)
            try:
                bal = float(get_balance(user_id) or 0)
            except Exception:
                bal = 0

            if bal < cost:
                await tg_send_message(
                    chat_id,
                    f"Недостаточно токенов 😕\nНужно: {cost} токен(а) для Topaz Фото.",
                    reply_markup=_topup_packs_kb(),
                )
                return {"ok": True}

            job_id = uuid4().hex
            charged = False
            try:
                try:
                    add_tokens(user_id, -cost, reason="topaz_image_upscale")
                except TypeError:
                    add_tokens(user_id, -int(cost), reason="topaz_image_upscale")
                charged = True

                await tg_send_message(
                    chat_id,
                    f"🖼 Topaz Фото ({preset_slug}) — запускаю…",
                    reply_markup=_photo_future_menu_keyboard(),
                )

                await enqueue_job(
                    {
                        "job_id": job_id,
                        "type": "topaz_image_upscale",
                        "chat_id": int(chat_id),
                        "user_id": int(user_id),
                        "photo_file_id": str(file_id or ""),
                        "preset_slug": preset_slug,
                        "charge_tokens": int(cost),
                        "refund_reason": "topaz_image_upscale_refund",
                        "reply_markup": _photo_future_menu_keyboard(),
                    },
                    queue_name=TOPAZ_PHOTO_QUEUE_NAME,
                )
            except Exception as e:
                if charged:
                    try:
                        try:
                            add_tokens(user_id, int(cost), reason="topaz_image_upscale_refund")
                        except TypeError:
                            add_tokens(user_id, int(cost), reason="topaz_image_upscale_refund")
                    except Exception:
                        pass
                await tg_send_message(
                    chat_id,
                    f"❌ Не удалось запустить Topaz Фото: {e}\nТокены возвращены.",
                    reply_markup=_photo_future_menu_keyboard(),
                )
                return {"ok": True}

            st["topaz_photo"] = {"step": "choose_preset", "preset_slug": None}
            st["ts"] = _now()
            return {"ok": True}


# ---- KLING Image → Video: accept start image as document ----
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

        # ---- GROK Image → Video: step=need_image ----
        if st.get("mode") == "grok_i2v":
            gi = st.get("grok_i2v") or {}
            step = (gi.get("step") or "need_image")

            if step == "need_image":
                gi["image_bytes"] = img_bytes
                gi["image_name"] = "grok_start.jpg"
                gi["step"] = "need_prompt"
                st["grok_i2v"] = gi
                st["ts"] = _now()

                gs = st.get("grok_settings") or {}
                model = normalize_grok_model(gs.get("model") or GROK_LEGACY_MODEL)
                if is_grok15_model(model):
                    duration = normalize_grok15_duration(gs.get("duration") or 5)
                    resolution = normalize_grok15_resolution(gs.get("resolution") or "480p")
                    aspect_ratio = normalize_grok15_aspect_ratio(gs.get("aspect_ratio") or "16:9")
                    provider_mode = ""
                    display_name = "Grok 1.5 Preview"
                else:
                    duration = normalize_grok_duration(gs.get("duration") or 6)
                    resolution = normalize_grok_resolution(gs.get("resolution") or "480p")
                    aspect_ratio = normalize_grok_aspect_ratio(gs.get("aspect_ratio") or "16:9")
                    provider_mode = normalize_grok_provider_mode(gs.get("provider_mode") or "normal")
                    display_name = "Grok"
                await tg_send_message(
                    chat_id,
                    f"Фото получил ✅\nТеперь напиши текстом, что должно происходить ({display_name} • {duration} сек • {resolution} • {aspect_ratio}" + (f" • Mode: {provider_mode}" if provider_mode else "") + ")",
                    reply_markup=_help_menu_for(user_id),
                )
                return {"ok": True}

            await tg_send_message(
                chat_id,
                "Стартовое фото уже есть ✅ Теперь жду ТЕКСТ для Grok.",
                reply_markup=_help_menu_for(user_id),
            )
            return {"ok": True}

        # ---- GOOGLE OMNI FLASH Video Edit: optional photo references ----
        if st.get("mode") == "omni_flash_video_edit":
            ov = st.get("omni_flash_video_edit") or {}
            step = (ov.get("step") or "need_video")
            if step == "need_video":
                await tg_send_message(chat_id, f"Сначала пришли исходное видео до {KIE_OMNI_VIDEO_EDIT_MAX_DURATION_SEC} секунд.", reply_markup=_help_menu_for(user_id))
                return {"ok": True}
            if step in {"need_images", "need_prompt"}:
                images = [str(url or "").strip() for url in (ov.get("image_urls") or []) if str(url or "").strip()]
                if len(images) >= 5:
                    ov["image_urls"] = images[:5]
                    ov["step"] = "need_prompt"
                    st["omni_flash_video_edit"] = ov
                    st["ts"] = _now()
                    await tg_send_message(chat_id, "Уже получено 5/5 фото ✅ Теперь пришли промпт, что изменить в видео.", reply_markup=_help_menu_for(user_id))
                    return {"ok": True}
                if img_bytes:
                    try:
                        ext, mime = _detect_image_type(img_bytes)
                        input_path = f"omni_flash_inputs/{int(user_id)}/{int(time.time())}_{uuid4().hex[:10]}_video_edit_ref_{len(images) + 1}.{ext}"
                        uploaded_url = upload_bytes_to_supabase(input_path, img_bytes, mime)
                    except Exception:
                        logging.exception("Google Omni Flash Video Edit Telegram ref upload failed")
                        await tg_send_message(chat_id, "❌ Не удалось загрузить фото-референс. Попробуй отправить фото ещё раз.", reply_markup=_help_menu_for(user_id))
                        return {"ok": True}
                    if uploaded_url:
                        images.append(str(uploaded_url).strip())
                images = images[:5]
                ov["image_urls"] = images
                ov["step"] = "need_images" if len(images) < 5 else "need_prompt"
                st["omni_flash_video_edit"] = ov
                st["ts"] = _now()
                if len(images) >= 5:
                    await tg_send_message(chat_id, "Получил 5/5 фото ✅ Теперь пришли промпт, что изменить в видео.", reply_markup=_help_menu_for(user_id))
                else:
                    await tg_send_message(chat_id, f"Фото #{len(images)} получил ✅\nПришли ещё фото (до 5) или сразу напиши промпт.", reply_markup=_help_menu_for(user_id))
                return {"ok": True}

        # ---- GOOGLE OMNI FLASH Image → Video: сбор фото-референсов ----
        if st.get("mode") == "omni_flash_i2v":
            oi = st.get("omni_flash_i2v") or {}
            step = (oi.get("step") or "need_images")
            if step == "need_images":
                images = [str(url or "").strip() for url in (oi.get("image_urls") or []) if str(url or "").strip()]
                limit = int((st.get("omni_flash_settings") or {}).get("max_images") or 7)
                limit = max(1, min(7, limit))
                if len(images) >= limit:
                    oi["image_urls"] = images[:limit]
                    oi.pop("images", None)
                    oi["step"] = "need_prompt"
                    st["omni_flash_i2v"] = oi
                    st["ts"] = _now()
                    await tg_send_message(chat_id, f"Уже получено {limit}/{limit} фото ✅ Теперь пришли ТЕКСТ (промпт), что должно происходить в видео.", reply_markup=_help_menu_for(user_id))
                    return {"ok": True}
                if img_bytes:
                    try:
                        ext, mime = _detect_image_type(img_bytes)
                        input_path = f"omni_flash_inputs/{int(user_id)}/{int(time.time())}_{uuid4().hex[:10]}_{len(images) + 1}.{ext}"
                        uploaded_url = upload_bytes_to_supabase(input_path, img_bytes, mime)
                    except Exception:
                        logging.exception("Google Omni Flash Telegram input upload failed")
                        await tg_send_message(chat_id, "❌ Не удалось загрузить фото-референс. Попробуй отправить фото ещё раз.", reply_markup=_help_menu_for(user_id))
                        return {"ok": True}
                    if uploaded_url:
                        images.append(str(uploaded_url).strip())
                images = images[:limit]
                oi["image_urls"] = images
                oi.pop("images", None)
                st["omni_flash_i2v"] = oi
                st["ts"] = _now()
                if len(images) >= limit:
                    oi["step"] = "need_prompt"
                    st["omni_flash_i2v"] = oi
                    st["ts"] = _now()
                    await tg_send_message(chat_id, f"Получил {limit}/{limit} фото ✅ Теперь пришли ТЕКСТ (промпт), что должно происходить в видео.", reply_markup=_help_menu_for(user_id))
                    return {"ok": True}
                await tg_send_message(chat_id, f"Фото #{len(images)} получил ✅\nПришли ещё фото (до {limit}) или напиши «Готово», чтобы перейти к промпту.", reply_markup=_help_menu_for(user_id))
                return {"ok": True}

            await tg_send_message(chat_id, "Фото уже собраны ✅ Теперь жду промпт текстом.", reply_markup=_help_menu_for(user_id))
            return {"ok": True}

        # ---- KLING 3.0 Turbo: приём стартового кадра через фото ----
        if st.get("mode") == "kling3_turbo_wait_prompt":
            kt = st.get("kling3_turbo_settings") or {}
            gen_mode = normalize_kling3_turbo_mode(kt.get("gen_mode") or "text_to_video")

            if gen_mode != "image_to_video":
                await tg_send_message(
                    chat_id,
                    "Для Kling 3.0 Turbo в режиме Text→Video фото не нужно. Пришли текстовый prompt.",
                    reply_markup=_help_menu_for(user_id),
                )
                return {"ok": True}

            if not kt.get("start_image_bytes") and not kt.get("start_image_url"):
                kt["start_image_bytes"] = img_bytes
                st["kling3_turbo_settings"] = kt
                st["ts"] = _now()
                await tg_send_message(chat_id, "Стартовый кадр получил ✅\nТеперь пришли prompt.", reply_markup=_help_menu_for(user_id))
                return {"ok": True}

            await tg_send_message(chat_id, "Стартовый кадр уже сохранён ✅ Теперь пришли prompt.", reply_markup=_help_menu_for(user_id))
            return {"ok": True}

        # ---- KLING 3.0 - New: приём общего стартового/последнего кадра через фото ----
        if st.get("mode") in ("kling3_kie_wait_prompt", "kling3_kie_wait_multishot_action"):
            ks3n = st.get("kling3_kie_settings") or {}
            gen_mode = str(ks3n.get("gen_mode") or "text_to_video")

            if gen_mode not in ("image_to_video", "multi_shot"):
                await tg_send_message(
                    chat_id,
                    "Для Kling 3.0 - New в режиме Text→Video фото не нужно. Пришли текстовый prompt.",
                    reply_markup=_help_menu_for(user_id),
                )
                return {"ok": True}

            if not ks3n.get("start_image_bytes"):
                ks3n["start_image_bytes"] = img_bytes
                st["kling3_kie_settings"] = ks3n
                st["ts"] = _now()
                if gen_mode == "multi_shot":
                    msg = "Общий стартовый кадр получил ✅\nОсновной prompt не нужен. Когда будешь готов — отправь сообщением: СТАРТ\nLast frame в Multi-shot не используется."
                else:
                    msg = "Стартовый кадр получил ✅\nЕсли хочешь — пришли ещё одно фото как последний кадр. Потом пришли prompt."
                await tg_send_message(chat_id, msg, reply_markup=_help_menu_for(user_id))
                return {"ok": True}

            if gen_mode == "image_to_video" and not ks3n.get("end_image_bytes"):
                ks3n["end_image_bytes"] = img_bytes
                st["kling3_kie_settings"] = ks3n
                st["ts"] = _now()
                await tg_send_message(chat_id, "Последний кадр получил ✅\nТеперь пришли prompt.", reply_markup=_help_menu_for(user_id))
                return {"ok": True}

            await tg_send_message(
                chat_id,
                "Стартовый кадр уже сохранён ✅\nЕсли всё готово — отправь сообщением: СТАРТ",
                reply_markup=_help_menu_for(user_id),
            )
            return {"ok": True}

        # ---- KLING 3.0 legacy/PiAPI disabled ----
        if st.get("mode") == "kling3_wait_prompt":
            st.pop("kling3_settings", None)
            _set_mode(chat_id, user_id, "chat")
            await tg_send_message(
                chat_id,
                "⚠️ Старый Kling/PiAPI Kling 3.0 отключён. Используй Kling 3.0 - New.",
                reply_markup=_main_menu_for(user_id),
            )
            return {"ok": True}

        # ---- KLING 3.0: приём 1-го/последнего кадра через фото ----
        if False and st.get("mode") == "kling3_wait_prompt":
            ks3 = st.get("kling3_settings") or {}
            gen_mode = (ks3.get("gen_mode") or "t2v")

            # Если режим не i2v/multishot — фото не нужно
            if gen_mode not in ("i2v", "multishot"):
                await tg_send_message(
                    chat_id,
                    "Для Kling 3.0 в режиме Text→Video фото не нужно.\n"
                    "Открой WebApp и выбери Image→Video, либо пришли текстовый промпт.",
                    reply_markup=_help_menu_for(user_id),
                )
                return {"ok": True}

            # 1-й кадр
            if not ks3.get("start_image_bytes"):
                ks3["start_image_bytes"] = img_bytes
                st["kling3_settings"] = ks3
                st["ts"] = _now()
                await tg_send_message(
                    chat_id,
                    "Стартовый кадр (1-й) получил ✅\n"
                    "Если хочешь — пришли ещё одно фото как последний кадр.\n"
                    "После этого пришли промпт.",
                    reply_markup=_help_menu_for(user_id),
                )
                return {"ok": True}

            # последний кадр
            if not ks3.get("end_image_bytes"):
                ks3["end_image_bytes"] = img_bytes
                st["kling3_settings"] = ks3
                st["ts"] = _now()
                await tg_send_message(
                    chat_id,
                    "Последний кадр получил ✅\nТеперь пришли промпт.",
                    reply_markup=_help_menu_for(user_id),
                )
                return {"ok": True}

            await tg_send_message(
                chat_id,
                "1-й и последний кадры уже загружены ✅\nТеперь жду промпт (или /start чтобы выйти).",
                reply_markup=_help_menu_for(user_id),
            )
            return {"ok": True}


        # ---- SEEDANCE 2 Image/Omni: сбор фото-референсов ----
        if st.get("mode") in ("seedance_i2v", "seedance_omni"):
            settings = st.get("seedance_settings") or {}
            limit = int(settings.get("max_images") or (7 if _seedance_uses_kie_backend(settings) else 2))
            total_limit = int(settings.get("max_total_refs") or 12)

            if st.get("mode") == "seedance_omni":
                so = st.get("seedance_omni") or {}
                step = (so.get("step") or "collect_refs")
                if step == "collect_refs":
                    image_ids = list(so.get("image_file_ids") or [])
                    video_ids = list(so.get("video_file_ids") or [])
                    audio_ids = list(so.get("audio_file_ids") or [])
                    total_refs = len(image_ids) + len(video_ids) + len(audio_ids)
                    if len(image_ids) >= limit:
                        await tg_send_message(chat_id, f"Фото refs уже {limit}/{limit}. Пришли видео/аудио refs или нажми «✅ Готово».", reply_markup=_seedance_refs_collect_kb())
                        return {"ok": True}
                    if total_refs >= total_limit:
                        await tg_send_message(chat_id, f"Всего refs уже {total_limit}/{total_limit}. Нажми «✅ Готово», чтобы перейти к промпту.", reply_markup=_seedance_refs_collect_kb())
                        return {"ok": True}
                    if file_id and (file_id not in image_ids):
                        image_ids.append(str(file_id))
                    so["image_file_ids"] = image_ids[:limit]
                    st["seedance_omni"] = so
                    st["ts"] = _now()
                    await tg_send_message(
                        chat_id,
                        f"Фото reference #{len(so['image_file_ids'])} получил ✅\n"
                        "Пришли ещё refs или нажми «✅ Готово», чтобы перейти к промпту.",
                        reply_markup=_seedance_refs_collect_kb(),
                    )
                    return {"ok": True}
                await tg_send_message(chat_id, "Референсы уже собраны ✅ Теперь жду промпт текстом. Если хочешь добавить refs — нажми «⬅️ Вернуться к refs».", reply_markup=_seedance_prompt_back_kb())
                return {"ok": True}

            si = st.get("seedance_i2v") or {}
            step = (si.get("step") or "need_images")
            if step == "need_images":
                imgs = list(si.get("image_file_ids") or [])
                if file_id and (file_id not in imgs):
                    imgs.append(str(file_id))
                imgs = imgs[:limit]
                si["image_file_ids"] = imgs
                st["seedance_i2v"] = si
                st["ts"] = _now()

                if len(imgs) >= limit:
                    si["step"] = "need_prompt"
                    st["seedance_i2v"] = si
                    st["ts"] = _now()
                    await tg_send_message(
                        chat_id,
                        f"Получил {limit}/{limit} фото ✅ Теперь пришли ТЕКСТ (промпт), что должно происходить в видео.",
                        reply_markup=_seedance_prompt_back_kb(),
                    )
                    return {"ok": True}

                await tg_send_message(
                    chat_id,
                    f"Фото #{len(imgs)} получил ✅\n"
                    f"Пришли ещё фото (до {limit}) или нажми «✅ Готово», чтобы перейти к промпту.",
                    reply_markup=_seedance_refs_collect_kb(),
                )
                return {"ok": True}

            await tg_send_message(
                chat_id,
                "Фото уже собраны ✅ Теперь жду промпт текстом. Если хочешь добавить refs — нажми «⬅️ Вернуться к refs».",
                reply_markup=_seedance_prompt_back_kb(),
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
                        "Стартовое фото получил ✅\nТеперь пришли референсы (до 3 фото).\n"
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
                        "Last frame получил ✅\nТеперь пришли референсы (до 3 фото).\n"
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

            # 3) Reference images (до 3)
            if step == "need_refs":
                refs = vi.get("reference_images_bytes") or []
                if not isinstance(refs, list):
                    refs = []

                if len(refs) >= 3:
                    await tg_send_message(
                        chat_id,
                        "Референсов уже 3/3 ✅\nНапиши «Готово», чтобы перейти к промпту.",
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
                    f"Референс принят ✅ ({len(refs)}/3)\n"
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
                    "Фото аватара получил ✅\nТеперь пришли ВИДЕО с движением (3–30 сек).",
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
                    "aspect_ratio": str(tp.get("aspect_ratio") or "9:16"),
                    "model": str(tp.get("model") or "seedream_45"),
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
        if st.get("mode") == "chat":
            if st.get("ai_chat_mode") != "chat":
                await tg_send_message(
                    chat_id,
                    "Фото получил, но сначала выбери модель чата: Claude Sonnet, Claude Opus 4.7, Claude Fable 5 или ChatGPT.",
                    reply_markup=_ai_chat_mode_inline_kb(),
                )
                return {"ok": True}
            if await _enqueue_tg_fable_image_chat_or_notify(
                chat_id=chat_id,
                user_id=user_id,
                st=st,
                file_id=str(file_id or ""),
                filename="telegram_photo.jpg",
                mime_type="image/jpeg",
                size_bytes=int(largest.get("file_size") or 0),
                prompt=incoming_text or "",
            ):
                return {"ok": True}
            if not await _tg_consume_free_chat_or_notify(chat_id, user_id):
                st["ts"] = _now()
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

    
    # ---------------- Video (message.video) ----------------
    vid = message.get("video") or {}
    if vid:
        if st.get("mode") == "seedance_omni":
            so = st.get("seedance_omni") or {}
            step = (so.get("step") or "collect_refs")
            if step != "collect_refs":
                await tg_send_message(chat_id, "Видео refs уже собраны ✅ Теперь жду промпт текстом. Если хочешь добавить refs — нажми «⬅️ Вернуться к refs».", reply_markup=_seedance_prompt_back_kb())
                return {"ok": True}
            file_id = str(vid.get("file_id") or "").strip()
            if not file_id:
                await tg_send_message(chat_id, "Не смог прочитать video file_id. Пришли видео ещё раз.", reply_markup=_seedance_refs_collect_kb())
                return {"ok": True}
            duration_hint = float(vid.get("duration") or 0)
            if duration_hint and duration_hint > 15.4:
                await tg_send_message(chat_id, "Видео reference слишком длинное. Для Seedance Omni сейчас максимум около 15 секунд.", reply_markup=_seedance_refs_collect_kb())
                return {"ok": True}
            settings = st.get("seedance_settings") or {}
            video_limit = int(settings.get("max_videos") or 3)
            total_limit = int(settings.get("max_total_refs") or 12)
            image_ids = list(so.get("image_file_ids") or [])
            video_ids = list(so.get("video_file_ids") or [])
            video_durations = list(so.get("video_durations_sec") or [])
            audio_ids = list(so.get("audio_file_ids") or [])
            if len(video_ids) >= video_limit:
                await tg_send_message(chat_id, f"Видео refs уже {video_limit}/{video_limit}. Пришли другие refs или нажми «✅ Готово».", reply_markup=_seedance_refs_collect_kb())
                return {"ok": True}
            if len(image_ids) + len(video_ids) + len(audio_ids) >= total_limit:
                await tg_send_message(chat_id, f"Всего refs уже {total_limit}/{total_limit}. Нажми «✅ Готово», чтобы перейти к промпту.", reply_markup=_seedance_refs_collect_kb())
                return {"ok": True}
            if file_id not in video_ids:
                video_ids.append(file_id)
                video_durations.append(float(duration_hint or 15.4))
            so["video_file_ids"] = video_ids[:video_limit]
            so["video_durations_sec"] = video_durations[:video_limit]
            st["seedance_omni"] = so
            st["ts"] = _now()
            await tg_send_message(chat_id, f"Видео reference #{len(so['video_file_ids'])} получил ✅\nПришли ещё refs или нажми «✅ Готово».", reply_markup=_seedance_refs_collect_kb())
            return {"ok": True}

        if st.get("mode") == "omni_flash_video_edit":
            ov = st.get("omni_flash_video_edit") or {}
            if (ov.get("step") or "need_video") != "need_video":
                await tg_send_message(chat_id, "Исходное видео уже получено ✅ Пришли до 5 фото-референсов или сразу промпт.", reply_markup=_help_menu_for(user_id))
                return {"ok": True}
            file_id = str(vid.get("file_id") or "").strip()
            if not file_id:
                await tg_send_message(chat_id, "Не смог прочитать video file_id. Пришли видео ещё раз.", reply_markup=_help_menu_for(user_id))
                return {"ok": True}
            duration_hint = float(vid.get("duration") or 0)
            if duration_hint > float(KIE_OMNI_VIDEO_EDIT_MAX_DURATION_SEC):
                await tg_send_message(chat_id, f"Видео слишком длинное. Максимум {KIE_OMNI_VIDEO_EDIT_MAX_DURATION_SEC} секунд.", reply_markup=_help_menu_for(user_id))
                return {"ok": True}
            try:
                file_path = await tg_get_file_path(file_id)
                video_bytes = await tg_download_file_bytes(file_path)
                source_upload_id, duration_sec = _upload_omni_flash_source_video(
                    user_id=user_id,
                    video_bytes=video_bytes,
                    filename="omni_flash_source.mp4",
                    content_type="video/mp4",
                    duration_hint=duration_hint,
                )
            except Exception as e:
                await tg_send_message(chat_id, f"❌ Не удалось загрузить видео для Google Omni Flash: {e}", reply_markup=_help_menu_for(user_id))
                return {"ok": True}
            ov["source_video_upload_id"] = source_upload_id
            ov.pop("source_video_url", None)
            ov["source_video_duration"] = round(float(duration_sec or 0), 2)
            ov["step"] = "need_images"
            ov.setdefault("image_urls", [])
            st["omni_flash_video_edit"] = ov
            st["ts"] = _now()
            await tg_send_message(chat_id, f"Видео получил ✅ ({int(round(duration_sec))} сек). Теперь пришли до 5 фото-референсов или сразу напиши промпт, что изменить.", reply_markup=_help_menu_for(user_id))
            return {"ok": True}

        if st.get("mode") == "topaz_video":
            tv = st.get("topaz_video") or {}
            preset_slug = str(tv.get("preset_slug") or "").strip().lower()
            if not preset_slug:
                await tg_send_message(
                    chat_id,
                    "Сначала выбери пресет для Topaz Видео.",
                    reply_markup=_topaz_video_presets_keyboard(),
                )
                return {"ok": True}

            file_id = vid.get("file_id")
            duration_sec = int(vid.get("duration") or 0)
            if not file_id:
                await tg_send_message(chat_id, "Не смог прочитать video file_id. Пришли видео ещё раз.", reply_markup=_topaz_video_presets_keyboard())
                return {"ok": True}
            if duration_sec <= 0:
                await tg_send_message(
                    chat_id,
                    "Не смог определить длительность видео. Пришли ролик как обычное Telegram-видео, не документом.",
                    reply_markup=_topaz_video_presets_keyboard(),
                )
                return {"ok": True}

            cost = int(calc_video_retail_tokens(preset_slug, duration_sec))
            ensure_user_row(user_id)
            try:
                bal = float(get_balance(user_id) or 0)
            except Exception:
                bal = 0

            if bal < cost:
                await tg_send_message(
                    chat_id,
                    f"Недостаточно токенов 😕\nНужно: {cost} токен(а) для Topaz Видео.",
                    reply_markup=_topup_packs_kb(),
                )
                return {"ok": True}

            job_id = uuid4().hex
            charged = False
            try:
                try:
                    add_tokens(user_id, -cost, reason="topaz_video_upscale")
                except TypeError:
                    add_tokens(user_id, -int(cost), reason="topaz_video_upscale")
                charged = True

                await tg_send_message(
                    chat_id,
                    f"🎬 Topaz Видео ({preset_slug}, {duration_sec} сек) — запускаю…",
                    reply_markup=_photo_future_menu_keyboard(),
                )

                await enqueue_job(
                    {
                        "job_id": job_id,
                        "type": "topaz_video_upscale",
                        "chat_id": int(chat_id),
                        "user_id": int(user_id),
                        "video_file_id": str(file_id or ""),
                        "preset_slug": preset_slug,
                        "duration_sec": int(duration_sec),
                        "charge_tokens": int(cost),
                        "refund_reason": "topaz_video_upscale_refund",
                        "reply_markup": _photo_future_menu_keyboard(),
                    },
                    queue_name=TOPAZ_VIDEO_QUEUE_NAME,
                )
            except Exception as e:
                if charged:
                    try:
                        try:
                            add_tokens(user_id, int(cost), reason="topaz_video_upscale_refund")
                        except TypeError:
                            add_tokens(user_id, int(cost), reason="topaz_video_upscale_refund")
                    except Exception:
                        pass
                await tg_send_message(
                    chat_id,
                    f"❌ Не удалось запустить Topaz Видео: {e}\nТокены возвращены.",
                    reply_markup=_photo_future_menu_keyboard(),
                )
                return {"ok": True}

            st["topaz_video"] = {"step": "choose_preset", "preset_slug": None}
            st["ts"] = _now()
            return {"ok": True}

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
            try:
                km["video_duration"] = int(float(vid.get("duration") or 0)) or None
            except Exception:
                km["video_duration"] = None
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
        filename = str(doc.get("file_name") or "").strip()
        filename_l = filename.lower()
        is_image_document = bool(file_id) and (
            mime.startswith("image/")
            or filename_l.endswith((".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"))
        )

        if file_id and is_image_document and st.get("mode") == "midjourney":
            mj = _midjourney_state(st)
            step = str(mj.get("step") or "need_prompt")
            if step not in {"need_style_ref", "need_omni_ref", "need_image_prompt_ref"}:
                await tg_send_message(chat_id, "Файл-фото получил, но сейчас Midjourney ждёт prompt или выбор reference-кнопки.", reply_markup=_midjourney_settings_kb(mj, user_id=user_id))
                return {"ok": True}
            try:
                file_path = await tg_get_file_path(file_id)
                img_bytes = await tg_download_file_bytes(file_path)
                file_url = _midjourney_upload_reference_bytes(user_id=user_id, image_bytes=img_bytes, filename=filename or "reference.jpg", content_type=mime or "image/jpeg")
            except Exception as e:
                logging.exception("Midjourney document reference upload failed")
                await tg_send_message(chat_id, f"Ошибка при загрузке reference: {e}", reply_markup=_midjourney_settings_kb(mj, user_id=user_id))
                return {"ok": True}
            if step == "need_style_ref":
                mj["style_ref_url"] = file_url
                mj["style_ref_file_id"] = str(file_id or "")
                mj["step"] = "need_prompt"
            elif step == "need_omni_ref":
                mj["omni_ref_url"] = file_url
                mj["omni_ref_file_id"] = str(file_id or "")
                mj["step"] = "need_prompt"
            elif step == "need_image_prompt_ref":
                urls = [str(x or "").strip() for x in (mj.get("image_prompt_urls") or []) if str(x or "").strip()]
                ids = [str(x or "").strip() for x in (mj.get("image_prompt_file_ids") or []) if str(x or "").strip()]
                if len(urls) >= 4:
                    await tg_send_message(chat_id, "У Midjourney V8.1 можно добавить максимум 4 image refs. Теперь пришли prompt или нажми запуск.", reply_markup=_midjourney_settings_kb(mj, user_id=user_id))
                    return {"ok": True}
                urls.append(file_url)
                ids.append(str(file_id or ""))
                mj["image_prompt_urls"] = urls[:4]
                mj["image_prompt_file_ids"] = ids[:4]
                mj["step"] = "need_prompt"
            else:
                await tg_send_message(chat_id, "Файл-фото получил, но сейчас Midjourney ждёт prompt или выбор reference-кнопки.", reply_markup=_midjourney_settings_kb(mj, user_id=user_id))
                return {"ok": True}
            st["midjourney"] = mj
            st["ts"] = _now()
            await tg_send_message(chat_id, _midjourney_settings_text(mj, user_id=user_id), reply_markup=_midjourney_settings_kb(mj, user_id=user_id))
            return {"ok": True}

        if file_id and st.get("mode") == "seedance_omni":
            so = st.get("seedance_omni") or {}
            step = (so.get("step") or "collect_refs")
            if step != "collect_refs":
                await tg_send_message(chat_id, "Refs уже собраны ✅ Теперь жду промпт текстом. Если хочешь добавить refs — нажми «⬅️ Вернуться к refs».", reply_markup=_seedance_prompt_back_kb())
                return {"ok": True}

            is_audio_document = mime.startswith("audio/") or mime in ("application/ogg",) or filename_l.endswith((".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus"))
            is_video_document = (mime.startswith("video/") or filename_l.endswith((".mp4", ".mov"))) and not is_audio_document
            if not (is_image_document or is_video_document or is_audio_document):
                await tg_send_message(chat_id, "Для Seedance Omni отправь фото, видео MP4/MOV или аудио MP3/WAV/M4A/OGG/OPUS.", reply_markup=_seedance_refs_collect_kb())
                return {"ok": True}
            if is_video_document and not (mime in ("video/mp4", "video/quicktime") or filename_l.endswith((".mp4", ".mov"))):
                await tg_send_message(chat_id, "Видео reference отправь в MP4 или MOV.", reply_markup=_seedance_refs_collect_kb())
                return {"ok": True}
            try:
                doc_size = int(doc.get("file_size") or 0)
            except Exception:
                doc_size = 0
            if is_audio_document and doc_size and doc_size > SEEDANCE_AUDIO_MAX_UPLOAD_BYTES:
                await tg_send_message(chat_id, f"Audio reference слишком большой. Лимит: до {SEEDANCE_AUDIO_MAX_UPLOAD_MB} МБ.", reply_markup=_seedance_refs_collect_kb())
                return {"ok": True}
            if not is_audio_document and doc_size and doc_size > 100 * 1024 * 1024:
                await tg_send_message(chat_id, "Файл слишком большой для reference. Лимит: до 100 МБ.", reply_markup=_seedance_refs_collect_kb())
                return {"ok": True}

            settings = st.get("seedance_settings") or {}
            image_limit = int(settings.get("max_images") or 7)
            video_limit = int(settings.get("max_videos") or 3)
            audio_limit = int(settings.get("max_audios") or 3)
            total_limit = int(settings.get("max_total_refs") or 12)
            image_ids = list(so.get("image_file_ids") or [])
            video_ids = list(so.get("video_file_ids") or [])
            video_durations = list(so.get("video_durations_sec") or [])
            audio_ids = list(so.get("audio_file_ids") or [])
            if len(image_ids) + len(video_ids) + len(audio_ids) >= total_limit:
                await tg_send_message(chat_id, f"Всего refs уже {total_limit}/{total_limit}. Нажми «✅ Готово», чтобы перейти к промпту.", reply_markup=_seedance_refs_collect_kb())
                return {"ok": True}

            if is_image_document:
                if len(image_ids) >= image_limit:
                    await tg_send_message(chat_id, f"Фото refs уже {image_limit}/{image_limit}. Нажми «✅ Готово» или отправь другой тип refs.", reply_markup=_seedance_refs_collect_kb())
                    return {"ok": True}
                if file_id not in image_ids:
                    image_ids.append(str(file_id))
                so["image_file_ids"] = image_ids[:image_limit]
                label = f"Фото reference #{len(so['image_file_ids'])}"
            elif is_video_document:
                if len(video_ids) >= video_limit:
                    await tg_send_message(chat_id, f"Видео refs уже {video_limit}/{video_limit}. Нажми «✅ Готово» или отправь другой тип refs.", reply_markup=_seedance_refs_collect_kb())
                    return {"ok": True}
                duration_sec = 0.0
                try:
                    file_path = await tg_get_file_path(str(file_id))
                    raw_video = await tg_download_file_bytes(file_path)
                    ext = "mov" if filename_l.endswith(".mov") or mime == "video/quicktime" else "mp4"
                    duration_sec = float(_probe_video_duration_from_bytes(raw_video, ext) or 0.0)
                except Exception:
                    duration_sec = 0.0
                if duration_sec and duration_sec > 15.4:
                    await tg_send_message(chat_id, "Видео reference слишком длинное. Для Seedance Omni сейчас максимум около 15 секунд.", reply_markup=_seedance_refs_collect_kb())
                    return {"ok": True}
                if file_id not in video_ids:
                    video_ids.append(str(file_id))
                    video_durations.append(float(duration_sec or 15.4))
                so["video_file_ids"] = video_ids[:video_limit]
                so["video_durations_sec"] = video_durations[:video_limit]
                label = f"Видео reference #{len(so['video_file_ids'])}"
            else:
                if len(audio_ids) >= audio_limit:
                    await tg_send_message(chat_id, f"Аудио refs уже {audio_limit}/{audio_limit}. Нажми «✅ Готово» или отправь другой тип refs.", reply_markup=_seedance_refs_collect_kb())
                    return {"ok": True}
                if file_id not in audio_ids:
                    audio_ids.append(str(file_id))
                so["audio_file_ids"] = audio_ids[:audio_limit]
                label = f"Аудио reference #{len(so['audio_file_ids'])}"

            st["seedance_omni"] = so
            st["ts"] = _now()
            await tg_send_message(chat_id, f"{label} получил ✅\nПришли ещё refs или нажми «✅ Готово».", reply_markup=_seedance_refs_collect_kb())
            return {"ok": True}

        if file_id and mime.startswith("video/") and st.get("mode") == "omni_flash_video_edit":
            ov = st.get("omni_flash_video_edit") or {}
            if (ov.get("step") or "need_video") != "need_video":
                await tg_send_message(chat_id, "Исходное видео уже получено ✅ Пришли до 5 фото-референсов или сразу промпт.", reply_markup=_help_menu_for(user_id))
                return {"ok": True}
            try:
                file_path = await tg_get_file_path(file_id)
                video_bytes = await tg_download_file_bytes(file_path)
                source_upload_id, duration_sec = _upload_omni_flash_source_video(
                    user_id=user_id,
                    video_bytes=video_bytes,
                    filename=str(doc.get("file_name") or "omni_flash_source.mp4"),
                    content_type=mime or "video/mp4",
                    duration_hint=0,
                )
            except Exception as e:
                await tg_send_message(chat_id, f"❌ Не удалось загрузить видео для Google Omni Flash: {e}", reply_markup=_help_menu_for(user_id))
                return {"ok": True}
            ov["source_video_upload_id"] = source_upload_id
            ov.pop("source_video_url", None)
            ov["source_video_duration"] = round(float(duration_sec or 0), 2)
            ov["step"] = "need_images"
            ov.setdefault("image_urls", [])
            st["omni_flash_video_edit"] = ov
            st["ts"] = _now()
            await tg_send_message(chat_id, f"Видео получил ✅ ({int(round(duration_sec))} сек). Теперь пришли до 5 фото-референсов или сразу напиши промпт, что изменить.", reply_markup=_help_menu_for(user_id))
            return {"ok": True}

        if file_id and mime.startswith("video/") and st.get("mode") == "topaz_video":
            await tg_send_message(
                chat_id,
                "Для Topaz Видео пришли ролик как обычное Telegram-видео, не документом. Так я вижу длительность и правильно считаю цену.",
                reply_markup=_topaz_video_presets_keyboard(),
            )
            return {"ok": True}

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
            km["video_duration"] = None
            km["step"] = "need_prompt"
            st["kling_mc"] = km
            st["ts"] = _now()

            await tg_send_message(
                chat_id,
                "Видео получил ✅\nТеперь напиши текстом, что должно происходить (или просто: Старт).",
                reply_markup=_help_menu_for(user_id),
            )
            return {"ok": True}
        if file_id and is_image_document:
            try:
                file_path = await tg_get_file_path(file_id)
                img_bytes = await tg_download_file_bytes(file_path)
            except Exception as e:
                await tg_send_message(chat_id, f"Ошибка при загрузке фото: {e}", reply_markup=_main_menu_for(user_id))
                return {"ok": True}

            # ---- Gpt Image 2: accept provider-safe reference image documents ----
            if st.get("mode") == "gpt_image_2_kie_i2i":
                gi2k = st.get("gpt_image_2_kie_i2i") or {}
                photo_ids = [str(item or "").strip() for item in (gi2k.get("photo_file_ids") or []) if str(item or "").strip()]
                photo_urls = [str(item or "").strip() for item in (gi2k.get("photo_urls") or []) if str(item or "").strip()]
                if len(photo_ids) >= 16:
                    await tg_send_message(
                        chat_id,
                        "У Gpt Image 2 можно использовать максимум 16 reference images. Уже набрано 16/16 ✅ Теперь просто напиши prompt.",
                        reply_markup=_gpt_image_2_kie_inline_kb("i2i", str(gi2k.get("aspect_ratio") or "16:9"), str(gi2k.get("resolution") or "2K"), 16),
                    )
                    return {"ok": True}

                try:
                    safe_ext, detected_mime = validate_gpt_image_2_kie_reference_bytes(
                        img_bytes,
                        filename=filename,
                        content_type=mime,
                        source_label="reference image",
                    )
                except Exception as e:
                    await tg_send_message(chat_id, f"❌ {e}", reply_markup=_gpt_image_2_kie_inline_kb("i2i", str(gi2k.get("aspect_ratio") or "16:9"), str(gi2k.get("resolution") or "2K"), len(photo_ids)))
                    return {"ok": True}

                photo_ids.append(str(file_id))
                try:
                    input_path = f"gpt_image2_kie_inputs/{int(user_id)}/{int(time.time())}_{uuid4().hex[:10]}_{len(photo_ids)}.{safe_ext}"
                    uploaded_url = upload_bytes_to_supabase(input_path, img_bytes, detected_mime)
                    if uploaded_url:
                        photo_urls.append(str(uploaded_url).strip())
                except Exception as e:
                    logging.exception("Gpt Image 2 document input upload failed")
                    await tg_send_message(chat_id, f"❌ Не удалось загрузить reference image для Gpt Image 2: {e}", reply_markup=_gpt_image_2_kie_inline_kb("i2i", str(gi2k.get("aspect_ratio") or "16:9"), str(gi2k.get("resolution") or "2K"), len(photo_ids)))
                    return {"ok": True}

                gi2k["photo_file_ids"] = photo_ids[:16]
                gi2k["photo_urls"] = photo_urls[:16]
                gi2k["photo_file_id"] = str(photo_ids[0]) if photo_ids else None
                gi2k["aspect_ratio"] = _gpt_image_2_kie_aspect(gi2k.get("aspect_ratio") or "16:9")
                gi2k["resolution"] = _gpt_image_2_kie_resolution(gi2k.get("resolution") or "2K")
                gi2k["step"] = "need_prompt"
                st["gpt_image_2_kie_i2i"] = gi2k
                st["ts"] = _now()
                count_refs = len(gi2k.get("photo_file_ids") or [])
                current_aspect = str(gi2k.get("aspect_ratio") or "16:9")
                current_resolution = str(gi2k.get("resolution") or "2K")
                if count_refs >= 16:
                    msg = f"Файл-фото принято 16/16 ✅\nGpt Image 2: {current_resolution} • {_gpt_image_2_kie_user_cost(user_id, current_resolution)} ток.\nФормат: {current_aspect}\nТеперь напиши prompt одним сообщением."
                else:
                    msg = f"Файл-фото принято {count_refs}/16 ✅\nGpt Image 2: {current_resolution} • {_gpt_image_2_kie_user_cost(user_id, current_resolution)} ток.\nФормат: {current_aspect}\nМожешь отправить ещё фото или сразу написать prompt."
                await tg_send_message(
                    chat_id,
                    msg,
                    reply_markup=_gpt_image_2_kie_inline_kb("i2i", current_aspect, current_resolution, count_refs),
                )
                return {"ok": True}

            # ---- GPT Image 2.0: accept reference images sent as document, including iPhone HEIC/HEIF ----
            if st.get("mode") == "gpt_image_2_i2i":
                gi2 = st.get("gpt_image_2_i2i") or {}
                photo_ids = [str(item or "").strip() for item in (gi2.get("photo_file_ids") or []) if str(item or "").strip()]
                if not photo_ids and str(gi2.get("photo_file_id") or "").strip():
                    photo_ids = [str(gi2.get("photo_file_id") or "").strip()]
                photo_urls = [str(item or "").strip() for item in (gi2.get("photo_urls") or []) if str(item or "").strip()]
                if len(photo_ids) >= 4:
                    await tg_send_message(
                        chat_id,
                        "У GPT Image 2.0 можно использовать максимум 4 фото в этом режиме. Уже набрано 4/4 ✅ Теперь просто напиши, что нужно изменить.",
                        reply_markup=_gpt_image_2_aspect_inline_kb("i2i", str(gi2.get("aspect_ratio") or _gpt_image_2_aspect_for_size(gi2.get("size")) or "1:1")),
                    )
                    return {"ok": True}

                photo_ids.append(str(file_id))
                try:
                    ext, detected_mime = _detect_image_type(img_bytes)
                    safe_ext = (filename_l.rsplit(".", 1)[-1] if "." in filename_l else ext)
                    if safe_ext not in {"jpg", "jpeg", "png", "webp", "heic", "heif"}:
                        safe_ext = ext
                    input_path = f"gpt_image2_inputs/{int(user_id)}/{int(time.time())}_{uuid4().hex[:10]}_{len(photo_ids)}.{safe_ext}"
                    uploaded_url = upload_bytes_to_supabase(input_path, img_bytes, detected_mime)
                    if uploaded_url:
                        photo_urls.append(str(uploaded_url).strip())
                except Exception:
                    logging.exception("GPT Image 2.0 document input upload failed")

                gi2["photo_file_id"] = str(photo_ids[0]) if photo_ids else None
                gi2["photo_file_ids"] = photo_ids[:4]
                gi2["photo_urls"] = photo_urls[:4]
                gi2["aspect_ratio"] = str(gi2.get("aspect_ratio") or _gpt_image_2_aspect_for_size(gi2.get("size")) or "1:1")
                gi2["size"] = _gpt_image_2_size_for_aspect_ratio(gi2["aspect_ratio"])
                gi2["step"] = "need_prompt"
                st["gpt_image_2_i2i"] = gi2
                st["ts"] = _now()
                count_refs = len(gi2.get("photo_file_ids") or [])
                current_aspect = str(gi2.get("aspect_ratio") or "1:1")
                if count_refs >= 4:
                    msg = f"Файл-фото принято 4/4 ✅\nФормат результата: {current_aspect}\nТеперь напиши одним сообщением, что нужно изменить через GPT Image 2.0."
                else:
                    msg = f"Файл-фото принято {count_refs}/4 ✅\nФормат результата: {current_aspect}\nМожешь отправить ещё фото или сразу написать, что нужно изменить через GPT Image 2.0."
                await tg_send_message(
                    chat_id,
                    msg,
                    reply_markup=_gpt_image_2_aspect_inline_kb("i2i", current_aspect),
                )
                return {"ok": True}

        elif st.get("mode") in {"gpt_image_2_i2i", "gpt_image_2_kie_i2i", "nano_banana", "nano_banana_2", "nano_banana_pro", "nano_banana_pro_new", "topaz_photo", "kling_i2v", "two_photos", "photosession", "poster"}:
            if st.get("mode") == "gpt_image_2_kie_i2i":
                await tg_send_message(chat_id, f"Это не похоже на подходящее изображение. Для Gpt Image 2 пришли JPG/PNG/WebP до {KIE_GPT_IMAGE_2_MAX_INPUT_MB} МБ.", reply_markup=_main_menu_for(user_id))
            else:
                await tg_send_message(chat_id, "Это не похоже на изображение. Пришли JPG/PNG/WebP/HEIC/HEIF как фото или файл.", reply_markup=_main_menu_for(user_id))
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
                    f"Фото принял ✅\nТеперь напиши одним сообщением, что изменить (фон/стиль/детали).\n\n{_nano_banana_basic_cost_hint_for_user(user_id, 'nano_banana', '2K')}",
                    reply_markup=_photo_future_menu_keyboard(),
                )
                return {"ok": True}
                                
        # ---- NANO BANANA 2 (PiAPI): ждём фото ----
        if st.get("mode") == "nano_banana_2":
            nb2 = st.get("nano_banana_2") or {}
            step = (nb2.get("step") or "need_photo")
            if step == "need_photo":
                nb2["photo_bytes"] = img_bytes
                nb2["photo_file_id"] = file_id
                nb2["step"] = "need_prompt"
                st["nano_banana_2"] = nb2
                st["ts"] = _now()
                current_aspect = (nb2.get("aspect_ratio") or "9:16")
                await tg_send_message(
                    chat_id,
                    f"Фото принял ✅\nФормат: {current_aspect}\nТеперь напиши одним сообщением, что изменить (фон/стиль/детали).\n\n{_nano_banana_basic_cost_hint_for_user(user_id, 'nano_banana_2', '2K')}",
                    reply_markup=_photo_future_menu_keyboard(),
                )
                return {"ok": True}

        # ---- NANO BANANA PRO (PiAPI): ждём фото ----
        if st.get("mode") == "nano_banana_pro":
            nbp = st.get("nano_banana_pro") or {}
            step = (nbp.get("step") or "need_photo")
            if step == "need_photo":
                nbp["photo_bytes"] = img_bytes
                nbp["photo_file_id"] = file_id
                nbp["step"] = "need_prompt"
                st["nano_banana_pro"] = nbp
                st["ts"] = _now()
                await tg_send_message(
                    chat_id,
                    "Фото принял ✅\nТеперь напиши одним сообщением, что изменить (фон/стиль/детали).\n\nСтоимость: 2 токена.",
                    reply_markup=_photo_future_menu_keyboard(),
                )
                return {"ok": True}

        # ---- NANO BANANA PRO - NEW (KIE): ждём фото ----
        if st.get("mode") == "nano_banana_pro_new":
            nbpn = st.get("nano_banana_pro_new") or {}
            step = (nbpn.get("step") or "need_photo")
            if step == "need_photo":
                nbpn["photo_bytes"] = img_bytes
                nbpn["photo_file_id"] = file_id
                nbpn["step"] = "need_prompt"
                st["nano_banana_pro_new"] = nbpn
                st["ts"] = _now()
                current_resolution = str(nbpn.get("resolution") or "2K").upper() or "2K"
                current_cost = nano_banana_pro_new_cost(current_resolution)
                current_aspect = (nbpn.get("aspect_ratio") or "9:16")
                await tg_send_message(
                    chat_id,
                    f"Фото принял ✅\nResolution: {current_resolution}\nФормат: {current_aspect}\nТеперь напиши одним сообщением, что изменить (фон/стиль/детали).\n\nСтоимость: {current_cost} токен(а).",
                    reply_markup=_photo_future_menu_keyboard(),
                )
                return {"ok": True}

        if st.get("mode") == "topaz_photo":
            tpz = st.get("topaz_photo") or {}
            preset_slug = str(tpz.get("preset_slug") or "").strip().lower()
            if not preset_slug:
                await tg_send_message(
                    chat_id,
                    "Сначала выбери пресет для Topaz Фото.",
                    reply_markup=_topaz_photo_presets_keyboard(),
                )
                return {"ok": True}

            cost = int(get_photo_preset_tokens(preset_slug))
            ensure_user_row(user_id)
            try:
                bal = float(get_balance(user_id) or 0)
            except Exception:
                bal = 0

            if bal < cost:
                await tg_send_message(
                    chat_id,
                    f"Недостаточно токенов 😕\nНужно: {cost} токен(а) для Topaz Фото.",
                    reply_markup=_topup_packs_kb(),
                )
                return {"ok": True}

            job_id = uuid4().hex
            charged = False
            try:
                try:
                    add_tokens(user_id, -cost, reason="topaz_image_upscale")
                except TypeError:
                    add_tokens(user_id, -int(cost), reason="topaz_image_upscale")
                charged = True

                await tg_send_message(
                    chat_id,
                    f"🖼 Topaz Фото ({preset_slug}) — запускаю…",
                    reply_markup=_photo_future_menu_keyboard(),
                )

                await enqueue_job(
                    {
                        "job_id": job_id,
                        "type": "topaz_image_upscale",
                        "chat_id": int(chat_id),
                        "user_id": int(user_id),
                        "photo_file_id": str(file_id or ""),
                        "preset_slug": preset_slug,
                        "charge_tokens": int(cost),
                        "refund_reason": "topaz_image_upscale_refund",
                        "reply_markup": _photo_future_menu_keyboard(),
                    },
                    queue_name=TOPAZ_PHOTO_QUEUE_NAME,
                )
            except Exception as e:
                if charged:
                    try:
                        try:
                            add_tokens(user_id, int(cost), reason="topaz_image_upscale_refund")
                        except TypeError:
                            add_tokens(user_id, int(cost), reason="topaz_image_upscale_refund")
                    except Exception:
                        pass
                await tg_send_message(
                    chat_id,
                    f"❌ Не удалось запустить Topaz Фото: {e}\nТокены возвращены.",
                    reply_markup=_photo_future_menu_keyboard(),
                )
                return {"ok": True}

            st["topaz_photo"] = {"step": "choose_preset", "preset_slug": None}
            st["ts"] = _now()
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
                        "aspect_ratio": str(tp.get("aspect_ratio") or "9:16"),
                        "model": str(tp.get("model") or "seedream_45"),
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

            if st.get("mode") == "chat":
                if st.get("ai_chat_mode") != "chat":
                    await tg_send_message(
                        chat_id,
                        "Фото получил, но сначала выбери модель чата: Claude Sonnet, Claude Opus 4.7, Claude Fable 5 или ChatGPT.",
                        reply_markup=_ai_chat_mode_inline_kb(),
                    )
                    return {"ok": True}
                if await _enqueue_tg_fable_image_chat_or_notify(
                    chat_id=chat_id,
                    user_id=user_id,
                    st=st,
                    file_id=str(file_id or ""),
                    filename=filename or "telegram_image.jpg",
                    mime_type=mime or "image/jpeg",
                    size_bytes=int(doc.get("file_size") or 0),
                    prompt=incoming_text or "",
                ):
                    return {"ok": True}
                if not await _tg_consume_free_chat_or_notify(chat_id, user_id):
                    st["ts"] = _now()
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
            nav_text = (incoming_text or "").strip()
            if nav_text in ("⬅ Назад", "Назад") or nav_text.startswith("/"):
                pass
            elif nav_text in ("Фото будущего", "📸 Фото будущего", "Фото/Афиши", "Нейро фотосессии", "2 фото", "Картинка+Картинка", "Seedream", "Seedream 4.5", "Апскейл", "🍌 Nano Banana", "🍌 Nano Banana 2", "🍌 Nano Banana Pro", "🍌 Nano Banana Pro - NEW", "Текст→Картинка", "🖼 Апскейл фото", "🎬 Апскейл видео", "🧠 ИИ (чат)", "ИИ (чат)", "🧠 ИИ чат"):
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

                photo_file_id = str(nb.get("photo_file_id") or "").strip()
                if not photo_file_id:
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

                ensure_user_row(user_id)
                try:
                    bal = float(get_balance(user_id) or 0)
                except Exception:
                    bal = 0

                cost = 0.0 if _nano_banana_basic_is_included_for_user(user_id, "nano_banana", "2K") else 1.0
                if bal < cost:
                    await tg_send_message(
                        chat_id,
                        f"Недостаточно токенов 😕\nНужно: {cost} токен(а) для Nano Banana.",
                        reply_markup=_topup_packs_kb(),
                    )
                    return {"ok": True}

                charge_ref_id = uuid4().hex if cost > 0 else ""
                charged = False
                try:
                    if cost > 0:
                        try:
                            add_tokens(user_id, -cost, reason="nano_banana", ref_id=charge_ref_id)
                        except TypeError:
                            add_tokens(user_id, -int(cost), reason="nano_banana")
                        charged = True

                    await enqueue_job({
                        "job_id": uuid4().hex,
                        "type": "nano_banana",
                        "chat_id": int(chat_id),
                        "user_id": int(user_id),
                        "prompt": user_prompt,
                        "photo_file_id": photo_file_id,
                        "cost": int(cost),
                        "charge_ref_id": charge_ref_id,
                    }, queue_name=NANO_BANANA_QUEUE_NAME)
                except Exception as e:
                    if charged:
                        try:
                            try:
                                add_tokens(user_id, cost, reason="nano_banana_refund", ref_id=charge_ref_id)
                            except TypeError:
                                add_tokens(user_id, int(cost), reason="nano_banana_refund")
                        except Exception:
                            pass
                    refund_note = "\nТокены возвращены." if charged else ""
                    await tg_send_message(
                        chat_id,
                        f"❌ Не удалось поставить Nano Banana в очередь: {e}{refund_note}",
                        reply_markup=_photo_future_menu_keyboard(),
                    )
                    return {"ok": True}

                await tg_send_message(
                    chat_id,
                    "✅ Nano Banana: запрос принят. Пришлю результат, как будет готово.",
                    reply_markup=_photo_future_menu_keyboard(),
                )
                st["nano_banana"] = {"step": "need_photo", "photo_bytes": None, "photo_file_id": None}
                st["ts"] = _now()
                return {"ok": True}

        # NANO BANANA 2 (PiAPI): текст→картинка ИЛИ фото→фото
        if st.get("mode") == "nano_banana_2":
            nb2 = st.get("nano_banana_2") or {}
            step = (nb2.get("step") or "need_photo")

            nav_text = (incoming_text or "").strip()

            # -----------------------------
            # CASE 1: TEXT → IMAGE
            # -----------------------------
            if step != "need_prompt":

                if (not nav_text) or _is_nav_or_menu_text(nav_text):
                    await tg_send_message(
                        chat_id,
                        "🍌 Nano Banana 2:\n"
                        "• Пришли фото (для редактирования)\n"
                        "ИЛИ\n"
                        "• Пришли текст (для генерации картинки без фото).",
                        reply_markup=_photo_future_menu_keyboard(),
                    )
                    return {"ok": True}

                user_prompt = nav_text
                selected_resolution = str(nb2.get("resolution") or "2K").strip().upper() or "2K"
                cost = 1
                ensure_user_row(user_id)
                try:
                    bal = float(get_balance(user_id) or 0)
                except Exception:
                    bal = 0

                if bal < cost:
                    await tg_send_message(
                        chat_id,
                        f"Недостаточно токенов 😕\nНужно: {cost} токен(а) для Nano Banana 2.",
                        reply_markup=_topup_packs_kb(),
                    )
                    return {"ok": True}

                nano_banana_2_charged = False
                job_id = uuid4().hex
                try:
                    if cost > 0:
                        try:
                            add_tokens(user_id, -cost, reason="nano_banana_2")
                        except TypeError:
                            add_tokens(user_id, -int(cost), reason="nano_banana_2")
                        nano_banana_2_charged = True

                    await tg_send_message(
                        chat_id,
                        "🍌 Nano Banana 2 (текст→картинка) — запускаю…",
                        reply_markup=_photo_future_menu_keyboard(),
                    )

                    await enqueue_job({
                        "job_id": job_id,
                        "type": "nano_banana_2",
                        "chat_id": int(chat_id),
                        "user_id": int(user_id),
                        "prompt": user_prompt,
                        "photo_file_id": "",
                        "resolution": (nb2.get("resolution") or "2K"),
                        "aspect_ratio": (nb2.get("aspect_ratio") or "9:16"),
                        "output_format": "png",
                        "cost": cost,
                    }, queue_name="gen")
                except Exception as e:
                    if nano_banana_2_charged:
                        try:
                            try:
                                add_tokens(user_id, cost, reason="nano_banana_2_refund")
                            except TypeError:
                                add_tokens(user_id, int(cost), reason="nano_banana_2_refund")
                        except Exception:
                            pass
                    try:
                        refund_note = "\nТокены возвращены." if nano_banana_2_charged else ""
                        await tg_send_message(
                            chat_id,
                            f"❌ Не удалось запустить Nano Banana 2: {e}{refund_note}",
                            reply_markup=_photo_future_menu_keyboard(),
                        )
                    except Exception:
                        pass
                    return {"ok": True}

                st["nano_banana_2"] = {
                    "step": "need_photo",
                    "photo_bytes": None,
                    "resolution": (nb2.get("resolution") or "2K"),
                    "aspect_ratio": (nb2.get("aspect_ratio") or "9:16"),
                }
                st["ts"] = _now()
                return {"ok": True}

            # -----------------------------
            # CASE 2: IMAGE → IMAGE
            # -----------------------------
            src_bytes = nb2.get("photo_bytes")

            if not src_bytes:
                st["nano_banana_2"] = {
                    "step": "need_photo",
                    "photo_bytes": None,
                    "resolution": (nb2.get("resolution") or "2K"),
                    "aspect_ratio": (nb2.get("aspect_ratio") or "9:16"),
                }
                st["ts"] = _now()

                await tg_send_message(
                    chat_id,
                    "Фото не найдено. Пришли фото ещё раз.",
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

            selected_resolution = str(nb2.get("resolution") or "2K").strip().upper() or "2K"
            cost = 1
            ensure_user_row(user_id)
            try:
                bal = float(get_balance(user_id) or 0)
            except Exception:
                bal = 0

            if bal < cost:
                await tg_send_message(
                    chat_id,
                    f"Недостаточно токенов 😕\nНужно: {cost} токен(а) для Nano Banana 2.",
                    reply_markup=_topup_packs_kb(),
                )
                return {"ok": True}

            nano_banana_2_charged = False
            job_id = uuid4().hex
            try:
                if cost > 0:
                    try:
                        add_tokens(user_id, -cost, reason="nano_banana_2")
                    except TypeError:
                        add_tokens(user_id, -int(cost), reason="nano_banana_2")
                    nano_banana_2_charged = True

                await tg_send_message(
                    chat_id,
                    "🍌 Nano Banana 2 — запускаю…",
                )

                await enqueue_job({
                    "job_id": job_id,
                    "type": "nano_banana_2",
                    "chat_id": int(chat_id),
                    "user_id": int(user_id),
                    "prompt": user_prompt,
                    "photo_file_id": (nb2.get("photo_file_id") or ""),
                    "resolution": (nb2.get("resolution") or "2K"),
                    "aspect_ratio": (nb2.get("aspect_ratio") or "match_input_image"),
                    "output_format": "png",
                    "cost": cost,
                }, queue_name="gen")
            except Exception as e:
                if nano_banana_2_charged:
                    try:
                        try:
                            add_tokens(user_id, cost, reason="nano_banana_2_refund")
                        except TypeError:
                            add_tokens(user_id, int(cost), reason="nano_banana_2_refund")
                    except Exception:
                        pass
                try:
                    refund_note = "\nТокены возвращены." if nano_banana_2_charged else ""
                    await tg_send_message(
                        chat_id,
                        f"❌ Не удалось запустить Nano Banana 2: {e}{refund_note}",
                        reply_markup=_photo_future_menu_keyboard(),
                    )
                except Exception:
                    pass
                return {"ok": True}

            st["nano_banana_2"] = {
                "step": "need_photo",
                "photo_bytes": None,
                "resolution": (nb2.get("resolution") or "2K"),
                "aspect_ratio": (nb2.get("aspect_ratio") or "9:16"),
            }
            st["ts"] = _now()
            return {"ok": True}

        # NANO BANANA PRO (PiAPI): текст→картинка ИЛИ фото→фото
        if st.get("mode") == "nano_banana_pro":
            nbp = st.get("nano_banana_pro") or {}
            step = (nbp.get("step") or "need_photo")

            nav_text = (incoming_text or "").strip()

            # -----------------------------
            # CASE 1: TEXT → IMAGE
            # -----------------------------
            if step != "need_prompt":

                # если это кнопка/навигация — не считаем промптом
                if (not nav_text) or _is_nav_or_menu_text(nav_text):
                    await tg_send_message(
                        chat_id,
                        "🍌 Nano Banana Pro:\n"
                        "• Пришли фото (для редактирования)\n"
                        "ИЛИ\n"
                        "• Пришли текст (для генерации картинки без фото).",
                        reply_markup=_photo_future_menu_keyboard(),
                    )
                    return {"ok": True}

                user_prompt = nav_text
                cost = 2
                ensure_user_row(user_id)
                try:
                    bal = float(get_balance(user_id) or 0)
                except Exception:
                    bal = 0

                if bal < cost:
                    await tg_send_message(
                        chat_id,
                        f"Недостаточно токенов 😕\nНужно: {cost} токен(а) для Nano Banana Pro.",
                        reply_markup=_topup_packs_kb(),
                    )
                    return {"ok": True}

                nano_banana_pro_charged = False
                job_id = uuid4().hex
                try:
                    try:
                        add_tokens(user_id, -cost, reason="nano_banana_pro")
                    except TypeError:
                        add_tokens(user_id, -int(cost), reason="nano_banana_pro")
                    nano_banana_pro_charged = True

                    await tg_send_message(
                        chat_id,
                        "🍌 Nano Banana Pro (текст→картинка) — запускаю…",
                        reply_markup=_photo_future_menu_keyboard(),
                    )

                    await enqueue_job({
                        "job_id": job_id,
                        "type": "nano_banana_pro",
                        "chat_id": int(chat_id),
                        "user_id": int(user_id),
                        "prompt": user_prompt,
                        "photo_file_id": "",
                        "resolution": (nbp.get("resolution") or "2K"),
                        "aspect_ratio": (nbp.get("aspect_ratio") or "9:16"),
                        "safety_level": (nbp.get("safety_level") or "high"),
                        "output_format": "png",
                        "cost": cost,
                    }, queue_name="gen")
                except Exception as e:
                    # возврат токенов при ошибке после списания
                    if nano_banana_pro_charged:
                        try:
                            try:
                                add_tokens(user_id, cost, reason="nano_banana_pro_refund")
                            except TypeError:
                                add_tokens(user_id, int(cost), reason="nano_banana_pro_refund")
                        except Exception:
                            pass
                    try:
                        await tg_send_message(
                            chat_id,
                            f"❌ Не удалось запустить Nano Banana Pro: {e}\nТокены возвращены.",
                            reply_markup=_photo_future_menu_keyboard(),
                        )
                    except Exception:
                        pass
                    return {"ok": True}

                st["nano_banana_pro"] = {
                    "step": "need_photo",
                    "photo_bytes": None,
                    "resolution": (nbp.get("resolution") or "2K"),
                    "aspect_ratio": (nbp.get("aspect_ratio") or "9:16"),
                }
                st["ts"] = _now()
                return {"ok": True}

            # -----------------------------
            # CASE 2: IMAGE → IMAGE
            # -----------------------------
            src_bytes = nbp.get("photo_bytes")

            if not src_bytes:
                st["nano_banana_pro"] = {
                    "step": "need_photo",
                    "photo_bytes": None,
                    "resolution": (nbp.get("resolution") or "2K"),
                    "aspect_ratio": (nbp.get("aspect_ratio") or "9:16"),
                }
                st["ts"] = _now()

                await tg_send_message(
                    chat_id,
                    "Фото не найдено. Пришли фото ещё раз.",
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

            cost = 2
            ensure_user_row(user_id)
            try:
                bal = float(get_balance(user_id) or 0)
            except Exception:
                bal = 0

            if bal < cost:
                await tg_send_message(
                    chat_id,
                    f"Недостаточно токенов 😕\nНужно: {cost} токен(а) для Nano Banana Pro.",
                    reply_markup=_topup_packs_kb(),
                )
                return {"ok": True}

            nano_banana_pro_charged = False
            job_id = uuid4().hex
            try:
                try:
                    add_tokens(user_id, -cost, reason="nano_banana_pro")
                except TypeError:
                    add_tokens(user_id, -int(cost), reason="nano_banana_pro")
                nano_banana_pro_charged = True

                await tg_send_message(
                    chat_id,
                    "🍌 Nano Banana Pro — запускаю…",
                )

                await enqueue_job({
                    "job_id": job_id,
                    "type": "nano_banana_pro",
                    "chat_id": int(chat_id),
                    "user_id": int(user_id),
                    "prompt": user_prompt,
                    "photo_file_id": (nbp.get("photo_file_id") or ""),
                    "resolution": (nbp.get("resolution") or "2K"),
                    "aspect_ratio": (nbp.get("aspect_ratio") or "match_input_image"),
                    "safety_level": (nbp.get("safety_level") or "high"),
                    "output_format": "png",
                    "cost": cost,
                }, queue_name="gen")
            except Exception as e:
                # возврат токенов при ошибке после списания
                if nano_banana_pro_charged:
                    try:
                        try:
                            add_tokens(user_id, cost, reason="nano_banana_pro_refund")
                        except TypeError:
                            add_tokens(user_id, int(cost), reason="nano_banana_pro_refund")
                    except Exception:
                        pass
                try:
                    await tg_send_message(
                        chat_id,
                        f"❌ Не удалось запустить Nano Banana Pro: {e}\nТокены возвращены.",
                        reply_markup=_photo_future_menu_keyboard(),
                    )
                except Exception:
                    pass
                return {"ok": True}

            st["nano_banana_pro"] = {
                "step": "need_photo",
                "photo_bytes": None,
                "resolution": (nbp.get("resolution") or "2K"),
                "aspect_ratio": (nbp.get("aspect_ratio") or "9:16"),
            }
            st["ts"] = _now()

            return {"ok": True}

        # NANO BANANA PRO - NEW (KIE): текст→картинка ИЛИ фото→фото
        if st.get("mode") == "nano_banana_pro_new":
            nbpn = st.get("nano_banana_pro_new") or {}
            step = (nbpn.get("step") or "need_photo")

            nav_text = (incoming_text or "").strip()
            selected_resolution = str(nbpn.get("resolution") or "2K").upper() or "2K"
            cost = nano_banana_pro_new_cost(selected_resolution)
            photo_ids = [str(item or "").strip() for item in (nbpn.get("photo_file_ids") or []) if str(item or "").strip()]
            has_refs = bool(photo_ids or str(nbpn.get("photo_file_id") or "").strip() or nbpn.get("photo_bytes"))

            if step != "need_prompt" and not has_refs:
                if (not nav_text) or _is_nav_or_menu_text(nav_text):
                    await tg_send_message(
                        chat_id,
                        "🍌 Nano Banana Pro - NEW:\n"
                        "• Пришли фото (для редактирования)\n"
                        "ИЛИ\n"
                        "• Пришли текст (для генерации картинки без фото).\n\n"
                        f"Текущий resolution: {selected_resolution} • {cost} ток.",
                        reply_markup=_photo_future_menu_keyboard(),
                    )
                    return {"ok": True}

                user_prompt = nav_text
                ensure_user_row(user_id)
                try:
                    bal = float(get_balance(user_id) or 0)
                except Exception:
                    bal = 0

                if bal < cost:
                    await tg_send_message(
                        chat_id,
                        f"Недостаточно токенов 😕\nНужно: {cost} токен(а) для Nano Banana Pro - NEW ({selected_resolution}).",
                        reply_markup=_topup_packs_kb(),
                    )
                    return {"ok": True}

                nano_banana_pro_new_charged = False
                job_id = uuid4().hex
                try:
                    try:
                        add_tokens(user_id, -cost, reason="nano_banana_pro_new")
                    except TypeError:
                        add_tokens(user_id, -int(cost), reason="nano_banana_pro_new")
                    nano_banana_pro_new_charged = True

                    await tg_send_message(
                        chat_id,
                        f"🍌 Nano Banana Pro - NEW ({selected_resolution}, текст→картинка) — запускаю…",
                        reply_markup=_photo_future_menu_keyboard(),
                    )

                    await enqueue_job({
                        "job_id": job_id,
                        "type": "nano_banana_pro_new",
                        "chat_id": int(chat_id),
                        "user_id": int(user_id),
                        "prompt": user_prompt,
                        "photo_file_id": "",
                        "resolution": selected_resolution,
                        "aspect_ratio": (nbpn.get("aspect_ratio") or "9:16"),
                        "output_format": "jpg",
                        "cost": cost,
                    }, queue_name="gen")
                except Exception as e:
                    if nano_banana_pro_new_charged:
                        try:
                            try:
                                add_tokens(user_id, cost, reason="nano_banana_pro_new_refund")
                            except TypeError:
                                add_tokens(user_id, int(cost), reason="nano_banana_pro_new_refund")
                        except Exception:
                            pass
                    try:
                        await tg_send_message(
                            chat_id,
                            f"❌ Не удалось запустить Nano Banana Pro - NEW: {e}\nТокены возвращены.",
                            reply_markup=_photo_future_menu_keyboard(),
                        )
                    except Exception:
                        pass
                    return {"ok": True}

                st["nano_banana_pro_new"] = {
                    "step": "need_photo",
                    "photo_bytes": None,
                    "photo_file_id": None,
                    "photo_file_ids": [],
                    "resolution": selected_resolution,
                    "aspect_ratio": (nbpn.get("aspect_ratio") or "9:16"),
                }
                st["ts"] = _now()
                return {"ok": True}

            src_bytes = nbpn.get("photo_bytes")

            if not src_bytes:
                st["nano_banana_pro_new"] = {
                    "step": "need_photo",
                    "photo_bytes": None,
                    "photo_file_id": None,
                    "photo_file_ids": [],
                    "resolution": selected_resolution,
                    "aspect_ratio": (nbpn.get("aspect_ratio") or "9:16"),
                }
                st["ts"] = _now()

                await tg_send_message(
                    chat_id,
                    "Фото не найдено. Пришли фото ещё раз.",
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

            ensure_user_row(user_id)
            try:
                bal = float(get_balance(user_id) or 0)
            except Exception:
                bal = 0

            if bal < cost:
                await tg_send_message(
                    chat_id,
                    f"Недостаточно токенов 😕\nНужно: {cost} токен(а) для Nano Banana Pro - NEW ({selected_resolution}).",
                    reply_markup=_topup_packs_kb(),
                )
                return {"ok": True}

            nano_banana_pro_new_charged = False
            job_id = uuid4().hex
            try:
                try:
                    add_tokens(user_id, -cost, reason="nano_banana_pro_new")
                except TypeError:
                    add_tokens(user_id, -int(cost), reason="nano_banana_pro_new")
                nano_banana_pro_new_charged = True

                await tg_send_message(
                    chat_id,
                    f"🍌 Nano Banana Pro - NEW ({selected_resolution}) — запускаю…",
                )

                await enqueue_job({
                    "job_id": job_id,
                    "type": "nano_banana_pro_new",
                    "chat_id": int(chat_id),
                    "user_id": int(user_id),
                    "prompt": user_prompt,
                    "photo_file_id": (nbpn.get("photo_file_id") or ""),
                    "photo_file_ids": [str(item or "").strip() for item in (nbpn.get("photo_file_ids") or []) if str(item or "").strip()][:8],
                    "resolution": selected_resolution,
                    "aspect_ratio": (nbpn.get("aspect_ratio") or "match_input_image"),
                    "output_format": "jpg",
                    "cost": cost,
                }, queue_name="gen")
            except Exception as e:
                if nano_banana_pro_new_charged:
                    try:
                        try:
                            add_tokens(user_id, cost, reason="nano_banana_pro_new_refund")
                        except TypeError:
                            add_tokens(user_id, int(cost), reason="nano_banana_pro_new_refund")
                    except Exception:
                        pass
                try:
                    await tg_send_message(
                        chat_id,
                        f"❌ Не удалось запустить Nano Banana Pro - NEW: {e}\nТокены возвращены.",
                        reply_markup=_photo_future_menu_keyboard(),
                    )
                except Exception:
                    pass
                return {"ok": True}

            st["nano_banana_pro_new"] = {
                "step": "need_photo",
                "photo_bytes": None,
                "resolution": selected_resolution,
                "aspect_ratio": (nbpn.get("aspect_ratio") or "9:16"),
            }
            st["ts"] = _now()

            return {"ok": True}

        # TWO PHOTOS: после 2 фото — пользователь пишет инструкцию
        if st.get("mode") == "two_photos":
            tp = st.get("two_photos") or {}
            step = (tp.get("step") or "need_photo_1")
            if step != "need_prompt":
                await tg_send_message(chat_id, "В режиме «Картинка+Картинка» сначала пришли 2 фото подряд.", reply_markup=_main_menu_for(user_id))
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

            ensure_user_row(int(user_id))
            try:
                bal = int(get_balance(int(user_id)) or 0)
            except Exception:
                bal = 0

            aspect_ratio = str(tp.get("aspect_ratio") or "9:16")
            size = _seedream_size_for_aspect_ratio(aspect_ratio)
            seedream_model = _seedream_model_for_bot()

            cost_tokens = 1
            if bal < cost_tokens:
                await tg_send_message(
                    chat_id,
                    f"Недостаточно токенов 😕\nНужно: {cost_tokens} токен для режима «Картинка+Картинка».",
                    reply_markup=_topup_packs_kb(),
                )
                return {"ok": True}

            charge_ref_id = f"two_photos:{int(user_id)}:{uuid4().hex}"
            prompt = user_task
            try:
                add_tokens(
                    int(user_id),
                    -int(cost_tokens),
                    reason="two_photos",
                    ref_id=charge_ref_id,
                    meta={
                        "cost": int(cost_tokens),
                        "photo1_file_id": str(photo1_file_id),
                        "photo2_file_id": str(photo2_file_id),
                    },
                )
            except Exception as e:
                await tg_send_message(chat_id, f"❌ Не удалось списать токен: {e}", reply_markup=_topup_packs_kb())
                return {"ok": True}

            try:
                await enqueue_job({
                    "job_id": uuid4().hex,
                    "type": "two_photos",
                    "chat_id": int(chat_id),
                    "user_id": int(user_id),
                    "photo1_file_id": str(photo1_file_id),
                    "photo2_file_id": str(photo2_file_id),
                    "prompt": prompt,
                    "size": size,
                    "seedream_model": seedream_model,
                    "charge_tokens": int(cost_tokens),
                    "charge_ref_id": charge_ref_id,
                }, queue_name="gen")
            except Exception as e:
                try:
                    add_tokens(
                        int(user_id),
                        int(cost_tokens),
                        reason="two_photos_refund",
                        ref_id=charge_ref_id,
                        meta={"error": f"enqueue_failed: {str(e)[:300]}"},
                    )
                except Exception:
                    pass
                await tg_send_message(
                    chat_id,
                    f"❌ Не удалось поставить задачу «Картинка+Картинка» в очередь: {e}",
                    reply_markup=_photo_future_menu_keyboard(),
                )
                return {"ok": True}

            await tg_send_message(chat_id, f"⏳ Seedream 4.5: запускаю «Картинка+Картинка» ({aspect_ratio}). Как будет готово — пришлю результат.", reply_markup=_main_menu_for(user_id))

            st["two_photos"] = {
                "step": "need_photo_1",
                "photo1_bytes": None,
                "photo1_file_id": None,
                "photo2_bytes": None,
                "photo2_file_id": None,
                "aspect_ratio": aspect_ratio,
                "model": "seedream_45",
            }
            st["ts"] = _now()
            return {"ok": True}

        if st.get("mode") == "seedream_single":
            sd = st.get("seedream_single") or {}
            step = (sd.get("step") or "need_photo")
            photo_file_id = str(sd.get("photo_file_id") or "").strip()

            if step != "need_prompt" or not photo_file_id:
                await tg_send_message(
                    chat_id,
                    "Сначала пришли фото для Seedream 4.5.",
                    reply_markup=_photo_future_menu_keyboard(),
                )
                return {"ok": True}

            user_task = incoming_text.strip()
            if not user_task:
                await tg_send_message(
                    chat_id,
                    "Напиши одним сообщением, что сделать с фото.",
                    reply_markup=_photo_future_menu_keyboard(),
                )
                return {"ok": True}

            ensure_user_row(int(user_id))
            try:
                bal = int(get_balance(int(user_id)) or 0)
            except Exception:
                bal = 0

            aspect_ratio = str(sd.get("aspect_ratio") or "9:16")
            size = _seedream_size_for_aspect_ratio(aspect_ratio)
            seedream_model = _seedream_model_for_bot()

            cost_tokens = 1
            if bal < cost_tokens:
                await tg_send_message(
                    chat_id,
                    f"Недостаточно токенов 😕\nНужно: {cost_tokens} токен для Seedream 4.5.",
                    reply_markup=_topup_packs_kb(),
                )
                return {"ok": True}

            charge_ref_id = f"seedream_45_single:{int(user_id)}:{uuid4().hex}"
            try:
                add_tokens(
                    int(user_id),
                    -int(cost_tokens),
                    reason="seedream_45_single",
                    ref_id=charge_ref_id,
                    meta={
                        "cost": int(cost_tokens),
                        "photo_file_id": str(photo_file_id),
                    },
                )
            except Exception as e:
                await tg_send_message(chat_id, f"❌ Не удалось списать токен: {e}", reply_markup=_topup_packs_kb())
                return {"ok": True}

            try:
                await enqueue_job({
                    "job_id": uuid4().hex,
                    "type": "seedream_45_single",
                    "chat_id": int(chat_id),
                    "user_id": int(user_id),
                    "photo_file_id": str(photo_file_id),
                    "prompt": user_task,
                    "size": size,
                    "seedream_model": seedream_model,
                    "charge_tokens": int(cost_tokens),
                    "charge_ref_id": charge_ref_id,
                }, queue_name="gen")
            except Exception as e:
                try:
                    add_tokens(
                        int(user_id),
                        int(cost_tokens),
                        reason="seedream_45_single_refund",
                        ref_id=charge_ref_id,
                        meta={"error": f"enqueue_failed: {str(e)[:300]}"},
                    )
                except Exception:
                    pass
                await tg_send_message(
                    chat_id,
                    f"❌ Не удалось поставить Seedream 4.5 в очередь: {e}",
                    reply_markup=_photo_future_menu_keyboard(),
                )
                return {"ok": True}

            await tg_send_message(
                chat_id,
                f"⏳ Seedream 4.5: запускаю режим «1 фото + промпт» ({aspect_ratio}). Как будет готово — пришлю результат.",
                reply_markup=_main_menu_for(user_id),
            )

            st["seedream_single"] = {
                "step": "need_photo",
                "photo_file_id": None,
                "aspect_ratio": aspect_ratio,
                "model": "seedream_45",
            }
            st["ts"] = _now()
            return {"ok": True}

        # ---- KLING 2.5 Text → Video: запуск по тексту ----
        if st.get("mode") == "kling_t2v":
            kt = st.get("kling_t2v") or {}
            step = (kt.get("step") or "need_prompt")

            if step != "need_prompt":
                st["kling_t2v"] = {"step": "need_prompt"}

            user_prompt = incoming_text.strip()
            if user_prompt.lower() in ("старт", "start", "go"):
                user_prompt = "Cinematic realistic video, subtle natural motion, high detail, natural lighting."

            ks = st.get("kling_settings") or {}
            duration = int((ks.get("duration") or kt.get("duration") or 5))
            aspect_ratio = str(ks.get("aspect_ratio") or kt.get("aspect_ratio") or "16:9")
            model_slug = str(ks.get("model_slug") or "kwaivgi/kling-v2.5-turbo-pro")
            product = str(ks.get("product") or "kling_2_5_turbo_pro")

            await tg_send_message(chat_id, f"🎬 Генерирую Kling 2.5 Turbo Pro ({duration} сек, {aspect_ratio})…", reply_markup=_main_menu_for(user_id))

            _busy_start(int(user_id), "Kling 2.5 T2V")

            try:
                out_url = await run_text_to_video_from_prompt(
                    user_id=user_id,
                    prompt=user_prompt,
                    duration_seconds=duration,
                    aspect_ratio=aspect_ratio,
                    model_slug=model_slug,
                    product=product,
                    billing_meta={"flow": "t2v", "kling_version": "2_5", "model": "kling-v2.5-turbo-pro"},
                )
                await tg_send_message(chat_id, f"✅ Готово!\n{out_url}", reply_markup=_main_menu_for(user_id))
            except Exception as e:
                await tg_send_message(chat_id, f"❌ Ошибка Kling 2.5 Text → Video: {e}", reply_markup=_main_menu_for(user_id))
            finally:
                st["kling_t2v"] = {"step": "need_prompt", "duration": duration, "aspect_ratio": aspect_ratio}
                _set_mode(chat_id, user_id, "chat")
                _busy_end(int(user_id))

            return {"ok": True}

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
            kling_version = (ks.get("kling_version") or "1_6").lower()
            quality = (ks.get("quality") or "std").lower()
            duration = int((ks.get("duration") or ki.get("duration") or 5))
            kling_mode = "pro" if quality in ("pro", "professional") else "std"

            if kling_version == "2_5":
                model_label = "Kling 2.5 Turbo Pro"
                aspect_ratio = str(ks.get("aspect_ratio") or "16:9")
                model_slug = str(ks.get("model_slug") or "kwaivgi/kling-v2.5-turbo-pro")
                product = str(ks.get("product") or "kling_2_5_turbo_pro")
                await tg_send_message(chat_id, f"🎬 Генерирую {model_label} ({duration} сек)…", reply_markup=_main_menu_for(user_id))
            else:
                model_label = f"Kling Image → Video {kling_mode.upper()}"
                aspect_ratio = str(ks.get("aspect_ratio") or "16:9")
                model_slug = None
                product = None
                await tg_send_message(chat_id, f"🎬 Генерирую видео ({duration} сек, {kling_mode.upper()})…", reply_markup=_main_menu_for(user_id))

            _busy_start(int(user_id), "Kling I2V")

            try:
                out_url = await run_image_to_video_from_bytes(
                    user_id=user_id,
                    start_image_bytes=start_image_bytes,
                    prompt=user_prompt,
                    duration_seconds=duration,
                    mode=("pro" if kling_version == "2_5" else kling_mode),
                    aspect_ratio=aspect_ratio,
                    model_slug=model_slug,
                    product=product,
                    billing_meta={"flow": "i2v", "kling_version": kling_version, "model": model_label},
                )
                await tg_send_message(chat_id, f"✅ Готово!\n{out_url}", reply_markup=_main_menu_for(user_id))
            except Exception as e:
                await tg_send_message(chat_id, f"❌ Ошибка {model_label}: {e}", reply_markup=_main_menu_for(user_id))
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

            await tg_send_message(chat_id, "🎬 Генерирую видео (обычно 5–20 минут)…", reply_markup=_main_menu_for(user_id))

            _busy_start(int(user_id), "Kling Motion")

            try:
                # настройки Motion Control из WebApp
                ks = st.get("kling_settings") or {}
                quality = (ks.get("quality") or "std").lower()
                kling_mode = "pro" if quality in ("pro", "professional", "1080", "1080p") else "std"
                flow = str(ks.get("flow") or "motion").lower().strip()
                video_duration = km.get("video_duration")

                if flow == "motion_3_0":
                    resolution = normalize_kling3_motion_resolution(
                        ks.get("resolution") or ("1080p" if kling_mode == "pro" else "720p")
                    )
                    out_url = await run_kling3_motion_kie_from_bytes(
                        user_id=user_id,
                        avatar_bytes=avatar_bytes,
                        motion_video_bytes=video_bytes,
                        prompt=user_prompt or "A person performs the same motion as in the reference video.",
                        resolution=resolution,
                        character_orientation="video",
                        duration_seconds=video_duration,
                        bill_user=True,
                        billing_meta={"origin": "telegram", "ui_flow": "motion_control_3_0"},
                    )
                else:
                    out_url = await run_motion_control_from_bytes(
                        user_id=user_id,
                        avatar_bytes=avatar_bytes,
                        motion_video_bytes=video_bytes,
                        prompt=user_prompt or "A person performs the same motion as in the reference video.",
                        mode=kling_mode,
                        character_orientation="video",
                        keep_original_sound=True,
                        duration_seconds=video_duration,
                    )
                await tg_send_message(chat_id, f"✅ Готово!\n{out_url}", reply_markup=_main_menu_for(user_id))
            except Exception as e:
                await tg_send_message(chat_id, f"❌ Ошибка Kling Motion Control: {e}", reply_markup=_main_menu_for(user_id))
            finally:
                st["kling_mc"] = {"step": "need_avatar", "avatar_bytes": None, "video_bytes": None, "video_duration": None}
                _set_mode(chat_id, user_id, "chat")
                _busy_end(int(user_id))

            return {"ok": True}


        # Midjourney: prompt + custom numeric settings
        if st.get("mode") == "midjourney":
            mj = _midjourney_state(st)
            step = str(mj.get("step") or "need_prompt")
            if _is_nav_or_menu_text(incoming_text):
                mj["step"] = "need_prompt"
                st["midjourney"] = mj
                st["ts"] = _now()
                await tg_send_message(chat_id, "Ок. Открыл меню фото.", reply_markup=_photo_future_menu_keyboard())
                return {"ok": True}

            if step == "need_custom_stylize":
                try:
                    value = int(incoming_text.strip())
                except Exception:
                    await tg_send_message(chat_id, "Введи число от 0 до 1000.")
                    return {"ok": True}
                mj["stylize"] = max(0, min(1000, value))
                mj["step"] = "need_prompt"
                st["midjourney"] = mj
                st["ts"] = _now()
                await tg_send_message(chat_id, _midjourney_settings_text(mj, user_id=user_id), reply_markup=_midjourney_settings_kb(mj, user_id=user_id))
                return {"ok": True}

            if step == "need_custom_chaos":
                try:
                    value = int(incoming_text.strip())
                except Exception:
                    await tg_send_message(chat_id, "Введи число от 0 до 100.")
                    return {"ok": True}
                mj["chaos"] = max(0, min(100, value))
                mj["step"] = "need_prompt"
                st["midjourney"] = mj
                st["ts"] = _now()
                await tg_send_message(chat_id, _midjourney_settings_text(mj, user_id=user_id), reply_markup=_midjourney_settings_kb(mj, user_id=user_id))
                return {"ok": True}

            prompt = incoming_text.strip()
            if not prompt:
                await tg_send_message(chat_id, "Пришли prompt для Midjourney.", reply_markup=_midjourney_settings_kb(mj, user_id=user_id))
                return {"ok": True}
            mj["prompt"] = prompt
            mj["step"] = "need_prompt"
            st["midjourney"] = mj
            st["ts"] = _now()
            await tg_send_message(chat_id, _midjourney_settings_text(mj, user_id=user_id), reply_markup=_midjourney_settings_kb(mj, user_id=user_id))
            return {"ok": True}


        # Legacy official GPT Image 2.0 states are now routed to the KIE provider.
        if st.get("mode") == "gpt_image_2_t2i":
            gi2_old = st.get("gpt_image_2_t2i") or {}
            legacy_aspect = gi2_old.get("aspect_ratio") or _gpt_image_2_aspect_for_size(gi2_old.get("size")) or "16:9"
            resolution, aspect_ratio = _gpt_image_2_kie_options("2K", _legacy_gpt_image_2_aspect_to_kie(legacy_aspect))
            st["mode"] = "gpt_image_2_kie_t2i"
            st["gpt_image_2_kie_t2i"] = {"step": "need_prompt", "aspect_ratio": aspect_ratio, "resolution": resolution}
            st.pop("gpt_image_2_t2i", None)
            st["ts"] = _now()
        elif st.get("mode") == "gpt_image_2_i2i":
            gi2_old = st.get("gpt_image_2_i2i") or {}
            photo_file_ids = [str(item or "").strip() for item in (gi2_old.get("photo_file_ids") or []) if str(item or "").strip()]
            if not photo_file_ids and str(gi2_old.get("photo_file_id") or "").strip():
                photo_file_ids = [str(gi2_old.get("photo_file_id") or "").strip()]
            photo_urls = [str(item or "").strip() for item in (gi2_old.get("photo_urls") or []) if str(item or "").strip()]
            legacy_aspect = gi2_old.get("aspect_ratio") or _gpt_image_2_aspect_for_size(gi2_old.get("size")) or "16:9"
            resolution, aspect_ratio = _gpt_image_2_kie_options("2K", _legacy_gpt_image_2_aspect_to_kie(legacy_aspect))
            st["mode"] = "gpt_image_2_kie_i2i"
            st["gpt_image_2_kie_i2i"] = {
                "step": "need_prompt" if (photo_file_ids or photo_urls) else "need_image",
                "photo_file_id": photo_file_ids[0] if photo_file_ids else None,
                "photo_file_ids": photo_file_ids[:16],
                "photo_urls": photo_urls[:16],
                "aspect_ratio": aspect_ratio,
                "resolution": resolution,
            }
            st.pop("gpt_image_2_i2i", None)
            st["ts"] = _now()

        # Gpt Image 2: text-to-image through workspace image worker
        if st.get("mode") == "gpt_image_2_kie_t2i":
            gi2k = st.get("gpt_image_2_kie_t2i") or {}
            if (gi2k.get("step") or "need_prompt") != "need_prompt":
                gi2k["step"] = "need_prompt"

            user_prompt = incoming_text.strip()
            if not user_prompt:
                await tg_send_message(chat_id, "Напиши описание для генерации.", reply_markup=_main_menu_for(user_id))
                return {"ok": True}

            resolution, aspect_ratio = _gpt_image_2_kie_options(gi2k.get("resolution") or "2K", gi2k.get("aspect_ratio") or "16:9")
            cost_tokens = _gpt_image_2_kie_user_cost(user_id, resolution)
            try:
                ensure_user_row(user_id)
                bal = int(get_balance(user_id) or 0)
            except Exception:
                bal = 0
            if bal < cost_tokens:
                await tg_send_message(
                    chat_id,
                    f"❌ Недостаточно токенов для Gpt Image 2. Нужно: {cost_tokens}, баланс: {bal}",
                    reply_markup=_topup_packs_kb(),
                )
                return {"ok": True}

            charge_ref_id = uuid4().hex if cost_tokens > 0 else ""
            charged = False
            try:
                if cost_tokens > 0:
                    add_tokens(
                        user_id,
                        -cost_tokens,
                        reason="gpt_image_2",
                        ref_id=charge_ref_id,
                        meta={"mode": "text_to_image", "provider": "gpt_image_2_kie", "resolution": resolution, "aspect_ratio": aspect_ratio, "cost_tokens": cost_tokens},
                    )
                    charged = True
                await enqueue_job({
                    "job_id": uuid4().hex,
                    "kind": "telegram_gpt_image_2_kie_run",
                    "type": "gpt_image_2_kie_t2i",
                    "chat_id": int(chat_id),
                    "user_id": int(user_id),
                    "prompt": user_prompt,
                    "mode": "text_to_image",
                    "aspect_ratio": aspect_ratio,
                    "resolution": resolution,
                    "charge_tokens": cost_tokens,
                    "charge_ref_id": charge_ref_id,
                    "refund_reason": "gpt_image_2_refund",
                }, queue_name=WORKSPACE_IMAGE_QUEUE_NAME)
            except Exception as e:
                if charged:
                    try:
                        add_tokens(user_id, cost_tokens, reason="gpt_image_2_refund", ref_id=charge_ref_id, meta={"stage": "enqueue_failed", "provider": "gpt_image_2_kie", "error": str(e)[:300]})
                    except Exception:
                        pass
                await tg_send_message(chat_id, f"❌ Не удалось поставить Gpt Image 2 в очередь: {e}", reply_markup=_main_menu_for(user_id))
                return {"ok": True}

            await tg_send_message(
                chat_id,
                f"✅ Gpt Image 2: запрос принят. Списано {cost_tokens} токен. Пришлю результат, как будет готово.",
                reply_markup=_main_menu_for(user_id),
            )
            st["gpt_image_2_kie_t2i"] = {"step": "need_prompt", "aspect_ratio": aspect_ratio, "resolution": resolution}
            st["ts"] = _now()
            return {"ok": True}

        # Gpt Image 2: image-to-image through workspace image worker
        if st.get("mode") == "gpt_image_2_kie_i2i":
            gi2k = st.get("gpt_image_2_kie_i2i") or {}
            step = (gi2k.get("step") or "need_image")
            photo_file_ids = [str(item or "").strip() for item in (gi2k.get("photo_file_ids") or []) if str(item or "").strip()]
            if not photo_file_ids and str(gi2k.get("photo_file_id") or "").strip():
                photo_file_ids = [str(gi2k.get("photo_file_id") or "").strip()]
            photo_urls = [str(item or "").strip() for item in (gi2k.get("photo_urls") or []) if str(item or "").strip()]

            if step == "need_image" or not (photo_file_ids or photo_urls):
                await tg_send_message(chat_id, "Сначала пришли от 1 до 16 фото для Gpt Image 2 → Картинка→Картинка.", reply_markup=_gpt_image_2_kie_inline_kb("i2i", str(gi2k.get("aspect_ratio") or "16:9"), str(gi2k.get("resolution") or "2K"), len(photo_file_ids)))
                return {"ok": True}

            user_prompt = incoming_text.strip()
            if not user_prompt:
                await tg_send_message(chat_id, "Напиши, что нужно изменить на фото.", reply_markup=_main_menu_for(user_id))
                return {"ok": True}

            resolution, aspect_ratio = _gpt_image_2_kie_options(gi2k.get("resolution") or "2K", gi2k.get("aspect_ratio") or "16:9")
            cost_tokens = _gpt_image_2_kie_user_cost(user_id, resolution)
            try:
                ensure_user_row(user_id)
                bal = int(get_balance(user_id) or 0)
            except Exception:
                bal = 0
            if bal < cost_tokens:
                await tg_send_message(
                    chat_id,
                    f"❌ Недостаточно токенов для Gpt Image 2. Нужно: {cost_tokens}, баланс: {bal}",
                    reply_markup=_topup_packs_kb(),
                )
                return {"ok": True}

            charge_ref_id = uuid4().hex if cost_tokens > 0 else ""
            charged = False
            try:
                if cost_tokens > 0:
                    add_tokens(
                        user_id,
                        -cost_tokens,
                        reason="gpt_image_2",
                        ref_id=charge_ref_id,
                        meta={"mode": "image_to_image", "provider": "gpt_image_2_kie", "resolution": resolution, "aspect_ratio": aspect_ratio, "refs": len(photo_file_ids[:16] or photo_urls[:16]), "cost_tokens": cost_tokens},
                    )
                    charged = True
                await enqueue_job({
                    "job_id": uuid4().hex,
                    "kind": "telegram_gpt_image_2_kie_run",
                    "type": "gpt_image_2_kie_i2i",
                    "chat_id": int(chat_id),
                    "user_id": int(user_id),
                    "prompt": user_prompt,
                    "mode": "image_to_image",
                    "aspect_ratio": aspect_ratio,
                    "resolution": resolution,
                    "photo_file_id": photo_file_ids[0] if photo_file_ids else None,
                    "photo_file_ids": photo_file_ids[:16],
                    "photo_urls": photo_urls[:16],
                    "charge_tokens": cost_tokens,
                    "charge_ref_id": charge_ref_id,
                    "refund_reason": "gpt_image_2_refund",
                }, queue_name=WORKSPACE_IMAGE_QUEUE_NAME)
            except Exception as e:
                if charged:
                    try:
                        add_tokens(user_id, cost_tokens, reason="gpt_image_2_refund", ref_id=charge_ref_id, meta={"stage": "enqueue_failed", "provider": "gpt_image_2_kie", "error": str(e)[:300]})
                    except Exception:
                        pass
                await tg_send_message(chat_id, f"❌ Не удалось поставить Gpt Image 2 в очередь: {e}", reply_markup=_main_menu_for(user_id))
                return {"ok": True}

            await tg_send_message(
                chat_id,
                f"✅ Gpt Image 2: запрос принят. Списано {cost_tokens} токен. Пришлю результат, как будет готово.",
                reply_markup=_main_menu_for(user_id),
            )
            st["gpt_image_2_kie_i2i"] = {
                "step": "need_image",
                "photo_file_id": None,
                "photo_file_ids": [],
                "photo_urls": [],
                "aspect_ratio": aspect_ratio,
                "resolution": resolution,
            }
            st["ts"] = _now()
            return {"ok": True}

        # GPT Image 2.0: text-to-image
        if st.get("mode") == "gpt_image_2_t2i":
            gi2 = st.get("gpt_image_2_t2i") or {}
            if (gi2.get("step") or "need_prompt") != "need_prompt":
                st["gpt_image_2_t2i"] = {"step": "need_prompt", "size": str(gi2.get("size") or "1024x1024")}

            user_prompt = incoming_text.strip()
            if not user_prompt:
                await tg_send_message(chat_id, "Напиши описание для генерации.", reply_markup=_main_menu_for(user_id))
                return {"ok": True}

            cost_tokens = int(GPT_IMAGE2_GENERATION_COST)
            try:
                ensure_user_row(user_id)
                bal = int(get_balance(user_id) or 0)
            except Exception:
                bal = 0
            if bal < cost_tokens:
                await tg_send_message(
                    chat_id,
                    f"❌ Недостаточно токенов для GPT Image 2.0. Нужно: {cost_tokens}, баланс: {bal}",
                    reply_markup=_topup_packs_kb(),
                )
                return {"ok": True}

            charge_ref_id = uuid4().hex
            charged = False
            try:
                aspect_ratio = str(gi2.get("aspect_ratio") or _gpt_image_2_aspect_for_size(gi2.get("size")) or "1:1")
                size = _gpt_image_2_size_for_aspect_ratio(aspect_ratio)
                add_tokens(
                    user_id,
                    -cost_tokens,
                    reason="gpt_image_2",
                    ref_id=charge_ref_id,
                    meta={"mode": "text_to_image", "cost_tokens": cost_tokens},
                )
                charged = True
                await enqueue_job({
                    "job_id": uuid4().hex,
                    "type": "gpt_image_2_t2i",
                    "chat_id": int(chat_id),
                    "user_id": int(user_id),
                    "prompt": user_prompt,
                    "aspect_ratio": aspect_ratio,
                    "size": size,
                    "charge_tokens": cost_tokens,
                    "charge_ref_id": charge_ref_id,
                    "refund_reason": "gpt_image_2_refund",
                }, queue_name=GPT_IMAGE2_QUEUE_NAME)
            except Exception as e:
                if charged:
                    try:
                        add_tokens(user_id, cost_tokens, reason="gpt_image_2_refund", ref_id=charge_ref_id, meta={"stage": "enqueue_failed", "error": str(e)[:300]})
                    except Exception:
                        pass
                await tg_send_message(chat_id, f"❌ Не удалось поставить GPT Image 2.0 в очередь: {e}", reply_markup=_main_menu_for(user_id))
                return {"ok": True}

            await tg_send_message(
                chat_id,
                f"✅ GPT Image 2.0: запрос принят. Списано {cost_tokens} токен. Пришлю результат, как будет готово.",
                reply_markup=_main_menu_for(user_id),
            )
            st["gpt_image_2_t2i"] = {"step": "need_prompt", "aspect_ratio": aspect_ratio, "size": size}
            st["ts"] = _now()
            return {"ok": True}
        # GPT Image 2.0: image-to-image
        if st.get("mode") == "gpt_image_2_i2i":
            gi2 = st.get("gpt_image_2_i2i") or {}
            step = (gi2.get("step") or "need_image")
            photo_file_ids = [str(item or "").strip() for item in (gi2.get("photo_file_ids") or []) if str(item or "").strip()]
            if not photo_file_ids and str(gi2.get("photo_file_id") or "").strip():
                photo_file_ids = [str(gi2.get("photo_file_id") or "").strip()]
            photo_urls = [str(item or "").strip() for item in (gi2.get("photo_urls") or []) if str(item or "").strip()]

            if step == "need_image" or not photo_file_ids:
                await tg_send_message(chat_id, "Сначала пришли от 1 до 4 фото для GPT Image 2.0 → Картинка→Картинка.", reply_markup=_main_menu_for(user_id))
                return {"ok": True}

            user_prompt = incoming_text.strip()
            if not user_prompt:
                await tg_send_message(chat_id, "Напиши, что нужно изменить на фото.", reply_markup=_main_menu_for(user_id))
                return {"ok": True}

            cost_tokens = int(GPT_IMAGE2_GENERATION_COST)
            try:
                ensure_user_row(user_id)
                bal = int(get_balance(user_id) or 0)
            except Exception:
                bal = 0
            if bal < cost_tokens:
                await tg_send_message(
                    chat_id,
                    f"❌ Недостаточно токенов для GPT Image 2.0. Нужно: {cost_tokens}, баланс: {bal}",
                    reply_markup=_topup_packs_kb(),
                )
                return {"ok": True}

            charge_ref_id = uuid4().hex
            charged = False
            try:
                aspect_ratio = str(gi2.get("aspect_ratio") or _gpt_image_2_aspect_for_size(gi2.get("size")) or "1:1")
                size = _gpt_image_2_size_for_aspect_ratio(aspect_ratio)
                add_tokens(
                    user_id,
                    -cost_tokens,
                    reason="gpt_image_2",
                    ref_id=charge_ref_id,
                    meta={"mode": "image_to_image", "cost_tokens": cost_tokens, "refs": len(photo_file_ids[:4] or photo_urls[:4])},
                )
                charged = True
                await enqueue_job({
                    "job_id": uuid4().hex,
                    "type": "gpt_image_2_i2i",
                    "chat_id": int(chat_id),
                    "user_id": int(user_id),
                    "prompt": user_prompt,
                    "aspect_ratio": aspect_ratio,
                    "size": size,
                    "photo_file_id": photo_file_ids[0],
                    "photo_file_ids": photo_file_ids[:4],
                    "photo_urls": photo_urls[:4],
                    "charge_tokens": cost_tokens,
                    "charge_ref_id": charge_ref_id,
                    "refund_reason": "gpt_image_2_refund",
                }, queue_name=GPT_IMAGE2_QUEUE_NAME)
            except Exception as e:
                if charged:
                    try:
                        add_tokens(user_id, cost_tokens, reason="gpt_image_2_refund", ref_id=charge_ref_id, meta={"stage": "enqueue_failed", "error": str(e)[:300]})
                    except Exception:
                        pass
                await tg_send_message(chat_id, f"❌ Не удалось поставить GPT Image 2.0 в очередь: {e}", reply_markup=_main_menu_for(user_id))
                return {"ok": True}

            await tg_send_message(
                chat_id,
                f"✅ GPT Image 2.0: запрос принят. Списано {cost_tokens} токен. Пришлю результат, как будет готово.",
                reply_markup=_main_menu_for(user_id),
            )
            st["gpt_image_2_i2i"] = {
                "step": "need_image",
                "photo_file_id": None,
                "photo_file_ids": [],
                "photo_urls": [],
                "aspect_ratio": aspect_ratio,
                "size": size,
            }
            st["ts"] = _now()
            return {"ok": True}

        # T2I flow: генерация Seedream по одному тексту (без входного фото)
        if st.get("mode") == "t2i":
            t2i = st.get("t2i") or {}
            step = (t2i.get("step") or "need_prompt")
            if step != "need_prompt":
                st["t2i"] = {"step": "need_prompt", "aspect_ratio": str(t2i.get("aspect_ratio") or "9:16"), "model": "seedream_45"}

            user_prompt = incoming_text.strip()
            if not user_prompt:
                await tg_send_message(chat_id, "Напиши описание для генерации (без фото).", reply_markup=_main_menu_for(user_id))
                return {"ok": True}

            aspect_ratio = str(t2i.get("aspect_ratio") or "9:16")
            model = _seedream_model_for_bot()
            size = _seedream_size_for_aspect_ratio(aspect_ratio)
            seedream_included = _seedream_t2i_is_included_for_user(int(user_id))
            cost_tokens = 0 if seedream_included else 1
            charge_ref_id = ""
            charged = False

            if cost_tokens > 0:
                ensure_user_row(int(user_id))
                try:
                    bal = int(get_balance(int(user_id)) or 0)
                except Exception:
                    bal = 0
                if bal < cost_tokens:
                    await tg_send_message(
                        chat_id,
                        f"Недостаточно токенов 😕\nНужно: {cost_tokens} токен для Seedream 4.5 Text-to-Image. На Spark/Pulse/Nexus этот режим бесплатный.",
                        reply_markup=_topup_packs_kb(),
                    )
                    return {"ok": True}
                charge_ref_id = str(uuid4())
                try:
                    add_tokens(
                        int(user_id),
                        -int(cost_tokens),
                        reason="seedream_t2i",
                        ref_id=charge_ref_id,
                        meta={"cost": int(cost_tokens), "aspect_ratio": aspect_ratio, "model": model},
                    )
                    charged = True
                except Exception as e:
                    await tg_send_message(chat_id, f"❌ Не удалось списать токен: {e}", reply_markup=_topup_packs_kb())
                    return {"ok": True}

            try:
                await enqueue_job({
                    "job_id": uuid4().hex,
                    "type": "seedream_t2i",
                    "chat_id": int(chat_id),
                    "user_id": int(user_id),
                    "prompt": user_prompt,
                    "size": size,
                    "seedream_model": model,
                    "aspect_ratio": aspect_ratio,
                    "charge_tokens": int(cost_tokens if charged else 0),
                    "charge_ref_id": charge_ref_id,
                }, queue_name=SEEDREAM_T2I_QUEUE_NAME)
            except Exception as e:
                if charged:
                    try:
                        add_tokens(int(user_id), int(cost_tokens), reason="seedream_t2i_refund", ref_id=charge_ref_id, meta={"stage": "enqueue_failed", "error": str(e)[:300]})
                    except Exception:
                        pass
                await tg_send_message(chat_id, f"❌ Не удалось поставить Seedream в очередь: {e}", reply_markup=_main_menu_for(user_id))
                return {"ok": True}

            await tg_send_message(
                chat_id,
                f"✅ Seedream: запрос принят ({aspect_ratio}). Пришлю результат, как будет готово." + ("\nБесплатно по активному тарифу." if seedream_included else ""),
                reply_markup=_main_menu_for(user_id),
            )
            st["t2i"] = {"step": "need_prompt", "aspect_ratio": str(t2i.get("aspect_ratio") or "9:16"), "model": "seedream_45"}
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

            # --- BILLING: 1 token for photosession generation ---
            ensure_user_row(int(user_id))

            bal = int(get_balance(int(user_id)) or 0)
            if bal < 1:
                text = (
                    "📸 Нейро-фотосессия стоит 1 токен.\n\n"
                    f"Ваш баланс: {bal}\n\n"
                    "Пополните баланс и продолжим генерацию 👇"
                )
                await tg_send_message(
                    chat_id,
                    text,
                    reply_markup=_topup_balance_inline_kb(),
                )
                return {"ok": True}

            charge_ref_id = uuid4().hex
            charged = False
            try:
                charge_photosession_generation(int(user_id), ref_id=charge_ref_id)
                charged = True
            except Exception as e:
                await tg_send_message(chat_id, f"Не удалось списать токен: {e}", reply_markup=_main_menu_for(user_id))
                return {"ok": True}

            # QUEUE: тяжёлую генерацию делаем в воркере
            job_id = uuid4().hex
            try:
                await enqueue_job({
                    "job_id": job_id,
                    "type": "photosession",
                    "chat_id": int(chat_id),
                    "user_id": int(user_id),
                    "photo_file_id": (ps.get("photo_file_id") or ""),
                    "prompt": prompt,
                    "size": ARK_SIZE_DEFAULT,
                    "charge_ref_id": charge_ref_id,
                }, queue_name="gen")
            except Exception as e:
                # если не смогли поставить в очередь — вернём токен
                try:
                    refund_photosession_generation(int(user_id), ref_id=charge_ref_id, error=f"enqueue_failed: {e}")
                except Exception:
                    pass

                await tg_send_message(chat_id, f"❌ Не удалось поставить в очередь: {e}", reply_markup=_main_menu_for(user_id))
                st["photosession"] = {"step": "need_photo", "photo_bytes": None}
                st["ts"] = _now()
                return {"ok": True}

            await tg_send_message(
                chat_id,
                "✅ Запрос принят. Начинаю обработку — пришлю результат, как будет готово.",
                reply_markup=_main_menu_for(user_id),
            )
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
            
        # ---- TTS: waiting for text ----
        if st.get("mode") == "tts_wait_text":
            tts = st.get("tts") or {}
            voice_id = (tts.get("voice_id") or "").strip()
            voice_name = (tts.get("name") or "голос").strip()

            user_text = (incoming_text or "").strip()
            if not user_text:
                await tg_send_message(chat_id, "Пришли текст одним сообщением — я озвучу.", reply_markup=_help_menu_for(user_id))
                return {"ok": True}

            if not await _tg_consume_free_tts_or_notify(chat_id, user_id, user_text):
                st["ts"] = _now()
                return {"ok": True}

            job_id = f"tg_tts:{user_id}:{uuid4().hex}"
            try:
                await enqueue_job(
                    {
                        "job_id": job_id,
                        "kind": "tg_tts_run",
                        "type": "tg_tts",
                        "chat_id": int(chat_id),
                        "user_id": int(user_id),
                        "text": user_text,
                        "voice_id": voice_id,
                        "voice_name": voice_name,
                        "model_id": "eleven_multilingual_v2",
                        "free_tts_consumed": True,
                        "main_menu_reply_markup": _main_menu_for(user_id),
                        "help_menu_reply_markup": _help_menu_for(user_id),
                    },
                    queue_name=TG_TTS_QUEUE_NAME,
                )
            except Exception as e:
                release_free_usage(user_id, FEATURE_TTS)
                await tg_send_message(
                    chat_id,
                    f"❌ Не удалось поставить озвучку в очередь: {e}",
                    reply_markup=_help_menu_for(user_id),
                )
                st["ts"] = _now()
                return {"ok": True}

            await tg_send_message(
                chat_id,
                f"✅ Озвучка принята в очередь ({voice_name}). Пришлю аудио, как будет готово.",
                reply_markup=None,
            )
            st["mode"] = "idle"
            st["tts"] = {}
            st["ts"] = _now()
            return {"ok": True}
                
        # CHAT: обычный текстовый ответ (с памятью только для режима ИИ-чата)
        if st.get("mode") == "chat":
            if st.get("ai_chat_mode") != "chat":
                await tg_send_message(
                    chat_id,
                    "Сначала выбери модель чата: Claude Sonnet, Claude Opus 4.7, Claude Fable 5 или ChatGPT. Либо выбери 🪄 Промт.",
                    reply_markup=_ai_chat_mode_inline_kb(),
                )
                return {"ok": True}

            model_key = _ai_chat_model_key(st)
            charge_ref_id = ""
            charge_tokens = 0
            if _is_fable_chat_model_key(model_key):
                charge_tokens = _tg_fable_chat_cost_tokens(st, has_files=False)
                charge_ref_id = await _tg_charge_fable_chat_or_notify(chat_id=chat_id, user_id=user_id, st=st, has_files=False)
                if not charge_ref_id:
                    st["ts"] = _now()
                    return {"ok": True}
            else:
                if not await _tg_consume_free_chat_or_notify(chat_id, user_id):
                    st["ts"] = _now()
                    return {"ok": True}

            queued = await _enqueue_tg_ai_chat_job(
                chat_id=chat_id,
                user_id=user_id,
                text=incoming_text,
                model_key=model_key,
                thinking=_tg_fable_thinking_enabled(st) if _is_fable_chat_model_key(model_key) else True,
                charge_tokens=charge_tokens,
                charge_ref_id=charge_ref_id,
            )
            if queued:
                return {"ok": True}

            if charge_ref_id:
                try:
                    add_tokens(user_id, int(charge_tokens), reason="claude_fable_chat_refund", ref_id=charge_ref_id, meta={"stage": "enqueue_failed", "source": "telegram"})
                except Exception:
                    pass
            else:
                release_free_usage(user_id, FEATURE_CHAT)
            await tg_send_message(chat_id, "❌ Не удалось поставить чат в очередь. Проверь REDIS_URL и worker_chat.py.", reply_markup=_main_menu_for(user_id))
            return {"ok": True}

        # fallback (should not happen): answer with Claude without memory
        answer = await kie_claude_answer(
            user_text=incoming_text,
            system_prompt=CLAUDE_TEXT_SYSTEM_PROMPT,
            history=[],
            summary="",
            max_tokens=1500,
            thinking=True,
        )
        await tg_send_message(chat_id, answer, reply_markup=_main_menu_for(user_id))
        return {"ok": True}

    return {"ok": True}


@app.post("/webhook/{secret}")
async def webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        return Response(status_code=403)

    update = await request.json()
    return await process_telegram_update(update)
