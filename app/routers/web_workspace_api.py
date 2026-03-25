from __future__ import annotations

import io
import json
import mimetypes
import os
import re
import tempfile
import zipfile
import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile, File, Form
from pydantic import BaseModel, Field

from ai_chat import openai_chat_answer
from app.routers.prompts import categories as prompts_categories
from app.routers.prompts import groups as prompts_groups
from app.routers.prompts import items as prompts_items
from app.routers.tts import ALLOWED_VOICE_IDS, ALLOWED_VOICES
from app.services.eleven_tts import ElevenTTS
from app.services.telegram_webauth import TelegramWebAuthError, validate_telegram_login_data
from app.services.workspace_account_service import (
    WorkspaceAccountError,
    WorkspaceAuthFailed,
    WorkspaceCodeExpired,
    WorkspaceCodeTooManyAttempts,
    WorkspaceMailerError,
    account_to_workspace_user_payload,
    change_password,
    confirm_email_registration,
    confirm_link_email,
    confirm_password_reset,
    ensure_workspace_account_from_claims,
    get_or_create_workspace_account_for_telegram,
    link_telegram_to_account,
    login_with_email,
    start_email_registration,
    start_link_email,
    start_password_reset,
)
from app.services.workspace_auth import WORKSPACE_SESSION_TTL_SEC, create_access_token, get_current_workspace_user, get_optional_workspace_user
from billing_db import add_tokens, ensure_user_row, get_balance
from db_supabase import supabase, track_user_activity
from kling3_flow import Kling3Error, create_kling3_task, get_kling3_task
from kling_flow import (
    KlingFlowError,
    REPLICATE_KLING_25_TURBO_PRO_MODEL,
    run_image_to_video_from_bytes,
    run_motion_control_from_bytes,
    run_text_to_video_from_prompt,
    upload_bytes_to_supabase,
)
from veo_flow import VeoFlowError, run_veo_image_to_video, run_veo_text_to_video
from kling3_pricing import calculate_kling3_price
from songwriter_prompt import SONGWRITER_SYSTEM_PROMPT
from queue_redis import enqueue_job
from nano_banana import run_nano_banana
from nano_banana_pro import handle_nano_banana_pro
from topaz_image_replicate import TopazImageParams, run_topaz_image_upscale
from topaz_pricing import get_photo_preset_settings, get_photo_preset_tokens
from yookassa_flow import create_yookassa_payment
from app.services.video_editor_service import (
    VIDEO_EDIT_QUEUE_NAME,
    MAX_AUDIO_CLIPS,
    MAX_MERGE_ITEMS,
    MAX_OUTPUT_DURATION_SEC,
    create_workspace_upload_record,
    get_workspace_edit_job_row,
    get_workspace_generation_row,
    get_workspace_upload_row,
    insert_workspace_edit_job_row,
    resolve_operation_type,
)

router = APIRouter(prefix="/api/workspace", tags=["workspace"])
OPENAI_CHAT_MODEL = (os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini") or "gpt-4o-mini").strip()
PROMPT_BUILDER_MODEL = (os.getenv("PROMPT_BUILDER_MODEL", "gpt-5.4") or "gpt-5.4").strip()
_tts_client: ElevenTTS | None = None

TOPAZ_PUBLIC_BUCKET = (os.getenv("TOPAZ_PUBLIC_BUCKET", "topaz-io") or "topaz-io").strip() or "topaz-io"
TOPAZ_INPUT_PREFIX = (os.getenv("TOPAZ_INPUT_PREFIX", "inputs") or "inputs").strip().strip("/") or "inputs"
TOPAZ_IMAGE_CREATE_RETRIES = max(1, int(os.getenv("TOPAZ_IMAGE_CREATE_RETRIES", "3") or "3"))
TOPAZ_IMAGE_RETRY_DELAY_SEC = max(0.25, float(os.getenv("TOPAZ_IMAGE_RETRY_DELAY_SEC", "1.5") or "1.5"))


CHAT_MODEL_LABEL_DEFAULT = "gpt-4o-mini"
PROMPT_MODEL_LABEL = "gpt-5.4"
MAX_CHAT_ATTACHMENTS = 6
MAX_CHAT_IMAGE_ATTACHMENTS = 4
MAX_CHAT_ATTACHMENT_BYTES = 8 * 1024 * 1024
MAX_CHAT_ATTACHMENT_TEXT_PER_FILE = 12000
MAX_CHAT_ATTACHMENT_TEXT_TOTAL = 28000
_TEXT_ATTACHMENT_EXTS = {
    ".txt", ".md", ".csv", ".json", ".js", ".ts", ".tsx", ".jsx", ".py", ".html", ".css",
    ".xml", ".yml", ".yaml", ".sql", ".ini", ".cfg", ".log", ".rtf", ".sh", ".bat"
}


def _normalize_chat_mode_value(value: Any) -> str:
    return "prompt_builder" if str(value or "").strip() == "prompt_builder" else "chat"


def _clamp_float(value: Any, default: float, low: float, high: float) -> float:
    try:
        out = float(value)
    except Exception:
        out = float(default)
    return max(low, min(high, out))


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        out = int(value)
    except Exception:
        out = int(default)
    return max(low, min(high, out))


WORKSPACE_TOPUP_PACKS: List[Dict[str, Any]] = [
    {"tokens": 5, "rub": 60, "stars": 33, "badge": "💰", "code": "lite"},
    {"tokens": 20, "rub": 180, "stars": 99, "badge": "⭐", "code": "plus"},
    {"tokens": 50, "rub": 450, "stars": 247, "badge": "🚀", "code": "pro"},
    {"tokens": 100, "rub": 850, "stars": 467, "badge": "👑", "code": "ultra"},
]


def _workspace_find_topup_pack(tokens: Any) -> Optional[Dict[str, Any]]:
    try:
        wanted = int(tokens)
    except Exception:
        return None
    for pack in WORKSPACE_TOPUP_PACKS:
        if int(pack.get("tokens") or 0) == wanted:
            return dict(pack)
    return None


def _sanitize_chat_history(value: Any) -> List[Dict[str, str]]:
    raw = value
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = []
    out: List[Dict[str, str]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        content = str(item.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            out.append({"role": role, "content": content[:8000]})
    return out


def _resolve_workspace_chat_model(requested_model: Any, mode: str) -> Dict[str, str]:
    mode_value = _normalize_chat_mode_value(mode)
    requested = str(requested_model or "").strip()
    if mode_value == "prompt_builder":
        return {"label": PROMPT_MODEL_LABEL, "actual": PROMPT_BUILDER_MODEL}
    if requested == PROMPT_MODEL_LABEL:
        return {"label": PROMPT_MODEL_LABEL, "actual": PROMPT_BUILDER_MODEL}
    if requested in {OPENAI_CHAT_MODEL, PROMPT_BUILDER_MODEL}:
        return {"label": requested, "actual": requested}
    return {"label": CHAT_MODEL_LABEL_DEFAULT, "actual": OPENAI_CHAT_MODEL}


_PROMPT_KEYWORDS = (
    "промпт", "prompt", "улучши", "усиль", "сцена", "референс", "reference", "кадр", "ракурс", "анимац",
    "видео", "video", "фото", "image", "изображен", "seedance", "kling", "veo", "sora", "nano banana",
    "cinematic", "camera", "lighting", "style", "shot", "motion", "aspect ratio", "negative prompt",
)


def _is_prompt_builder_request(text: str, has_files: bool) -> bool:
    value = str(text or "").strip().lower()
    if has_files:
        return True
    if not value:
        return False
    return any(token in value for token in _PROMPT_KEYWORDS)


def _prompt_builder_redirect_message() -> str:
    return (
        "Сейчас включён режим Prompt Builder. "
        "Опиши, какой prompt нужен: для фото, видео, улучшения черновика или по референсу."
    )


def _build_prompt_builder_system_prompt(model_label: str, image_refs: List[str]) -> str:
    ref_hint = ""
    if image_refs:
        ref_hint = (
            " Если пользователь просит prompt для Seedance и приложены изображения, используй теги "
            + ", ".join(image_refs)
            + ". Для Kling используй формат @image_1, @image_2. Для Veo, Sora и Nano Banana не придумывай inline-теги, если они не нужны."
        )
    return (
        "Ты — AstraBot Prompt Builder. "
        "Ты работаешь только с промптами и их улучшением. "
        "Если запрос не относится к созданию, адаптации или усилению промпта, ответь точно одной фразой: "
        "'Сейчас включён режим Prompt Builder. Опиши, какой prompt нужен: для фото, видео, улучшения черновика или по референсу.' "
        "Если запрос относится к промпту, сам выбери лучшую структуру ответа без лишних надстроек, уточняй только если это критично, "
        "и возвращай один сильный готовый prompt либо один короткий уточняющий вопрос, если без него нельзя. "
        "Не пиши объяснений, списков, вариантов, вступлений и служебных комментариев. "
        f"Если пользователь спрашивает, какая модель выбрана в интерфейсе, отвечай только названием модели: {model_label}."
        + ref_hint
    )


def _is_prompt_builder_output(answer: str) -> bool:
    value = str(answer or "").strip()
    return bool(value) and value != _prompt_builder_redirect_message()


def _decode_text_bytes(raw: bytes) -> str:
    for enc in ("utf-8", "utf-8-sig", "cp1251", "latin-1"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace")


def _extract_docx_text(raw: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", errors="ignore")
    except Exception:
        return ""
    text = re.sub(r"<w:tab[^>]*/>", "\t", xml)
    text = re.sub(r"<w:br[^>]*/>", "\n", text)
    text = re.sub(r"</w:p>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _guess_attachment_kind(filename: str, content_type: str) -> str:
    ext = Path(filename or "").suffix.lower()
    ctype = (content_type or "").lower()
    if ctype.startswith("image/"):
        return "image"
    if ext == ".pdf" or ctype == "application/pdf":
        return "pdf"
    if ext == ".docx" or ctype == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        return "docx"
    if ext in _TEXT_ATTACHMENT_EXTS or ctype.startswith("text/") or ctype in {
        "application/json", "application/xml", "text/csv", "application/javascript"
    }:
        return "text"
    return "binary"


async def _prepare_workspace_chat_attachments(files: List[UploadFile]) -> Dict[str, Any]:
    prepared: List[Dict[str, Any]] = []
    notices: List[str] = []
    text_blocks: List[str] = []
    image_bytes_list: List[bytes] = []
    total_text = 0

    for file in files[:MAX_CHAT_ATTACHMENTS]:
        filename = Path(getattr(file, "filename", "") or "file").name or "file"
        content_type = (
            getattr(file, "content_type", "")
            or mimetypes.guess_type(filename)[0]
            or "application/octet-stream"
        ).strip().lower()
        raw = await file.read()
        size_bytes = len(raw or b"")
        kind = _guess_attachment_kind(filename, content_type)
        item = {
            "name": filename,
            "kind": kind,
            "content_type": content_type,
            "size_bytes": size_bytes,
            "parsed": False,
        }

        if not raw:
            notices.append(f"{filename}: файл пустой.")
            prepared.append(item)
            continue

        if size_bytes > MAX_CHAT_ATTACHMENT_BYTES:
            notices.append(f"{filename}: файл больше 8 МБ, пропущен.")
            prepared.append(item)
            continue

        if kind == "image":
            if len(image_bytes_list) < MAX_CHAT_IMAGE_ATTACHMENTS:
                image_bytes_list.append(raw)
                item["parsed"] = True
            else:
                notices.append(
                    f"{filename}: превышен лимит изображений, учитываю только первые {MAX_CHAT_IMAGE_ATTACHMENTS}."
                )
            prepared.append(item)
            continue

        extracted = ""
        if kind == "text":
            extracted = _decode_text_bytes(raw)
        elif kind == "docx":
            extracted = _extract_docx_text(raw)
        elif kind == "pdf":
            notices.append(f"{filename}: PDF принят, но автоматическое извлечение текста на сервере пока не включено.")
        else:
            notices.append(f"{filename}: файл прикреплён, но этот формат пока не разбирается автоматически.")

        cleaned = extracted.replace("\x00", "").strip()
        if cleaned:
            cleaned = cleaned[:MAX_CHAT_ATTACHMENT_TEXT_PER_FILE]
            remaining = max(0, MAX_CHAT_ATTACHMENT_TEXT_TOTAL - total_text)
            if remaining > 0:
                block = f"[Файл: {filename}]\n{cleaned[:remaining]}"
                text_blocks.append(block)
                total_text += len(block)
                item["parsed"] = True
        elif kind == "docx":
            notices.append(f"{filename}: не удалось извлечь текст из DOCX.")

        prepared.append(item)

    summary_lines = [
        f"- {item['name']} · {item['kind']} · {max(1, round((item['size_bytes'] or 0) / 1024))} KB"
        for item in prepared
    ]
    context_parts: List[str] = []
    if summary_lines:
        context_parts.append("Пользователь приложил файлы:\n" + "\n".join(summary_lines))
    if notices:
        context_parts.append("Служебные заметки по вложениям:\n" + "\n".join(f"- {note}" for note in notices))
    if text_blocks:
        context_parts.append("Извлечённое содержимое файлов:\n\n" + "\n\n".join(text_blocks))

    return {
        "items": prepared,
        "context": "\n\n".join(part for part in context_parts if part).strip(),
        "image_bytes_list": image_bytes_list,
    }


def _chat_models() -> List[str]:
    out: List[str] = []
    for m in [OPENAI_CHAT_MODEL, PROMPT_BUILDER_MODEL]:
        m = (m or "").strip()
        if m and m not in out:
            out.append(m)
    return out or ["gpt-4o-mini"]


def _get_tts() -> ElevenTTS:
    global _tts_client
    if _tts_client is None:
        api_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
        if not api_key:
            raise HTTPException(status_code=500, detail="ELEVENLABS_API_KEY is not set")
        _tts_client = ElevenTTS(api_key=api_key)
    return _tts_client


def _find_existing_bot_user(telegram_user_id: int) -> Optional[Dict[str, Any]]:
    if supabase is None:
        return {"telegram_user_id": telegram_user_id}
    try:
        r = supabase.table("bot_users").select("telegram_user_id,username,first_name,last_name,language_code,is_premium").eq("telegram_user_id", int(telegram_user_id)).limit(1).execute()
        if getattr(r, "data", None):
            return r.data[0]
    except Exception:
        return {"telegram_user_id": telegram_user_id}
    return None


def _workspace_user_payload(user: Dict[str, Any]) -> Dict[str, Any]:
    account_id = int(user.get("workspace_user_id") or user.get("telegram_user_id") or user.get("id") or 0)
    linked_tg = user.get("linked_telegram_user_id")
    try:
        linked_tg = int(linked_tg) if linked_tg not in (None, "") else None
    except Exception:
        linked_tg = None
    return {
        "workspace_user_id": account_id,
        "telegram_user_id": account_id,
        "linked_telegram_user_id": linked_tg,
        "username": user.get("username"),
        "first_name": user.get("first_name"),
        "last_name": user.get("last_name"),
        "language_code": user.get("language_code"),
        "photo_url": user.get("photo_url"),
        "is_premium": bool(user.get("is_premium", False)),
        "email": user.get("email"),
        "email_verified": bool(user.get("email_verified", False)),
        "auth_methods": user.get("auth_methods") or (["telegram"] if linked_tg else []) + (["email"] if user.get("email") else []),
    }


def _songwriter_prompt_with_context(p: "SongwriterPayload") -> str:
    ctx_parts = []
    if p.language:
        ctx_parts.append(f"Язык: {p.language}")
    if p.genre:
        ctx_parts.append(f"Жанр: {p.genre}")
    if p.mood:
        ctx_parts.append(f"Настроение: {p.mood}")
    if p.references:
        ctx_parts.append(f"Референсы/вайб: {p.references}")
    if not ctx_parts:
        return SONGWRITER_SYSTEM_PROMPT
    return SONGWRITER_SYSTEM_PROMPT + "\n\nКонтекст:\n- " + "\n- ".join(ctx_parts)



def _first_nonempty(*values: Any) -> Optional[str]:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _normalize_kling3_task(task: Dict[str, Any]) -> Dict[str, Any]:
    payload = task.get("data") if isinstance(task.get("data"), dict) else task
    output = payload.get("output") if isinstance(payload, dict) and isinstance(payload.get("output"), dict) else {}
    error = payload.get("error") if isinstance(payload, dict) and isinstance(payload.get("error"), dict) else {}

    provider_status = str(
        (payload.get("status") if isinstance(payload, dict) else None)
        or task.get("status")
        or task.get("state")
        or ""
    ).strip().lower()

    video_url = _first_nonempty(
        output.get("video"),
        output.get("video_url"),
        output.get("url"),
        payload.get("video") if isinstance(payload, dict) else None,
        payload.get("video_url") if isinstance(payload, dict) else None,
        task.get("video"),
        task.get("video_url"),
    )
    download_url = _first_nonempty(
        output.get("download_url"),
        payload.get("download_url") if isinstance(payload, dict) else None,
        task.get("download_url"),
    )
    cover_url = _first_nonempty(
        output.get("cover_url"),
        payload.get("cover_url") if isinstance(payload, dict) else None,
        task.get("cover_url"),
    )
    output_url = video_url or download_url

    percent_raw = output.get("percent")
    percent: Optional[int] = None
    if percent_raw not in (None, ""):
        try:
            percent = max(0, min(100, int(round(float(percent_raw)))))
        except Exception:
            percent = None

    error_message = _first_nonempty(
        error.get("message"),
        error.get("raw_message"),
        payload.get("error_message") if isinstance(payload, dict) else None,
        output.get("message"),
        task.get("message"),
        task.get("detail"),
    )

    if provider_status in {"failed", "error", "cancelled", "canceled"}:
        status = "failed"
    elif output_url:
        status = "succeeded"
    elif provider_status in {"queued", "pending"}:
        status = "queued"
    else:
        status = "processing"

    return {
        "task_id": _first_nonempty(
            payload.get("task_id") if isinstance(payload, dict) else None,
            task.get("task_id"),
        ),
        "model": payload.get("model") if isinstance(payload, dict) else task.get("model"),
        "task_type": payload.get("task_type") if isinstance(payload, dict) else task.get("task_type"),
        "status": status,
        "provider_status": provider_status or "unknown",
        "percent": percent,
        "video_url": video_url,
        "download_url": download_url,
        "output_url": output_url,
        "cover_url": cover_url,
        "error_message": error_message,
        "finished": bool(output_url or provider_status in {"failed", "error", "cancelled", "canceled"}),
    }


_WORKSPACE_VIDEO_GENERATIONS_TABLE = "workspace_video_generations"

_WORKSPACE_IMAGE_GENERATIONS_TABLE = "workspace_image_generations"
_WORKSPACE_VOICE_GENERATIONS_TABLE = "workspace_voice_generations"
_WORKSPACE_MUSIC_GENERATIONS_TABLE = "workspace_music_generations"
_WORKSPACE_MUSIC_TRACKS_TABLE = "workspace_music_tracks"

PIAPI_API_KEY = os.getenv("PIAPI_API_KEY", "").strip()
PIAPI_BASE_URL = os.getenv("PIAPI_BASE_URL", "https://api.piapi.ai").rstrip("/")
SUNOAPI_API_KEY = os.getenv("SUNOAPI_API_KEY", "").strip()
SUNOAPI_BASE_URL = os.getenv("SUNOAPI_BASE_URL", "https://api.sunoapi.org/api/v1").rstrip("/")
if SUNOAPI_BASE_URL.rstrip("/") == "https://api.sunoapi.org":
    SUNOAPI_BASE_URL = "https://api.sunoapi.org/api/v1"
SUNOAPI_CALLBACK_URL = os.getenv("SUNOAPI_CALLBACK_URL", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
SUNOAPI_CALLBACK_SECRET = os.getenv("SUNOAPI_CALLBACK_SECRET", "").strip()
SUNOAPI_POLL_TIMEOUT_SEC = int(os.getenv("SUNOAPI_POLL_TIMEOUT_SEC", "600"))
PIAPI_POLL_TIMEOUT_SEC = int(os.getenv("PIAPI_POLL_TIMEOUT_SEC", "300"))
WORKSPACE_MUSIC_COST_TOKENS = max(0, int(os.getenv("WORKSPACE_MUSIC_COST_TOKENS", "2") or "2"))
WORKSPACE_MUSIC_UPLOAD_SIGNED_TTL_SEC = max(3600, int(os.getenv("WORKSPACE_MUSIC_UPLOAD_SIGNED_TTL_SEC", "86400") or "86400"))
_MUSIC_ALLOWED_SUNO_MODELS = {"V4", "V4_5", "V4_5PLUS", "V4_5ALL", "V5"}
_MUSIC_ALLOWED_PERSONA_MODELS = {"style_persona", "voice_persona"}




def _workspace_suno_callback_url() -> str:
    if SUNOAPI_CALLBACK_URL:
        return SUNOAPI_CALLBACK_URL
    if not PUBLIC_BASE_URL:
        raise RuntimeError("Set SUNOAPI_CALLBACK_URL or PUBLIC_BASE_URL for Suno callBackUrl")
    url = f"{PUBLIC_BASE_URL}/sunoapi/callback"
    if SUNOAPI_CALLBACK_SECRET:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}secret={SUNOAPI_CALLBACK_SECRET}"
    return url


def _normalize_suno_model(value: Any, *, fallback: str = "V4_5") -> str:
    raw = str(value or "").strip().upper().replace(".", "_")
    if raw == "V4_5PLUS":
        return raw
    if raw in _MUSIC_ALLOWED_SUNO_MODELS:
        return raw
    return fallback if fallback in _MUSIC_ALLOWED_SUNO_MODELS else "V4_5"


def _normalize_persona_model(value: Any) -> str:
    raw = str(value or "").strip().lower()
    return raw if raw in _MUSIC_ALLOWED_PERSONA_MODELS else "style_persona"


def _normalize_vocal_gender(value: Any) -> Optional[str]:
    raw = str(value or "").strip().lower()
    return raw if raw in {"m", "f"} else None


def _music_optional_float(value: Any, default: float = 0.65) -> float:
    try:
        out = round(float(value), 2)
    except Exception:
        out = default
    if out < 0:
        out = 0.0
    if out > 1:
        out = 1.0
    return out


def _workspace_music_source_audio_path(*, user_id: int, slot: str, filename: str = "audio.bin") -> str:
    dt = datetime.now(timezone.utc)
    safe_slot = re.sub(r"[^a-z0-9_-]+", "_", str(slot or "audio").strip().lower()).strip("_") or "audio"
    suffix = Path(str(filename or "audio.bin")).suffix.lower() or ".bin"
    return f"workspace_music_inputs/{int(user_id)}/{dt:%Y/%m/%d}/{safe_slot}_{uuid4().hex}{suffix}"


def _upload_workspace_music_source_file(*, file_bytes: bytes, filename: str, content_type: str, user_id: int, slot: str = "audio") -> Dict[str, Any]:
    if supabase is None:
        raise RuntimeError("Supabase client is not configured")
    if not file_bytes:
        raise RuntimeError("Audio file is empty")
    storage_path = _workspace_music_source_audio_path(user_id=user_id, slot=slot, filename=filename)
    supabase.storage.from_(_WORKSPACE_VIDEOS_BUCKET).upload(
        path=storage_path,
        file=file_bytes,
        file_options={"content-type": content_type or "application/octet-stream", "upsert": "true"},
    )
    signed_url = None
    try:
        signed_result = supabase.storage.from_(_WORKSPACE_VIDEOS_BUCKET).create_signed_url(storage_path, WORKSPACE_MUSIC_UPLOAD_SIGNED_TTL_SEC)
        signed_url = _absolutize_supabase_url(_extract_storage_signed_url(signed_result))
    except Exception:
        signed_url = None
    if not signed_url:
        try:
            public_result = supabase.storage.from_(_WORKSPACE_VIDEOS_BUCKET).get_public_url(storage_path)
            signed_url = _absolutize_supabase_url(_extract_storage_public_url(public_result))
        except Exception:
            signed_url = None
    if not signed_url:
        raise RuntimeError("Could not build public URL for uploaded audio")
    return {"storage_path": storage_path, "upload_url": signed_url, "mime_type": content_type or "application/octet-stream", "size_bytes": len(file_bytes)}

_WORKSPACE_IMAGE_OPTIONAL_COLUMNS = {"preset_slug", "source_image_url", "before_image_url", "after_image_url", "compare_mode"}

_WORKSPACE_VIDEOS_BUCKET = (os.getenv("WORKSPACE_VIDEOS_BUCKET", "workspace-videos") or "workspace-videos").strip() or "workspace-videos"
_WORKSPACE_VOICE_BUCKET = (os.getenv("SUPABASE_BUCKET") or _WORKSPACE_VIDEOS_BUCKET).strip() or _WORKSPACE_VIDEOS_BUCKET


def _storage_content_type_to_ext(content_type: Optional[str], url: Optional[str] = None) -> str:
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    mapping = {
        "video/mp4": "mp4",
        "video/quicktime": "mov",
        "video/webm": "webm",
        "video/x-matroska": "mkv",
    }
    if ctype in mapping:
        return mapping[ctype]
    guessed = mimetypes.guess_extension(ctype) or ""
    guessed = guessed.lstrip(".").lower()
    if guessed:
        return guessed
    parsed_path = urlparse(url or "").path
    suffix = Path(parsed_path).suffix.lstrip(".").lower()
    if suffix:
        return suffix
    return "mp4"


def _workspace_video_storage_path(*, user_id: int, generation_id: str, ext: str) -> str:
    now = datetime.now(timezone.utc)
    safe_ext = (ext or "mp4").lstrip(".").lower() or "mp4"
    return f"{user_id}/{now:%Y/%m/%d}/{generation_id}.{safe_ext}"


def _extract_storage_public_url(public_result: Any) -> Optional[str]:
    if isinstance(public_result, str):
        return public_result
    if isinstance(public_result, dict):
        return public_result.get("publicUrl") or public_result.get("public_url")
    return None


def _extract_storage_signed_url(signed_result: Any) -> Optional[str]:
    if isinstance(signed_result, str):
        return signed_result
    if isinstance(signed_result, dict):
        return (
            signed_result.get("signedURL")
            or signed_result.get("signedUrl")
            or signed_result.get("signed_url")
            or signed_result.get("url")
        )
    return None


def _absolutize_supabase_url(url: Optional[str]) -> Optional[str]:
    value = str(url or "").strip()
    if not value:
        return None
    if value.startswith("http://") or value.startswith("https://"):
        return value
    base = (os.getenv("SUPABASE_URL", "") or "").strip().rstrip("/")
    if base and value.startswith("/"):
        return f"{base}{value}"
    return value


def _build_workspace_video_access_urls(*, storage_path: Optional[str], fallback_url: Optional[str], expires_in: int = 3600) -> Dict[str, Optional[str]]:
    storage_path_text = str(storage_path or "").strip()
    fallback_text = str(fallback_url or "").strip() or None
    signed_url: Optional[str] = None

    if storage_path_text and supabase is not None:
        try:
            signed_result = supabase.storage.from_(_WORKSPACE_VIDEOS_BUCKET).create_signed_url(storage_path_text, expires_in)
            signed_url = _absolutize_supabase_url(_extract_storage_signed_url(signed_result))
        except Exception:
            signed_url = None
        if not signed_url:
            try:
                public_result = supabase.storage.from_(_WORKSPACE_VIDEOS_BUCKET).get_public_url(storage_path_text)
                signed_url = _absolutize_supabase_url(_extract_storage_public_url(public_result))
            except Exception:
                signed_url = None

    access_url = signed_url or fallback_text
    return {
        "video_url": access_url,
        "download_url": access_url,
        "signed_url": signed_url,
    }


def _serialize_workspace_generation(row: Dict[str, Any], *, signed_expires_in: int = 3600) -> Dict[str, Any]:
    access = _build_workspace_video_access_urls(
        storage_path=row.get("storage_path"),
        fallback_url=row.get("provider_video_url"),
        expires_in=signed_expires_in,
    )
    return {
        "id": row.get("id"),
        "user_id": row.get("user_id"),
        "provider": row.get("provider"),
        "model": row.get("model"),
        "mode": row.get("mode"),
        "task_id": row.get("task_id"),
        "prompt": row.get("prompt"),
        "status": row.get("status"),
        "aspect_ratio": row.get("aspect_ratio"),
        "duration_sec": row.get("duration_sec"),
        "resolution": row.get("resolution"),
        "enable_audio": row.get("enable_audio"),
        "provider_video_url": row.get("provider_video_url"),
        "storage_path": row.get("storage_path"),
        "thumbnail_path": row.get("thumbnail_path"),
        "file_size_bytes": row.get("file_size_bytes"),
        "mime_type": row.get("mime_type"),
        "error_code": row.get("error_code"),
        "error_message": row.get("error_message"),
        "origin": row.get("origin"),
        "is_favorite": row.get("is_favorite"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "completed_at": row.get("completed_at"),
        "video_url": access.get("video_url"),
        "download_url": access.get("download_url"),
        "signed_url": access.get("signed_url"),
        "has_storage_file": bool(str(row.get("storage_path") or "").strip()),
    }


def _get_workspace_generation_by_task(user_id: int, task_id: Optional[str]) -> Optional[Dict[str, Any]]:
    task_id_text = str(task_id or "").strip()
    if supabase is None or not task_id_text:
        return None
    try:
        resp = (
            supabase.table(_WORKSPACE_VIDEO_GENERATIONS_TABLE)
            .select("id,user_id,task_id,status,provider_video_url,storage_path,file_size_bytes,mime_type")
            .eq("user_id", str(user_id))
            .eq("task_id", task_id_text)
            .limit(1)
            .execute()
        )
        rows = getattr(resp, "data", None) or []
        if rows and isinstance(rows[0], dict):
            return rows[0]
    except Exception:
        return None
    return None


async def _download_video_to_tempfile(url: str) -> tuple[str, int, str]:
    target_url = str(url or "").strip()
    if not target_url:
        raise RuntimeError("Missing provider video url")

    timeout = httpx.Timeout(connect=20.0, read=300.0, write=60.0, pool=60.0)
    total_bytes = 0
    content_type = "video/mp4"
    ext = "mp4"
    tmp_path = ""

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        async with client.stream("GET", target_url) as resp:
            resp.raise_for_status()
            content_type = (resp.headers.get("content-type") or "video/mp4").split(";", 1)[0].strip() or "video/mp4"
            ext = _storage_content_type_to_ext(content_type, target_url)
            with tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}") as tmp:
                tmp_path = tmp.name
                async for chunk in resp.aiter_bytes(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    tmp.write(chunk)
                    total_bytes += len(chunk)

    if not tmp_path or total_bytes <= 0:
        raise RuntimeError("Downloaded provider video is empty")
    return tmp_path, total_bytes, content_type


def _upload_workspace_video_file(*, local_path: str, user_id: int, generation_id: str, content_type: str) -> Dict[str, Any]:
    if supabase is None:
        raise RuntimeError("Supabase client is not configured")
    source_path = str(local_path or "").strip()
    if not source_path:
        raise RuntimeError("local_path is empty")

    ext = Path(source_path).suffix.lstrip(".").lower() or _storage_content_type_to_ext(content_type)
    storage_path = _workspace_video_storage_path(user_id=user_id, generation_id=generation_id, ext=ext)

    with open(source_path, "rb") as fh:
        file_bytes = fh.read()

    if not file_bytes:
        raise RuntimeError("Local video file is empty")

    supabase.storage.from_(_WORKSPACE_VIDEOS_BUCKET).upload(
        path=storage_path,
        file=file_bytes,
        file_options={
            "content-type": content_type or "video/mp4",
            "upsert": "true",
        },
    )

    public_url = None
    try:
        public_url = _extract_storage_public_url(
            supabase.storage.from_(_WORKSPACE_VIDEOS_BUCKET).get_public_url(storage_path)
        )
    except Exception:
        public_url = None

    return {
        "storage_path": storage_path,
        "public_url": public_url,
        "file_size_bytes": len(file_bytes),
        "mime_type": content_type or "video/mp4",
    }


async def _archive_workspace_video_if_needed(user_id: int, task_id: Optional[str], normalized: Dict[str, Any]) -> None:
    if supabase is None:
        return

    row = _get_workspace_generation_by_task(user_id, task_id)
    if not row:
        return

    existing_storage_path = _first_nonempty(row.get("storage_path"))
    if existing_storage_path:
        return

    provider_video_url = _first_nonempty(
        row.get("provider_video_url"),
        normalized.get("video_url"),
        normalized.get("download_url"),
        normalized.get("output_url"),
    )
    if not provider_video_url:
        return

    generation_id = str(row.get("id") or "").strip()
    if not generation_id:
        return

    tmp_path = ""
    try:
        tmp_path, downloaded_bytes, content_type = await _download_video_to_tempfile(provider_video_url)
        uploaded = _upload_workspace_video_file(
            local_path=tmp_path,
            user_id=user_id,
            generation_id=generation_id,
            content_type=content_type,
        )
        patch: Dict[str, Any] = {
            "storage_path": uploaded.get("storage_path"),
            "file_size_bytes": int(uploaded.get("file_size_bytes") or downloaded_bytes or 0),
            "mime_type": uploaded.get("mime_type") or content_type or "video/mp4",
            "error_code": None,
        }
        # Keep public_url nullable because bucket is expected to be private.
        # Save only when available so the column stays harmless for private buckets.
        if uploaded.get("public_url"):
            patch["public_url"] = uploaded["public_url"]
        _update_workspace_generation(generation_id, patch)
    except Exception as e:
        _update_workspace_generation(
            generation_id,
            {
                "error_code": "archive_error",
                "error_message": f"Archive upload failed: {str(e)[:3800]}",
            },
        )
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
                pass



def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_generation_status(normalized_status: Optional[str]) -> str:
    status = str(normalized_status or "").strip().lower()
    if status == "succeeded":
        return "completed"
    if status in {"failed", "queued", "processing"}:
        return status
    return "processing"


def _insert_workspace_generation(row: Dict[str, Any]) -> str:
    generation_id = str(row.get("id") or uuid4())
    payload = dict(row)
    payload["id"] = generation_id
    if supabase is None:
        return generation_id
    resp = supabase.table(_WORKSPACE_VIDEO_GENERATIONS_TABLE).insert(payload).execute()
    data = getattr(resp, "data", None) or []
    if data and isinstance(data[0], dict):
        saved_id = data[0].get("id")
        if saved_id:
            return str(saved_id)
    return generation_id


def _update_workspace_generation(generation_id: Optional[str], patch: Dict[str, Any]) -> None:
    if not generation_id or not patch or supabase is None:
        return
    supabase.table(_WORKSPACE_VIDEO_GENERATIONS_TABLE).update(patch).eq("id", str(generation_id)).execute()


def _mark_workspace_generation_failed(generation_id: Optional[str], error_message: str, error_code: Optional[str] = None) -> None:
    if not generation_id:
        return
    patch: Dict[str, Any] = {
        "status": "failed",
        "error_message": (error_message or "").strip()[:4000] or "Unknown error",
    }
    if error_code:
        patch["error_code"] = str(error_code)[:255]
    try:
        _update_workspace_generation(generation_id, patch)
    except Exception:
        pass


async def _sync_workspace_generation_by_task(user_id: int, task_id: Optional[str], normalized: Dict[str, Any]) -> None:
    task_id_text = str(task_id or normalized.get("task_id") or "").strip()
    if supabase is None or not task_id_text:
        return

    db_status = _db_generation_status(normalized.get("status"))
    patch: Dict[str, Any] = {
        "status": db_status,
    }

    provider_video_url = _first_nonempty(
        normalized.get("video_url"),
        normalized.get("download_url"),
        normalized.get("output_url"),
    )
    if provider_video_url:
        patch["provider_video_url"] = provider_video_url

    error_message = _first_nonempty(normalized.get("error_message"))
    if db_status == "completed":
        patch["completed_at"] = _utc_now_iso()
        patch["error_code"] = None
        patch["error_message"] = None
    elif db_status == "failed":
        patch["completed_at"] = _utc_now_iso()
        patch["error_code"] = "provider_error"
        patch["error_message"] = (error_message or "Provider task failed")[:4000]

    supabase.table(_WORKSPACE_VIDEO_GENERATIONS_TABLE).update(patch).eq("user_id", str(user_id)).eq("task_id", task_id_text).execute()

    if db_status == "completed" and provider_video_url:
        try:
            await _archive_workspace_video_if_needed(user_id, task_id_text, normalized)
        except Exception:
            pass


PIAPI_BASE_URL = (os.getenv("PIAPI_BASE_URL", "https://api.piapi.ai") or "https://api.piapi.ai").strip().rstrip("/")
PIAPI_API_KEY = (os.getenv("PIAPI_API_KEY") or os.getenv("PIAPI_KEY") or "").strip()

SORA_TIMEOUT_SEC = int(os.getenv("SORA_TIMEOUT_SEC", "1800") or "1800")
SORA_POLL_SEC = float(os.getenv("SORA_POLL_SEC", "10") or "10")
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
OPENAI_API_BASE = (os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1") or "https://api.openai.com/v1").rstrip("/")


def _parse_form_bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_form_int(value: Any, default: int) -> int:
    try:
        return int(str(value or "").strip())
    except Exception:
        return int(default)


def _normalize_workspace_video_resolution(provider: str, model: str, resolution: Any) -> str:
    value = str(resolution or "").strip().lower()
    if provider == "veo":
        return "1080p" if model == "veo-3.1-pro" else "720p"
    if value in {"720", "720p"}:
        return "720"
    if value in {"1080", "1080p"}:
        return "1080"
    return "720"


def _history_mode_for_run(provider: str, mode: str) -> str:
    if provider == "kling" and mode == "motion_control":
        return "motion_control"
    return mode or "text_to_video"


async def _upload_reference_images_to_public_urls(user_id: int, images: List[bytes], prefix: str) -> List[str]:
    urls: List[str] = []
    for idx, raw in enumerate(images or [], start=1):
        if not raw:
            continue
        ext = "jpg"
        content_type = "image/jpeg"
        if raw[:8].startswith(b"\x89PNG"):
            ext = "png"
            content_type = "image/png"
        elif raw[:12].startswith(b"RIFF") and raw[8:12] == b"WEBP":
            ext = "webp"
            content_type = "image/webp"
        path = f"workspace_refs/{user_id}/{int(time.time())}_{uuid4().hex[:10]}_{prefix}_{idx}.{ext}"
        url = upload_bytes_to_supabase(path, raw, content_type)
        urls.append(url)
    return urls


async def _piapi_seedance_create_task_workspace(
    *,
    task_type: str,
    prompt: Optional[str] = None,
    duration: Optional[int] = None,
    aspect_ratio: Optional[str] = None,
    image_urls: Optional[List[str]] = None,
    service_mode: str = "public",
) -> Dict[str, Any]:
    if not PIAPI_API_KEY:
        raise RuntimeError("PIAPI_API_KEY not set")
    body: Dict[str, Any] = {"model": "seedance", "task_type": task_type, "input": {}}
    if prompt is not None:
        body["input"]["prompt"] = prompt
    if duration is not None:
        body["input"]["duration"] = int(duration)
    if aspect_ratio is not None:
        body["input"]["aspect_ratio"] = aspect_ratio
    if image_urls:
        body["input"]["image_urls"] = image_urls
    body["config"] = {"service_mode": service_mode}
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(f"{PIAPI_BASE_URL}/api/v1/task", headers={"X-API-Key": PIAPI_API_KEY}, json=body)
    if resp.status_code >= 300:
        raise RuntimeError(f"PiAPI seedance create failed: {resp.status_code} {resp.text[:600]}")
    try:
        return resp.json()
    except Exception:
        raise RuntimeError("PiAPI seedance create: bad JSON")


async def _piapi_seedance_get_task_workspace(task_id: str) -> Dict[str, Any]:
    if not PIAPI_API_KEY:
        raise RuntimeError("PIAPI_API_KEY not set")
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(f"{PIAPI_BASE_URL}/api/v1/task/{task_id}", headers={"X-API-Key": PIAPI_API_KEY})
    if resp.status_code >= 300:
        raise RuntimeError(f"PiAPI seedance get failed: {resp.status_code} {resp.text[:600]}")
    try:
        return resp.json()
    except Exception:
        raise RuntimeError("PiAPI seedance get: bad JSON")


def _seedance_status_lower_workspace(resp: Dict[str, Any]) -> str:
    if not isinstance(resp, dict):
        return ""
    if resp.get("status"):
        return str(resp.get("status") or "").lower().strip()
    data = resp.get("data") or {}
    if isinstance(data, dict):
        return str(data.get("status") or "").lower().strip()
    return ""


async def _piapi_seedance_wait_workspace(task_id: str, *, timeout_s: int = 7200, poll_s: float = 6.0) -> Dict[str, Any]:
    started = asyncio.get_event_loop().time()
    last: Dict[str, Any] = {}
    while True:
        last = await _piapi_seedance_get_task_workspace(task_id)
        status = _seedance_status_lower_workspace(last)
        if status in {"completed", "failed"}:
            return last
        if (asyncio.get_event_loop().time() - started) > float(timeout_s):
            raise TimeoutError("Seedance: timeout while waiting")
        await asyncio.sleep(poll_s)


def _seedance_extract_output_url_workspace(resp: Dict[str, Any]) -> Optional[str]:
    if not isinstance(resp, dict):
        return None
    payload = resp.get("data") if isinstance(resp.get("data"), dict) else resp
    output = (payload.get("output") or {}) if isinstance(payload, dict) else {}
    if isinstance(output, dict):
        for key in ("video", "video_url", "videoUrl", "url", "mp4_url", "mp4Url", "file_url", "fileUrl"):
            value = output.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value
        values = output.get("video_urls") or output.get("videoUrls")
        if isinstance(values, list):
            for value in values:
                if isinstance(value, str) and value.startswith("http"):
                    return value
    return None


def _sora_headers_workspace() -> Dict[str, str]:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set")
    return {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
    }


def _sora_size_from_aspect_workspace(aspect_ratio: str) -> str:
    ar = str(aspect_ratio or "16:9").strip()
    if ar == "9:16":
        return "720x1280"
    return "1280x720"


async def _sora_create_video_workspace(*, prompt: str, duration: int, aspect_ratio: str, model: str = "sora-2") -> Dict[str, Any]:
    url = f"{OPENAI_API_BASE}/videos"
    files = [
        ("model", (None, str(model or "sora-2"))),
        ("prompt", (None, str(prompt or "").strip())),
        ("seconds", (None, str(int(duration)))),
        ("size", (None, _sora_size_from_aspect_workspace(aspect_ratio))),
    ]
    headers = _sora_headers_workspace()
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(url, headers=headers, files=files)
    if resp.status_code >= 300:
        try:
            payload = resp.json()
            message = ((payload.get("error") or {}).get("message")) or resp.text[:800]
            raise RuntimeError(message)
        except Exception:
            raise RuntimeError(f"OpenAI create video failed: {resp.status_code} {resp.text[:800]}")
    return resp.json()


async def _sora_retrieve_video_workspace(video_id: str) -> Dict[str, Any]:
    url = f"{OPENAI_API_BASE}/videos/{video_id}"
    headers = _sora_headers_workspace()
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(url, headers=headers)
    if resp.status_code >= 300:
        try:
            payload = resp.json()
            message = ((payload.get("error") or {}).get("message")) or resp.text[:800]
            raise RuntimeError(message)
        except Exception:
            raise RuntimeError(f"OpenAI retrieve video failed: {resp.status_code} {resp.text[:800]}")
    return resp.json()


async def _sora_download_video_workspace(video_id: str) -> bytes:
    url = f"{OPENAI_API_BASE}/videos/{video_id}/content"
    headers = _sora_headers_workspace()
    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.get(url, headers=headers, params={"variant": "video"})
    if resp.status_code >= 300:
        try:
            payload = resp.json()
            message = ((payload.get("error") or {}).get("message")) or resp.text[:800]
            raise RuntimeError(message)
        except Exception:
            raise RuntimeError(f"OpenAI download video failed: {resp.status_code} {resp.text[:800]}")
    return resp.content


async def _sora_poll_video_workspace(video_id: str, *, timeout_sec: int = SORA_TIMEOUT_SEC, sleep_sec: float = SORA_POLL_SEC) -> Dict[str, Any]:
    started = time.time()
    last: Dict[str, Any] = {}
    while True:
        last = await _sora_retrieve_video_workspace(video_id)
        status = str(last.get("status") or "").strip().lower()
        if status in {"completed", "failed"}:
            return last
        if (time.time() - started) > float(timeout_sec):
            raise TimeoutError(f"Sora timeout after {timeout_sec}s (video_id={video_id}, status={status})")
        await asyncio.sleep(max(2.0, float(sleep_sec)))


async def _finalize_workspace_generation_from_bytes(
    *,
    generation_id: str,
    user_id: int,
    video_bytes: bytes,
    provider_video_url: Optional[str] = None,
    content_type: str = "video/mp4",
) -> None:
    if not video_bytes:
        raise RuntimeError("Provider returned empty video bytes")

    tmp_path = ""
    try:
        patch: Dict[str, Any] = {"status": "processing"}
        if provider_video_url:
            patch["provider_video_url"] = provider_video_url
        _update_workspace_generation(generation_id, patch)

        ext = _storage_content_type_to_ext(content_type, provider_video_url)
        with tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}") as tmp:
            tmp.write(video_bytes)
            tmp_path = tmp.name

        uploaded = _upload_workspace_video_file(
            local_path=tmp_path,
            user_id=user_id,
            generation_id=generation_id,
            content_type=content_type,
        )
        _update_workspace_generation(
            generation_id,
            {
                "status": "completed",
                "provider_video_url": provider_video_url,
                "storage_path": uploaded.get("storage_path"),
                "file_size_bytes": int(uploaded.get("file_size_bytes") or len(video_bytes) or 0),
                "mime_type": uploaded.get("mime_type") or content_type or "video/mp4",
                "completed_at": _utc_now_iso(),
                "error_code": None,
                "error_message": None,
            },
        )
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
                pass


async def _finalize_workspace_generation_from_url(
    *,
    generation_id: str,
    user_id: int,
    provider_video_url: str,
) -> None:
    tmp_path = ""
    try:
        _update_workspace_generation(generation_id, {"provider_video_url": provider_video_url, "status": "processing"})
        tmp_path, downloaded_bytes, content_type = await _download_video_to_tempfile(provider_video_url)
        uploaded = _upload_workspace_video_file(
            local_path=tmp_path,
            user_id=user_id,
            generation_id=generation_id,
            content_type=content_type,
        )
        _update_workspace_generation(
            generation_id,
            {
                "status": "completed",
                "provider_video_url": provider_video_url,
                "storage_path": uploaded.get("storage_path"),
                "file_size_bytes": int(uploaded.get("file_size_bytes") or downloaded_bytes or 0),
                "mime_type": uploaded.get("mime_type") or content_type or "video/mp4",
                "completed_at": _utc_now_iso(),
                "error_code": None,
                "error_message": None,
            },
        )
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
                pass


async def _run_workspace_kling3_job(
    *,
    generation_id: str,
    user_id: int,
    mode: str,
    prompt: str,
    duration: int,
    resolution: str,
    aspect_ratio: str,
    enable_audio: bool,
    start_frame: Optional[bytes],
    end_frame: Optional[bytes],
) -> None:
    created = await create_kling3_task(
        prompt=prompt,
        duration=duration,
        resolution=resolution,
        enable_audio=enable_audio,
        aspect_ratio=aspect_ratio,
        prefer_multi_shots=(mode == "multi_shot"),
        start_image_bytes=start_frame,
        end_image_bytes=end_frame,
    )
    provider_task_id = None
    if isinstance(created, dict):
        provider_task_id = ((created.get("data") or {}).get("task_id") or created.get("task_id") or "")
    if not provider_task_id:
        raise RuntimeError("Kling task_id missing in provider response")
    _update_workspace_generation(generation_id, {"task_id": str(provider_task_id), "status": "processing"})

    while True:
        task = await get_kling3_task(str(provider_task_id))
        normalized = _normalize_kling3_task(task if isinstance(task, dict) else {"raw": task})
        provider_url = _first_nonempty(normalized.get("video_url"), normalized.get("download_url"), normalized.get("output_url"))
        patch: Dict[str, Any] = {"status": _db_generation_status(normalized.get("status"))}
        if provider_url:
            patch["provider_video_url"] = provider_url
        if normalized.get("error_message"):
            patch["error_message"] = str(normalized.get("error_message"))[:4000]
        _update_workspace_generation(generation_id, patch)

        if normalized.get("status") == "failed":
            raise RuntimeError(normalized.get("error_message") or "Kling task failed")
        if provider_url and normalized.get("finished"):
            await _finalize_workspace_generation_from_url(
                generation_id=generation_id,
                user_id=user_id,
                provider_video_url=provider_url,
            )
            return
        await asyncio.sleep(5.0)


async def _run_workspace_video_job(
    *,
    generation_id: str,
    user_id: int,
    provider: str,
    model: str,
    mode: str,
    prompt: str,
    duration: int,
    resolution: str,
    aspect_ratio: str,
    enable_audio: bool,
    quality: str,
    start_frame: Optional[bytes],
    end_frame: Optional[bytes],
    last_frame: Optional[bytes],
    avatar_image: Optional[bytes],
    motion_video: Optional[bytes],
    reference_images: List[bytes],
) -> None:
    try:
        provider_video_url: Optional[str] = None

        if provider == "kling":
            if model == "kling-3.0":
                await _run_workspace_kling3_job(
                    generation_id=generation_id,
                    user_id=user_id,
                    mode=mode,
                    prompt=prompt,
                    duration=duration,
                    resolution=resolution,
                    aspect_ratio=aspect_ratio,
                    enable_audio=enable_audio,
                    start_frame=start_frame,
                    end_frame=end_frame,
                )
                return

            if model == "motion-control":
                if not avatar_image or not motion_video:
                    raise RuntimeError("Для Motion Control нужны avatar_image и motion_video")
                provider_video_url = await run_motion_control_from_bytes(
                    user_id=user_id,
                    avatar_bytes=avatar_image,
                    motion_video_bytes=motion_video,
                    prompt=prompt,
                    mode=("std" if quality == "standard" else "pro"),
                )
            elif mode == "text_to_video":
                provider_video_url = await run_text_to_video_from_prompt(
                    user_id=user_id,
                    prompt=prompt,
                    duration_seconds=duration,
                    aspect_ratio=aspect_ratio,
                    model_slug=(REPLICATE_KLING_25_TURBO_PRO_MODEL if model == "kling-2.5" else None),
                    product=("kling25" if model == "kling-2.5" else None),
                )
            else:
                if not start_frame:
                    raise RuntimeError("Для Image→Video нужен start_frame")
                provider_video_url = await run_image_to_video_from_bytes(
                    user_id=user_id,
                    start_image_bytes=start_frame,
                    end_image_bytes=end_frame,
                    prompt=prompt,
                    duration_seconds=duration,
                    mode=("std" if quality == "standard" else "pro"),
                    aspect_ratio=aspect_ratio,
                    model_slug=(REPLICATE_KLING_25_TURBO_PRO_MODEL if model == "kling-2.5" else None),
                    product=("kling25" if model == "kling-2.5" else None),
                )

        elif provider == "veo":
            if mode == "text_to_video":
                provider_video_url = await run_veo_text_to_video(
                    user_id=user_id,
                    model=model,
                    prompt=prompt,
                    duration=duration,
                    resolution=resolution,
                    aspect_ratio=aspect_ratio,
                    generate_audio=enable_audio,
                    tier=("pro" if model == "veo-3.1-pro" else "fast"),
                )
            else:
                if not start_frame:
                    raise RuntimeError("Для Veo Image→Video нужен start_frame")
                provider_video_url = await run_veo_image_to_video(
                    user_id=user_id,
                    model=model,
                    image_bytes=start_frame,
                    prompt=prompt,
                    duration=duration,
                    resolution=resolution,
                    aspect_ratio=aspect_ratio,
                    generate_audio=enable_audio,
                    tier=("pro" if model == "veo-3.1-pro" else "fast"),
                    last_frame_bytes=last_frame,
                    reference_images_bytes=(reference_images or None),
                )

        elif provider == "seedance":
            task_type = "seedance-2-fast-preview" if model == "seedance-fast" else "seedance-2-preview"
            image_urls = await _upload_reference_images_to_public_urls(user_id, reference_images[:9], "seedance") if reference_images else None
            created = await _piapi_seedance_create_task_workspace(
                task_type=task_type,
                prompt=prompt,
                duration=duration,
                aspect_ratio=aspect_ratio,
                image_urls=image_urls if mode == "image_to_video" else None,
            )
            provider_task_id = ((created.get("data") or {}).get("task_id") or created.get("task_id") or "")
            if provider_task_id:
                _update_workspace_generation(generation_id, {"task_id": str(provider_task_id), "status": "processing"})
                done = await _piapi_seedance_wait_workspace(str(provider_task_id))
            else:
                done = created
            status = _seedance_status_lower_workspace(done)
            if status == "failed":
                raise RuntimeError(str(((done.get("data") or {}).get("error") or {}).get("message") or "Seedance task failed"))
            provider_video_url = _seedance_extract_output_url_workspace(done)
            if not provider_video_url:
                raise RuntimeError("Seedance output video url missing")

        elif provider == "sora":
            created = await _sora_create_video_workspace(
                prompt=prompt,
                duration=duration,
                aspect_ratio=aspect_ratio,
                model=model or "sora-2",
            )
            provider_task_id = str(created.get("id") or "").strip()
            if not provider_task_id:
                raise RuntimeError(f"OpenAI did not return video id: {created}")
            _update_workspace_generation(
                generation_id,
                {
                    "task_id": provider_task_id,
                    "status": "processing",
                },
            )
            done = await _sora_poll_video_workspace(provider_task_id)
            status = str(done.get("status") or "").strip().lower()
            if status == "failed":
                error_message = ((done.get("error") or {}).get("message")) or "Sora generation failed"
                raise RuntimeError(error_message)
            if status != "completed":
                raise RuntimeError(f"Unexpected Sora status: {status}")
            video_bytes = await _sora_download_video_workspace(provider_task_id)
            await _finalize_workspace_generation_from_bytes(
                generation_id=generation_id,
                user_id=user_id,
                video_bytes=video_bytes,
                provider_video_url=f"{OPENAI_API_BASE}/videos/{provider_task_id}/content",
                content_type="video/mp4",
            )
            return

        else:
            raise RuntimeError(f"Провайдер {provider} пока не поддержан в workspace video run")

        if not provider_video_url:
            raise RuntimeError("Provider did not return video url")

        await _finalize_workspace_generation_from_url(
            generation_id=generation_id,
            user_id=user_id,
            provider_video_url=provider_video_url,
        )
    except (Kling3Error, KlingFlowError, VeoFlowError, ValueError, RuntimeError, TimeoutError) as e:
        _mark_workspace_generation_failed(generation_id, str(e), error_code="provider_error")
    except Exception as e:
        _mark_workspace_generation_failed(generation_id, f"Internal run error: {e}", error_code="internal_error")



class TelegramAuthPayload(BaseModel):
    auth_data: Dict[str, Any]


class EmailAuthStartPayload(BaseModel):
    email: str = Field(..., min_length=5, max_length=200)
    password: str = Field(..., min_length=6, max_length=200)


class EmailAuthConfirmPayload(BaseModel):
    email: str = Field(..., min_length=5, max_length=200)
    code: str = Field(..., min_length=4, max_length=12)


class EmailLoginPayload(BaseModel):
    email: str = Field(..., min_length=5, max_length=200)
    password: str = Field(..., min_length=6, max_length=200)


class EmailOnlyPayload(BaseModel):
    email: str = Field(..., min_length=5, max_length=200)


class ResetPasswordConfirmPayload(BaseModel):
    email: str = Field(..., min_length=5, max_length=200)
    code: str = Field(..., min_length=4, max_length=12)
    password: str = Field(..., min_length=6, max_length=200)


class ChangePasswordPayload(BaseModel):
    current_password: str = Field(..., min_length=6, max_length=200)
    new_password: str = Field(..., min_length=6, max_length=200)


class ChatTurn(BaseModel):
    role: str
    content: str = Field(..., min_length=1, max_length=8000)


class WorkspaceChatIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=12000)
    history: Optional[List[ChatTurn]] = None
    model: Optional[str] = None
    mode: str = Field(default="chat", pattern="^(chat|prompt_builder)$")
    temperature: float = Field(default=0.6, ge=0.0, le=1.5)
    max_tokens: int = Field(default=900, ge=150, le=4000)


class WorkspaceKlingCreateIn(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=4000)
    duration: int = Field(..., ge=3, le=15)
    resolution: str = Field(..., pattern="^(720|1080)$")
    enable_audio: bool = False
    aspect_ratio: str = Field(default="16:9", pattern="^(16:9|9:16|1:1)$")


class TTSGenerateIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=3000)
    voice_id: str = Field(..., min_length=10, max_length=64)
    model_id: str = Field(default="eleven_multilingual_v2")
    output_format: str = Field(default="mp3_44100_128")
    language_code: Optional[str] = Field(default=None, max_length=8)
    manual_voice_settings: bool = False
    stability: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    similarity_boost: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    style: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    speed: Optional[float] = Field(default=None, ge=0.7, le=1.2)
    use_speaker_boost: Optional[bool] = None


class SongwriterPayload(BaseModel):
    text: str = Field("", description="Current user message")
    history: Optional[List[ChatTurn]] = None
    language: Optional[str] = None
    genre: Optional[str] = None
    mood: Optional[str] = None
    references: Optional[str] = None


class MusicGenerateIn(BaseModel):
    ai: str = Field(default="suno", max_length=24)
    backend: str = Field(default="sunoapi", max_length=24)
    mode: str = Field(default="idea", max_length=24)
    model: str = Field(default="V4_5", max_length=24)
    title: Optional[str] = Field(default="", max_length=200)
    tags: Optional[str] = Field(default="", max_length=1000)
    language: Optional[str] = Field(default="ru", max_length=32)
    mood: Optional[str] = Field(default="", max_length=200)
    references: Optional[str] = Field(default="", max_length=1000)
    negative_tags: Optional[str] = Field(default="", max_length=1000)
    vocal_gender: Optional[str] = Field(default="", max_length=8)
    style_weight: Optional[float] = Field(default=0.65, ge=0.0, le=1.0)
    weirdness_constraint: Optional[float] = Field(default=0.65, ge=0.0, le=1.0)
    audio_weight: Optional[float] = Field(default=0.65, ge=0.0, le=1.0)
    persona_id: Optional[str] = Field(default="", max_length=200)
    persona_model: Optional[str] = Field(default="style_persona", max_length=32)
    instrumental: bool = False
    idea_text: Optional[str] = Field(default="", max_length=8000)
    lyrics_text: Optional[str] = Field(default="", max_length=12000)


class MusicLyricsGenerateIn(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=200)


class MusicTimestampLyricsIn(BaseModel):
    task_id: str = Field(..., min_length=1, max_length=255)
    audio_id: str = Field(..., min_length=1, max_length=255)


class MusicPersonaGenerateIn(BaseModel):
    task_id: str = Field(..., min_length=1, max_length=255)
    audio_id: str = Field(..., min_length=1, max_length=255)
    name: str = Field(..., min_length=1, max_length=120)
    description: str = Field(..., min_length=1, max_length=1000)
    vocal_start: float = Field(default=0.0, ge=0.0)
    vocal_end: float = Field(default=30.0, ge=0.0)
    style: Optional[str] = Field(default="", max_length=300)


class MusicExtendIn(BaseModel):
    audio_id: str = Field(..., min_length=1, max_length=255)
    model: str = Field(default="V4_5", max_length=24)
    use_custom_params: bool = True
    continue_at: Optional[float] = Field(default=60.0, ge=0.0)
    prompt: Optional[str] = Field(default="", max_length=5000)
    title: Optional[str] = Field(default="", max_length=200)
    style: Optional[str] = Field(default="", max_length=1000)
    negative_tags: Optional[str] = Field(default="", max_length=1000)
    vocal_gender: Optional[str] = Field(default="", max_length=8)
    style_weight: Optional[float] = Field(default=0.65, ge=0.0, le=1.0)
    weirdness_constraint: Optional[float] = Field(default=0.65, ge=0.0, le=1.0)
    audio_weight: Optional[float] = Field(default=0.65, ge=0.0, le=1.0)
    persona_id: Optional[str] = Field(default="", max_length=200)
    persona_model: Optional[str] = Field(default="style_persona", max_length=32)


class WorkspaceVideoTrimIn(BaseModel):
    enabled: bool = False
    start_sec: float = Field(default=0.0, ge=0.0)
    end_sec: float = Field(default=0.0, ge=0.0)


class WorkspaceVideoOriginalAudioIn(BaseModel):
    mute: bool = False
    volume: int = Field(default=100, ge=0, le=100)


class WorkspaceVideoAudioClipIn(BaseModel):
    upload_id: str = Field(..., min_length=1, max_length=128)
    audio_start: float = Field(..., ge=0.0)
    audio_end: float = Field(..., ge=0.0)
    video_start: float = Field(..., ge=0.0)
    volume: int = Field(default=100, ge=0, le=100)


class WorkspaceVideoMergeItemIn(BaseModel):
    type: str = Field(..., pattern="^(generation|upload)$")
    id: str = Field(..., min_length=1, max_length=128)


class WorkspaceVideoTimelineIn(BaseModel):
    trim: WorkspaceVideoTrimIn = Field(default_factory=WorkspaceVideoTrimIn)
    original_audio: WorkspaceVideoOriginalAudioIn = Field(default_factory=WorkspaceVideoOriginalAudioIn)
    audio_clips: List[WorkspaceVideoAudioClipIn] = Field(default_factory=list)
    merge_items: List[WorkspaceVideoMergeItemIn] = Field(default_factory=list)


class WorkspaceVideoEditIn(BaseModel):
    source_generation_id: str = Field(..., min_length=1, max_length=128)
    timeline: WorkspaceVideoTimelineIn = Field(default_factory=WorkspaceVideoTimelineIn)


@router.get("/health")
async def workspace_health() -> Dict[str, Any]:
    return {"ok": True, "service": "workspace"}


@router.get("/bootstrap")
async def workspace_bootstrap(user: Optional[Dict[str, Any]] = Depends(get_optional_workspace_user)) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "ok": True,
        "chat_models": _chat_models(),
        "live_integrations": ["workspace_chat", "balance", "kling3", "tts", "songwriter", "prompts"],
        "auth_required": True,
    }
    if user:
        uid = int(user["telegram_user_id"])
        ensure_user_row(uid)
        payload["user"] = _workspace_user_payload(user)
        payload["balance_tokens"] = int(get_balance(uid) or 0)
    return payload




def _workspace_detect_image_ext(raw: Optional[bytes] = None, filename: Optional[str] = None, default: str = "jpg") -> str:
    if isinstance(raw, (bytes, bytearray)):
        head = bytes(raw[:16])
        if head.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png"
        if head.startswith(b"\xff\xd8\xff"):
            return "jpg"
        if head.startswith(b"RIFF") and bytes(raw[8:12]) == b"WEBP":
            return "webp"
    suffix = Path(str(filename or "")).suffix.lstrip(".").lower()
    if suffix == "jpeg":
        return "jpg"
    if suffix in {"jpg", "png", "webp"}:
        return suffix
    return default


def _workspace_ark_size(size: Optional[str]) -> str:
    value = str(size or "").strip().upper()
    if value in {"1K", "2K", "4K"}:
        return value
    return (os.getenv("ARK_SIZE_DEFAULT", "2K") or "2K").strip()


def _workspace_image_input_path(user_id: int, slot: str, ext: str) -> str:
    dt = datetime.now(timezone.utc)
    safe_ext = (ext or "jpg").strip().lower()
    if safe_ext not in {"jpg", "jpeg", "png", "webp"}:
        safe_ext = "jpg"
    safe_slot = re.sub(r"[^a-z0-9_-]+", "_", str(slot or "source").strip().lower()).strip("_") or "source"
    return f"workspace_image_inputs/{int(user_id)}/{dt:%Y/%m/%d}/{safe_slot}_{uuid4().hex}.{safe_ext}"


def _upload_workspace_input_image(user_id: int, raw: bytes, *, filename: Optional[str], slot: str) -> str:
    ext = _workspace_detect_image_ext(raw, filename=filename)
    path = _workspace_image_input_path(user_id, slot, ext)
    return upload_bytes_to_supabase(path, raw, _workspace_image_content_type(ext))


def _supabase_public_object_url(bucket: str, path: str) -> str:
    base = (os.getenv("SUPABASE_URL", "") or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("SUPABASE_URL is not set")
    return f"{base}/storage/v1/object/public/{bucket}/{path.lstrip('/')}"


async def _upload_workspace_topaz_input_image(user_id: int, raw: bytes, *, filename: Optional[str], slot: str) -> str:
    supabase_url = (os.getenv("SUPABASE_URL", "") or "").strip().rstrip("/")
    supabase_service_key = (os.getenv("SUPABASE_SERVICE_KEY", "") or "").strip()
    if not supabase_url or not supabase_service_key:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY are not set")

    ext = _workspace_detect_image_ext(raw, filename=filename)
    safe_slot = re.sub(r"[^a-z0-9_-]+", "_", str(slot or "source").strip().lower()).strip("_") or "source"
    dt = datetime.now(timezone.utc)
    object_key = f"{TOPAZ_INPUT_PREFIX}/{int(user_id)}/{dt:%Y/%m/%d}/{safe_slot}_{uuid4().hex}.{ext}"

    put_url = f"{supabase_url}/storage/v1/object/{TOPAZ_PUBLIC_BUCKET}/{object_key}"
    headers = {
        "authorization": f"Bearer {supabase_service_key}",
        "apikey": supabase_service_key,
        "x-upsert": "true",
        "content-type": _workspace_image_content_type(ext),
    }

    timeout = httpx.Timeout(connect=20.0, read=120.0, write=120.0, pool=120.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.put(put_url, headers=headers, content=raw)
        if resp.status_code >= 300:
            raise RuntimeError(f"Topaz input upload failed: {resp.status_code} {resp.text[:400]}")

    return _supabase_public_object_url(TOPAZ_PUBLIC_BUCKET, object_key)


def _is_workspace_retryable_topaz_error(exc: Exception) -> bool:
    message = str(exc or "").lower()
    markers = (
        "connection reset by peer",
        "[errno 104]",
        "server disconnected",
        "remoteprotocolerror",
        "read error",
        "write error",
        "transport error",
        "temporarily unavailable",
        "connection aborted",
        "connection refused",
        "connect timeout",
        "read timeout",
        "pool timeout",
    )
    if any(marker in message for marker in markers):
        return True

    cause = getattr(exc, "__cause__", None)
    if cause and cause is not exc:
        return _is_workspace_retryable_topaz_error(cause)

    ctx = getattr(exc, "__context__", None)
    if ctx and ctx is not exc:
        return _is_workspace_retryable_topaz_error(ctx)

    return False


async def _run_workspace_topaz_with_retry(params: TopazImageParams):
    last_exc: Optional[Exception] = None
    for attempt in range(1, TOPAZ_IMAGE_CREATE_RETRIES + 1):
        try:
            return await run_topaz_image_upscale(params)
        except Exception as exc:
            last_exc = exc
            if attempt >= TOPAZ_IMAGE_CREATE_RETRIES or not _is_workspace_retryable_topaz_error(exc):
                raise
            await asyncio.sleep(TOPAZ_IMAGE_RETRY_DELAY_SEC * attempt)
    if last_exc:
        raise last_exc
    raise RuntimeError("Topaz upscale failed without exception")


def _strip_workspace_image_optional_columns(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    return {key: value for key, value in payload.items() if key not in _WORKSPACE_IMAGE_OPTIONAL_COLUMNS}


async def _download_workspace_image_bytes(url: str) -> tuple[bytes, str]:
    target_url = str(url or "").strip()
    if not target_url:
        raise RuntimeError("Missing image url")

    timeout = httpx.Timeout(connect=20.0, read=300.0, write=60.0, pool=60.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(target_url)
        resp.raise_for_status()
        raw = resp.content or b""
        if not raw:
            raise RuntimeError("Downloaded image is empty")
        content_type = (resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()

    ext = _workspace_detect_image_ext(raw, filename=urlparse(target_url).path, default="jpg")
    if content_type == "image/png":
        ext = "png"
    elif content_type == "image/webp":
        ext = "webp"
    elif content_type in {"image/jpeg", "image/jpg"}:
        ext = "jpg"
    return raw, ext


def _serialize_workspace_image_generation(row: Dict[str, Any]) -> Dict[str, Any]:
    image_url = _first_nonempty(row.get("download_url"), row.get("image_url"), row.get("after_image_url"))
    before_image_url = _first_nonempty(row.get("before_image_url"), row.get("source_image_url"))
    after_image_url = _first_nonempty(row.get("after_image_url"), image_url)
    compare_mode = bool(row.get("compare_mode")) and bool(before_image_url and after_image_url)
    return {
        "id": row.get("id"),
        "user_id": row.get("user_id"),
        "provider": row.get("provider"),
        "model": row.get("model"),
        "mode": row.get("mode"),
        "prompt": row.get("prompt"),
        "status": row.get("status"),
        "resolution": row.get("resolution"),
        "aspect_ratio": row.get("aspect_ratio"),
        "safety_level": row.get("safety_level"),
        "poster_style": row.get("poster_style"),
        "style_preset": row.get("style_preset"),
        "mood_preset": row.get("mood_preset"),
        "preset_slug": row.get("preset_slug"),
        "source_image_url": row.get("source_image_url"),
        "before_image_url": before_image_url,
        "after_image_url": after_image_url,
        "compare_mode": compare_mode,
        "storage_path": row.get("storage_path"),
        "image_url": image_url,
        "download_url": image_url,
        "file_size_bytes": row.get("file_size_bytes"),
        "mime_type": row.get("mime_type"),
        "error_code": row.get("error_code"),
        "error_message": row.get("error_message"),
        "origin": row.get("origin"),
        "is_favorite": row.get("is_favorite"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "completed_at": row.get("completed_at"),
        "has_storage_file": bool(str(row.get("storage_path") or "").strip() or image_url),
    }


def _workspace_voice_ext(output_format: Optional[str]) -> str:
    value = str(output_format or "").strip().lower()
    if value.startswith("mp3"):
        return "mp3"
    return "bin"


def _workspace_voice_content_type(output_format: Optional[str]) -> str:
    value = str(output_format or "").strip().lower()
    if value.startswith("mp3"):
        return "audio/mpeg"
    return "application/octet-stream"


def _workspace_voice_output_path(user_id: int, ext: str) -> str:
    dt = datetime.now(timezone.utc)
    safe_ext = (ext or "mp3").strip().lower() or "mp3"
    return f"workspace_voice/{int(user_id)}/{dt:%Y/%m/%d}/{uuid4().hex}.{safe_ext}"


def _serialize_workspace_voice_generation(row: Dict[str, Any]) -> Dict[str, Any]:
    audio_url = _first_nonempty(row.get("download_url"), row.get("audio_url"))
    return {
        "id": row.get("id"),
        "user_id": row.get("user_id"),
        "provider": row.get("provider"),
        "model": row.get("model"),
        "voice_id": row.get("voice_id"),
        "voice_name": row.get("voice_name"),
        "text": row.get("text"),
        "status": row.get("status"),
        "output_format": row.get("output_format"),
        "audio_url": audio_url,
        "download_url": audio_url,
        "storage_path": row.get("storage_path"),
        "file_size_bytes": row.get("file_size_bytes"),
        "mime_type": row.get("mime_type"),
        "error_code": row.get("error_code"),
        "error_message": row.get("error_message"),
        "origin": row.get("origin"),
        "is_favorite": row.get("is_favorite"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "completed_at": row.get("completed_at"),
        "has_storage_file": bool(str(row.get("storage_path") or "").strip() or audio_url),
    }


def _insert_workspace_voice_generation(row: Dict[str, Any]) -> str:
    generation_id = str(row.get("id") or uuid4())
    payload = dict(row)
    payload["id"] = generation_id
    if supabase is None:
        return generation_id
    resp = supabase.table(_WORKSPACE_VOICE_GENERATIONS_TABLE).insert(payload).execute()
    data = getattr(resp, "data", None) or []
    if data and isinstance(data[0], dict):
        saved_id = data[0].get("id")
        if saved_id:
            return str(saved_id)
    return generation_id


def _update_workspace_voice_generation(generation_id: Optional[str], patch: Dict[str, Any]) -> None:
    if not generation_id or not patch or supabase is None:
        return
    supabase.table(_WORKSPACE_VOICE_GENERATIONS_TABLE).update(patch).eq("id", str(generation_id)).execute()


def _mark_workspace_voice_generation_failed(generation_id: Optional[str], error_message: str, error_code: Optional[str] = None) -> None:
    if not generation_id:
        return
    patch: Dict[str, Any] = {
        "status": "failed",
        "error_message": (error_message or "").strip()[:4000] or "Unknown error",
        "completed_at": _utc_now_iso(),
    }
    if error_code:
        patch["error_code"] = str(error_code)[:255]
    try:
        _update_workspace_voice_generation(generation_id, patch)
    except Exception:
        pass


def _insert_workspace_image_generation(row: Dict[str, Any]) -> str:
    generation_id = str(row.get("id") or uuid4())
    payload = dict(row)
    payload["id"] = generation_id
    if supabase is None:
        return generation_id
    try:
        resp = supabase.table(_WORKSPACE_IMAGE_GENERATIONS_TABLE).insert(payload).execute()
    except Exception:
        fallback_payload = _strip_workspace_image_optional_columns(payload)
        if fallback_payload == payload:
            raise
        resp = supabase.table(_WORKSPACE_IMAGE_GENERATIONS_TABLE).insert(fallback_payload).execute()
    data = getattr(resp, "data", None) or []
    if data and isinstance(data[0], dict):
        saved_id = data[0].get("id")
        if saved_id:
            return str(saved_id)
    return generation_id


def _update_workspace_image_generation(generation_id: Optional[str], patch: Dict[str, Any]) -> None:
    if not generation_id or not patch or supabase is None:
        return
    try:
        supabase.table(_WORKSPACE_IMAGE_GENERATIONS_TABLE).update(patch).eq("id", str(generation_id)).execute()
    except Exception:
        fallback_patch = _strip_workspace_image_optional_columns(patch)
        if fallback_patch == patch:
            raise
        if fallback_patch:
            supabase.table(_WORKSPACE_IMAGE_GENERATIONS_TABLE).update(fallback_patch).eq("id", str(generation_id)).execute()


def _mark_workspace_image_generation_failed(generation_id: Optional[str], error_message: str, error_code: Optional[str] = None) -> None:
    if not generation_id:
        return
    patch: Dict[str, Any] = {
        "status": "failed",
        "error_message": (error_message or "").strip()[:4000] or "Unknown error",
        "completed_at": _utc_now_iso(),
    }
    if error_code:
        patch["error_code"] = str(error_code)[:255]
    try:
        _update_workspace_image_generation(generation_id, patch)
    except Exception:
        pass


def _workspace_image_output_path(user_id: int, ext: str) -> str:
    dt = datetime.now(timezone.utc)
    safe_ext = (ext or "jpg").strip().lower()
    if safe_ext not in {"jpg", "jpeg", "png", "webp"}:
        safe_ext = "jpg"
    return f"workspace_images/{int(user_id)}/{dt:%Y/%m/%d}/{uuid4().hex}.{safe_ext}"


def _workspace_image_content_type(ext: str) -> str:
    value = (ext or "jpg").strip().lower()
    if value == "png":
        return "image/png"
    if value == "webp":
        return "image/webp"
    return "image/jpeg"


async def _read_optional_upload_bytes(upload: Any) -> Optional[bytes]:
    if not upload or not getattr(upload, "filename", None):
        return None
    raw = await upload.read()
    return raw or None


def _compose_workspace_pair_image(base_bytes: bytes, source_bytes: bytes) -> bytes:
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(base_bytes)) as base_img, Image.open(io.BytesIO(source_bytes)) as source_img:
        base_rgb = ImageOps.exif_transpose(base_img).convert("RGB")
        source_rgb = ImageOps.exif_transpose(source_img).convert("RGB")
        slot_w = 1024
        slot_h = 1024
        left = ImageOps.contain(base_rgb, (slot_w, slot_h), method=Image.Resampling.LANCZOS)
        right = ImageOps.contain(source_rgb, (slot_w, slot_h), method=Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (slot_w * 2, slot_h), (11, 16, 28))
        left_x = (slot_w - left.width) // 2
        left_y = (slot_h - left.height) // 2
        right_x = slot_w + (slot_w - right.width) // 2
        right_y = (slot_h - right.height) // 2
        canvas.paste(left, (left_x, left_y))
        canvas.paste(right, (right_x, right_y))
        out = io.BytesIO()
        canvas.save(out, format="JPEG", quality=95)
        return out.getvalue()


def _build_workspace_image_prompt(
    *,
    provider: str,
    mode: str,
    prompt: str,
    poster_style: str,
    style_preset: str,
    mood_preset: str,
) -> str:
    base = str(prompt or "").strip()
    if provider == "posters" and mode == "poster":
        style = str(poster_style or "cinematic").strip()
        prefix = f"Create a polished promotional poster design in {style} style"
        return f"{prefix}. {base}" if base else prefix
    if provider == "photosession":
        parts = ["Create a high-end AI photosession result"]
        if style_preset:
            parts.append(f"style: {style_preset}")
        if mood_preset:
            parts.append(f"mood: {mood_preset}")
        if base:
            parts.append(base)
        return ". ".join(parts)
    if provider == "two_images":
        prefix = (
            "Use the uploaded collage where the left image is the base/reference and the right image is the source/style image. "
            "Combine important traits from both into one coherent final image."
        )
        return f"{prefix} {base}".strip()
    return base


def _workspace_image_cost(provider: str, mode: str, preset_slug: str = "") -> int:
    provider_key = str(provider or "").strip().lower()
    mode_key = str(mode or "").strip().lower()
    preset_key = str(preset_slug or "").strip().lower()

    if provider_key == "nano_banana":
        return 1
    if provider_key == "nano_banana_pro":
        return 2
    if provider_key == "photosession":
        return 1
    if provider_key == "two_images":
        return 1
    if provider_key == "topaz_photo":
        try:
            return int(get_photo_preset_tokens(preset_key or "standard"))
        except Exception:
            return int(get_photo_preset_tokens("standard"))
    if provider_key == "posters":
        return 0
    if provider_key == "text_to_image":
        return 0

    raise HTTPException(status_code=400, detail=f"Unsupported image provider: {provider_key} / {mode_key}")


def _workspace_image_charge_reason(provider: str, mode: str) -> Optional[str]:
    provider_key = str(provider or "").strip().lower()
    mode_key = str(mode or "").strip().lower()

    if provider_key == "nano_banana":
        return "nano_banana"
    if provider_key == "nano_banana_pro":
        return "nano_banana_pro"
    if provider_key == "photosession":
        return "photosession_generation"
    if provider_key == "two_images":
        return "two_photos"
    if provider_key == "topaz_photo":
        return "workspace_topaz_photo"

    return None

@router.post("/auth/telegram")
async def workspace_auth_telegram(payload: TelegramAuthPayload) -> Dict[str, Any]:
    try:
        verified = validate_telegram_login_data(payload.auth_data)
    except TelegramWebAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))

    uid = int(verified["id"])
    existing = _find_existing_bot_user(uid)
    if existing is None:
        raise HTTPException(status_code=403, detail="Пользователь не найден в AstraBot. Сначала открой Telegram-бота и запусти его хотя бы один раз.")

    account = get_or_create_workspace_account_for_telegram(verified, existing)
    token_user = account_to_workspace_user_payload(account)
    access_token = create_access_token(user=token_user)
    return {
        "ok": True,
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": WORKSPACE_SESSION_TTL_SEC,
        "user": token_user,
        "balance_tokens": int(get_balance(int(account["id"])) or 0),
    }


@router.post("/auth/email-register/start")
async def workspace_auth_email_register_start(payload: EmailAuthStartPayload) -> Dict[str, Any]:
    try:
        start_email_registration(payload.email, payload.password)
        return {"ok": True, "message": "Код отправлен на почту."}
    except (WorkspaceAccountError, WorkspaceMailerError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/auth/email-register/confirm")
async def workspace_auth_email_register_confirm(payload: EmailAuthConfirmPayload) -> Dict[str, Any]:
    try:
        account = confirm_email_registration(payload.email, payload.code)
    except (WorkspaceCodeExpired, WorkspaceCodeTooManyAttempts, WorkspaceAccountError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    token_user = account_to_workspace_user_payload(account)
    access_token = create_access_token(user=token_user)
    return {
        "ok": True,
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": WORKSPACE_SESSION_TTL_SEC,
        "user": token_user,
        "balance_tokens": int(get_balance(int(account["id"])) or 0),
    }


@router.post("/auth/email-login")
async def workspace_auth_email_login(payload: EmailLoginPayload) -> Dict[str, Any]:
    try:
        account = login_with_email(payload.email, payload.password)
    except WorkspaceAuthFailed as e:
        raise HTTPException(status_code=401, detail=str(e))
    except WorkspaceAccountError as e:
        raise HTTPException(status_code=400, detail=str(e))

    token_user = account_to_workspace_user_payload(account)
    access_token = create_access_token(user=token_user)
    return {
        "ok": True,
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": WORKSPACE_SESSION_TTL_SEC,
        "user": token_user,
        "balance_tokens": int(get_balance(int(account["id"])) or 0),
    }




@router.post("/auth/password-reset/start")
async def workspace_auth_password_reset_start(payload: EmailOnlyPayload) -> Dict[str, Any]:
    try:
        start_password_reset(payload.email)
        return {"ok": True, "message": "Код для сброса пароля отправлен на почту."}
    except (WorkspaceAccountError, WorkspaceMailerError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/auth/password-reset/confirm")
async def workspace_auth_password_reset_confirm(payload: ResetPasswordConfirmPayload) -> Dict[str, Any]:
    try:
        account = confirm_password_reset(payload.email, payload.code, payload.password)
    except (WorkspaceCodeExpired, WorkspaceCodeTooManyAttempts, WorkspaceAuthFailed, WorkspaceAccountError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    token_user = account_to_workspace_user_payload(account)
    access_token = create_access_token(user=token_user)
    return {
        "ok": True,
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": WORKSPACE_SESSION_TTL_SEC,
        "user": token_user,
        "balance_tokens": int(get_balance(int(account["id"])) or 0),
    }


@router.post("/account/link-email/start")
async def workspace_account_link_email_start(payload: EmailAuthStartPayload, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    account = ensure_workspace_account_from_claims(user)
    try:
        start_link_email(account_id=int(account["id"]), email=payload.email, password=payload.password)
        return {"ok": True, "message": "Код отправлен на почту."}
    except (WorkspaceAccountError, WorkspaceMailerError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/account/link-email/confirm")
async def workspace_account_link_email_confirm(payload: EmailAuthConfirmPayload, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    account = ensure_workspace_account_from_claims(user)
    try:
        account = confirm_link_email(account_id=int(account["id"]), email=payload.email, code=payload.code)
    except (WorkspaceCodeExpired, WorkspaceCodeTooManyAttempts, WorkspaceAccountError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    token_user = account_to_workspace_user_payload(account)
    access_token = create_access_token(user=token_user)
    return {
        "ok": True,
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": WORKSPACE_SESSION_TTL_SEC,
        "user": token_user,
        "balance_tokens": int(get_balance(int(account["id"])) or 0),
    }




@router.post("/account/change-password")
async def workspace_account_change_password(payload: ChangePasswordPayload, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    account = ensure_workspace_account_from_claims(user)
    try:
        account = change_password(account_id=int(account["id"]), current_password=payload.current_password, new_password=payload.new_password)
    except (WorkspaceAuthFailed, WorkspaceAccountError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    token_user = account_to_workspace_user_payload(account)
    access_token = create_access_token(user=token_user)
    return {
        "ok": True,
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": WORKSPACE_SESSION_TTL_SEC,
        "user": token_user,
        "balance_tokens": int(get_balance(int(account["id"])) or 0),
    }


@router.post("/account/link-telegram")
async def workspace_account_link_telegram(payload: TelegramAuthPayload, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    account = ensure_workspace_account_from_claims(user)
    try:
        verified = validate_telegram_login_data(payload.auth_data)
        account = link_telegram_to_account(account_id=int(account["id"]), verified=verified)
    except TelegramWebAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except WorkspaceAccountError as e:
        raise HTTPException(status_code=400, detail=str(e))

    token_user = account_to_workspace_user_payload(account)
    access_token = create_access_token(user=token_user)
    return {
        "ok": True,
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": WORKSPACE_SESSION_TTL_SEC,
        "user": token_user,
        "balance_tokens": int(get_balance(int(account["id"])) or 0),
    }


@router.post("/logout")
async def workspace_logout() -> Dict[str, Any]:
    return {"ok": True}


@router.get("/me")
async def workspace_me(user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    account = ensure_workspace_account_from_claims(user)
    user_payload = account_to_workspace_user_payload(account)
    return {"ok": True, "user": user_payload, "balance_tokens": int(get_balance(int(account["id"])) or 0)}


@router.get("/balance")
async def workspace_balance(user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    uid = int(user.get("workspace_user_id") or user["telegram_user_id"])
    ensure_user_row(uid)
    balance = int(get_balance(uid) or 0)
    return {"ok": True, "balance_tokens": balance}


class WorkspaceTopupCreatePayload(BaseModel):
    tokens: int = Field(..., ge=1, le=100000)
    return_url: Optional[str] = None


@router.get("/topup/packs")
async def workspace_topup_packs() -> Dict[str, Any]:
    return {"ok": True, "packs": WORKSPACE_TOPUP_PACKS}


@router.post("/topup/create")
async def workspace_topup_create(payload: WorkspaceTopupCreatePayload, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    account = ensure_workspace_account_from_claims(user)
    uid = int(account.get("id") or user.get("workspace_user_id") or user.get("telegram_user_id") or 0)
    if uid <= 0:
        raise HTTPException(status_code=400, detail="Не удалось определить пользователя для пополнения.")

    pack = _workspace_find_topup_pack(payload.tokens)
    if not pack:
        raise HTTPException(status_code=400, detail="Неизвестный пакет пополнения.")

    email = str(account.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Для оплаты картой или СБП сначала добавь email в профиле аккаунта.")

    try:
        payment_id, confirmation_url = await create_yookassa_payment(
            amount_rub=int(pack["rub"]),
            description=f'Пополнение баланса: {int(pack["tokens"])} токенов',
            user_id=uid,
            tokens=int(pack["tokens"]),
            customer_email=email,
            return_url=(str(payload.return_url or "").strip() or None),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Не удалось создать платёж: {e}")

    return {
        "ok": True,
        "payment_id": payment_id,
        "confirmation_url": confirmation_url,
        "tokens": int(pack["tokens"]),
        "amount_rub": int(pack["rub"]),
        "customer_email": email,
    }



@router.post("/chat")
async def workspace_chat(request: Request, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    content_type = (request.headers.get("content-type") or "").lower()
    files: List[UploadFile] = []

    if "multipart/form-data" in content_type:
        form = await request.form()
        text_value = str(form.get("text") or "").strip()
        mode = _normalize_chat_mode_value(form.get("mode"))
        history = _sanitize_chat_history(form.get("history"))
        temperature = _clamp_float(form.get("temperature"), 0.6, 0.0, 1.5)
        max_tokens = _clamp_int(form.get("max_tokens"), 900, 150, 4000)
        resolved_model = _resolve_workspace_chat_model(form.get("model"), mode)
        files = [f for f in form.getlist("files") if getattr(f, "filename", None)]
    else:
        payload = WorkspaceChatIn.model_validate(await request.json())
        text_value = payload.text.strip()
        mode = _normalize_chat_mode_value(payload.mode)
        history = [{"role": item.role, "content": item.content} for item in (payload.history or []) if item.role in ("user", "assistant")]
        temperature = payload.temperature
        max_tokens = payload.max_tokens
        resolved_model = _resolve_workspace_chat_model(payload.model, mode)

    mode = "prompt_builder" if resolved_model["label"] == PROMPT_MODEL_LABEL else mode

    if not text_value and not files:
        raise HTTPException(status_code=400, detail="Введите текст или прикрепите хотя бы один файл.")

    prepared_files = await _prepare_workspace_chat_attachments(files) if files else {"items": [], "context": "", "image_bytes_list": []}
    image_refs = [f"@image{i}" for i in range(1, len(prepared_files.get("image_bytes_list") or []) + 1)]

    user_text = text_value or "Проанализируй приложенные файлы и кратко скажи, что в них находится, затем предложи полезные следующие шаги."
    if prepared_files.get("context"):
        user_text = f"{user_text}\n\n{prepared_files['context']}"

    model_label = resolved_model["label"]
    model_actual = resolved_model["actual"]
    if mode == "prompt_builder":
        if not _is_prompt_builder_request(text_value, bool(files)):
            answer = _prompt_builder_redirect_message()
            return {
                "ok": True,
                "answer": answer,
                "mode": mode,
                "model": model_label,
                "resolved_model": model_actual,
                "attachments": prepared_files.get("items") or [],
                "is_prompt": False,
            }
        system_prompt = _build_prompt_builder_system_prompt(model_label, image_refs)
    else:
        system_prompt = (
            "Ты — AstraBot Workspace Assistant. "
            "Помогай как product-minded AI co-pilot: сценарии, промпты, creative direction, тексты, планы и упаковка идей в рабочий пайплайн. "
            "Пиши по делу, понятно и удобно для дальнейшего запуска в video/image/voice/music студиях. "
            f"Если пользователь спрашивает, какая модель выбрана в интерфейсе, отвечай только названием модели: {model_label}."
        )

    answer = await openai_chat_answer(
        user_text=user_text,
        system_prompt=system_prompt,
        history=history,
        temperature=temperature,
        max_tokens=max_tokens,
        model=model_actual,
        image_bytes_list=prepared_files.get("image_bytes_list") or None,
    )
    return {
        "ok": True,
        "answer": answer,
        "mode": mode,
        "model": model_label,
        "resolved_model": model_actual,
        "attachments": prepared_files.get("items") or [],
        "is_prompt": _is_prompt_builder_output(answer) if mode == "prompt_builder" else False,
    }


@router.post("/kling3/create")
async def workspace_kling3_create(payload: WorkspaceKlingCreateIn, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    request_id = str(uuid4())
    generation_id: Optional[str] = None
    tokens_required = 0
    try:
        tokens_required = calculate_kling3_price(
            resolution=payload.resolution,
            enable_audio=payload.enable_audio,
            duration=payload.duration,
        )
        add_tokens(
            uid,
            -tokens_required,
            reason="kling3_create",
            ref_id=request_id,
            meta={
                "duration": payload.duration,
                "resolution": payload.resolution,
                "enable_audio": payload.enable_audio,
                "aspect_ratio": payload.aspect_ratio,
            },
        )

        generation_id = _insert_workspace_generation(
            {
                "user_id": str(uid),
                "provider": "kling",
                "model": "3.0",
                "mode": "text_to_video",
                "prompt": payload.prompt.strip(),
                "status": "processing",
                "aspect_ratio": payload.aspect_ratio,
                "duration_sec": int(payload.duration),
                "resolution": payload.resolution,
                "enable_audio": bool(payload.enable_audio),
                "origin": "workspace",
            }
        )

        task = await create_kling3_task(
            prompt=payload.prompt,
            duration=payload.duration,
            resolution=payload.resolution,
            enable_audio=payload.enable_audio,
            aspect_ratio=payload.aspect_ratio,
        )
        provider_task_id = None
        if isinstance(task, dict):
            provider_task_id = (task.get("data") or {}).get("task_id") or task.get("task_id")

        if generation_id and provider_task_id:
            _update_workspace_generation(
                generation_id,
                {
                    "task_id": str(provider_task_id),
                },
            )

        return {
            "ok": True,
            "generation_id": generation_id,
            "request_id": request_id,
            "tokens_required": tokens_required,
            "provider_task_id": provider_task_id,
            "task": task,
        }
    except (ValueError, Kling3Error) as e:
        _mark_workspace_generation_failed(generation_id, str(e), error_code="provider_error")
        if tokens_required > 0:
            try:
                add_tokens(uid, tokens_required, reason="kling3_refund", ref_id=request_id, meta={"error": str(e)})
            except Exception:
                pass
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _mark_workspace_generation_failed(generation_id, str(e), error_code="internal_error")
        if tokens_required > 0:
            try:
                add_tokens(uid, tokens_required, reason="kling3_refund", ref_id=request_id, meta={"error": str(e)})
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=f"Internal error: {e}")


@router.get("/kling3/task/{task_id}")
async def workspace_kling3_task(task_id: str, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    try:
        task = await get_kling3_task(task_id)
        normalized = _normalize_kling3_task(task if isinstance(task, dict) else {"raw": task})
        try:
            await _sync_workspace_generation_by_task(uid, task_id, normalized)
        except Exception:
            pass
        return {"ok": True, "task": task, "normalized": normalized}
    except (ValueError, Kling3Error) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal error: {e}")


@router.get("/history")
async def workspace_history(
    limit: int = 20,
    offset: int = 0,
    user: Dict[str, Any] = Depends(get_current_workspace_user),
) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    safe_limit = max(1, min(int(limit or 20), 100))
    safe_offset = max(0, int(offset or 0))

    if supabase is None:
        return {"ok": True, "items": [], "limit": safe_limit, "offset": safe_offset}

    try:
        resp = (
            supabase.table(_WORKSPACE_VIDEO_GENERATIONS_TABLE)
            .select(
                "id,user_id,provider,model,mode,task_id,prompt,status,aspect_ratio,duration_sec,resolution,enable_audio,provider_video_url,storage_path,thumbnail_path,file_size_bytes,mime_type,error_code,error_message,origin,is_favorite,created_at,updated_at,completed_at"
            )
            .eq("user_id", str(uid))
            .is_("deleted_at", "null")
            .order("created_at", desc=True)
            .range(safe_offset, safe_offset + safe_limit - 1)
            .execute()
        )
        rows = getattr(resp, "data", None) or []
        items = [_serialize_workspace_generation(row) for row in rows if isinstance(row, dict)]
        return {
            "ok": True,
            "items": items,
            "limit": safe_limit,
            "offset": safe_offset,
            "count": len(items),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"History load failed: {e}")


@router.get("/history/{generation_id}")
async def workspace_history_item(
    generation_id: str,
    user: Dict[str, Any] = Depends(get_current_workspace_user),
) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    generation_id_text = str(generation_id or "").strip()
    if not generation_id_text:
        raise HTTPException(status_code=400, detail="Missing generation_id")

    if supabase is None:
        raise HTTPException(status_code=404, detail="Generation not found")

    try:
        resp = (
            supabase.table(_WORKSPACE_VIDEO_GENERATIONS_TABLE)
            .select(
                "id,user_id,provider,model,mode,task_id,prompt,status,aspect_ratio,duration_sec,resolution,enable_audio,provider_video_url,storage_path,thumbnail_path,file_size_bytes,mime_type,error_code,error_message,origin,is_favorite,created_at,updated_at,completed_at"
            )
            .eq("id", generation_id_text)
            .eq("user_id", str(uid))
            .is_("deleted_at", "null")
            .limit(1)
            .execute()
        )
        rows = getattr(resp, "data", None) or []
        if not rows or not isinstance(rows[0], dict):
            raise HTTPException(status_code=404, detail="Generation not found")
        row = rows[0]
        status_value = str(row.get("status") or "").strip().lower()
        if row.get("provider") == "kling" and str(row.get("model") or "") == "3.0" and row.get("task_id") and status_value in {"queued", "processing"}:
            try:
                task = await get_kling3_task(str(row.get("task_id")))
                normalized = _normalize_kling3_task(task if isinstance(task, dict) else {"raw": task})
                await _sync_workspace_generation_by_task(uid, str(row.get("task_id")), normalized)
                refreshed = (
                    supabase.table(_WORKSPACE_VIDEO_GENERATIONS_TABLE)
                    .select(
                        "id,user_id,provider,model,mode,task_id,prompt,status,aspect_ratio,duration_sec,resolution,enable_audio,provider_video_url,storage_path,thumbnail_path,file_size_bytes,mime_type,error_code,error_message,origin,is_favorite,created_at,updated_at,completed_at"
                    )
                    .eq("id", generation_id_text)
                    .eq("user_id", str(uid))
                    .is_("deleted_at", "null")
                    .limit(1)
                    .execute()
                )
                refreshed_rows = getattr(refreshed, "data", None) or []
                if refreshed_rows and isinstance(refreshed_rows[0], dict):
                    row = refreshed_rows[0]
            except Exception:
                pass
        item = _serialize_workspace_generation(row)
        return {"ok": True, "item": item}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"History item load failed: {e}")


@router.delete("/history/{generation_id}")
async def workspace_history_delete_item(
    generation_id: str,
    user: Dict[str, Any] = Depends(get_current_workspace_user),
) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    generation_id_text = str(generation_id or "").strip()
    if not generation_id_text:
        raise HTTPException(status_code=400, detail="Missing generation_id")

    if supabase is None:
        raise HTTPException(status_code=404, detail="Generation not found")

    try:
        resp = (
            supabase.table(_WORKSPACE_VIDEO_GENERATIONS_TABLE)
            .select("id,user_id,storage_path,thumbnail_path,deleted_at")
            .eq("id", generation_id_text)
            .eq("user_id", str(uid))
            .is_("deleted_at", "null")
            .limit(1)
            .execute()
        )
        rows = getattr(resp, "data", None) or []
        if not rows or not isinstance(rows[0], dict):
            raise HTTPException(status_code=404, detail="Generation not found")
        row = rows[0]

        storage_paths = []
        for key in ("storage_path", "thumbnail_path"):
            value = str(row.get(key) or "").strip()
            if value:
                storage_paths.append(value)
        if storage_paths:
            try:
                supabase.storage.from_(_WORKSPACE_VIDEOS_BUCKET).remove(storage_paths)
            except Exception:
                pass

        now_iso = datetime.now(timezone.utc).isoformat()
        (
            supabase.table(_WORKSPACE_VIDEO_GENERATIONS_TABLE)
            .update({"deleted_at": now_iso, "updated_at": now_iso, "storage_path": None, "thumbnail_path": None})
            .eq("id", generation_id_text)
            .eq("user_id", str(uid))
            .execute()
        )
        return {"ok": True, "generation_id": generation_id_text}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"History delete failed: {e}")


@router.post("/video/run")
async def workspace_video_run(
    request: Request,
    user: Dict[str, Any] = Depends(get_current_workspace_user),
) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    form = await request.form()

    provider = str(form.get("provider") or "").strip().lower()
    model = str(form.get("model") or "").strip()
    mode = str(form.get("mode") or "").strip().lower()
    prompt = str(form.get("prompt") or "").strip()
    duration = _parse_form_int(form.get("duration"), 5)
    aspect_ratio = str(form.get("aspect_ratio") or "16:9").strip() or "16:9"
    resolution = _normalize_workspace_video_resolution(provider, model, form.get("resolution"))
    enable_audio = _parse_form_bool(form.get("enable_audio"))
    quality = str(form.get("quality") or "pro").strip().lower() or "pro"

    if not provider:
        raise HTTPException(status_code=400, detail="Missing provider")
    if not model:
        raise HTTPException(status_code=400, detail="Missing model")
    if not mode:
        raise HTTPException(status_code=400, detail="Missing mode")
    if not prompt and provider != "seedance":
        raise HTTPException(status_code=400, detail="Missing prompt")

    supported = {"kling", "veo", "seedance", "sora"}
    if provider not in supported:
        raise HTTPException(status_code=400, detail=f"Provider {provider} is not supported in /video/run yet")

    start_file = form.get("start_frame")
    end_file = form.get("end_frame")
    last_file = form.get("last_frame")
    avatar_file = form.get("avatar_image")
    motion_file = form.get("motion_video")
    ref_files = [f for f in form.getlist("reference_images") if getattr(f, "filename", None)]

    async def _read_optional(upload: Any) -> Optional[bytes]:
        if not upload or not getattr(upload, "filename", None):
            return None
        data = await upload.read()
        return data or None

    start_frame = await _read_optional(start_file)
    end_frame = await _read_optional(end_file)
    last_frame = await _read_optional(last_file)
    avatar_image = await _read_optional(avatar_file)
    motion_video = await _read_optional(motion_file)
    reference_images: List[bytes] = []
    for rf in ref_files:
        raw = await rf.read()
        if raw:
            reference_images.append(raw)

    if provider == "kling" and mode in {"image_to_video", "multi_shot"} and model in {"kling-1.6", "kling-2.5", "kling-3.0"} and not start_frame:
        raise HTTPException(status_code=400, detail="Для Image→Video нужен start frame.")
    if provider == "veo" and mode == "image_to_video" and not start_frame:
        raise HTTPException(status_code=400, detail="Для Veo Image→Video нужен start frame.")
    if provider == "seedance" and mode == "image_to_video" and not reference_images:
        raise HTTPException(status_code=400, detail="Для Seedance Image→Video нужен хотя бы один reference image.")
    if provider == "sora":
        if mode != "text_to_video":
            raise HTTPException(status_code=400, detail="Sora в workspace сейчас поддерживает только Text→Video.")
        if duration not in {4, 8, 12}:
            raise HTTPException(status_code=400, detail="Для Sora доступны только 4, 8 или 12 секунд.")
        if aspect_ratio not in {"16:9", "9:16"}:
            raise HTTPException(status_code=400, detail="Для Sora доступны только 16:9 или 9:16.")
    if model == "motion-control" and (not avatar_image or not motion_video):
        raise HTTPException(status_code=400, detail="Для Motion Control нужны avatar image и motion video.")

    generation_id = _insert_workspace_generation(
        {
            "user_id": str(uid),
            "provider": provider,
            "model": model,
            "mode": _history_mode_for_run(provider, mode),
            "prompt": prompt,
            "status": "queued",
            "aspect_ratio": aspect_ratio,
            "duration_sec": int(duration or 0),
            "resolution": resolution,
            "enable_audio": bool(enable_audio),
            "origin": "workspace",
        }
    )

    _update_workspace_generation(generation_id, {"status": "processing"})

    asyncio.create_task(
        _run_workspace_video_job(
            generation_id=generation_id,
            user_id=uid,
            provider=provider,
            model=model,
            mode=mode,
            prompt=prompt,
            duration=duration,
            resolution=resolution,
            aspect_ratio=aspect_ratio,
            enable_audio=enable_audio,
            quality=quality,
            start_frame=start_frame,
            end_frame=end_frame,
            last_frame=last_frame,
            avatar_image=avatar_image,
            motion_video=motion_video,
            reference_images=reference_images,
        )
    )

    return {
        "ok": True,
        "generation_id": generation_id,
        "task_id": generation_id,
        "status": "processing",
        "status_text": "Генерация началась. Видео появится в рабочей зоне автоматически.",
    }


@router.post("/video/upload")
async def workspace_video_upload(
    file: UploadFile = File(...),
    user: Dict[str, Any] = Depends(get_current_workspace_user),
) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    try:
        raw = await file.read()
        row = create_workspace_upload_record(
            user_id=uid,
            filename=file.filename or "upload.bin",
            content_type=file.content_type or "",
            raw_bytes=raw,
        )
        return {
            "ok": True,
            "upload_id": row.get("id"),
            "file_type": row.get("file_type"),
            "storage_path": row.get("storage_path"),
            "duration": row.get("duration_sec"),
            "filename": row.get("filename"),
            "video_url": row.get("video_url"),
            "download_url": row.get("download_url"),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload failed: {e}")


@router.post("/video/edit")
async def workspace_video_edit(
    payload: WorkspaceVideoEditIn,
    user: Dict[str, Any] = Depends(get_current_workspace_user),
) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    source_generation_id = str(payload.source_generation_id or "").strip()
    source_row = get_workspace_generation_row(uid, source_generation_id)
    if not source_row:
        raise HTTPException(status_code=404, detail="Исходный ролик не найден.")

    request_payload = payload.model_dump()
    timeline = request_payload.get("timeline") or {}
    audio_clips = timeline.get("audio_clips") or []
    merge_items = timeline.get("merge_items") or []

    if len(audio_clips) > MAX_AUDIO_CLIPS:
        raise HTTPException(status_code=400, detail=f"Максимум {MAX_AUDIO_CLIPS} аудио-куска.")
    if len(merge_items) > MAX_MERGE_ITEMS:
        raise HTTPException(status_code=400, detail=f"Максимум {MAX_MERGE_ITEMS} видео в очереди склейки.")

    trim_cfg = timeline.get("trim") or {}
    if trim_cfg.get("enabled"):
        start_sec = float(trim_cfg.get("start_sec") or 0.0)
        end_sec = float(trim_cfg.get("end_sec") or 0.0)
        if start_sec < 0 or end_sec <= start_sec:
            raise HTTPException(status_code=400, detail="Неверный диапазон trim.")
        if end_sec - start_sec < 0.5:
            raise HTTPException(status_code=400, detail="Минимальная длина результата после trim — 0.5 сек.")
        if (end_sec - start_sec) > MAX_OUTPUT_DURATION_SEC:
            raise HTTPException(status_code=400, detail=f"Итоговое видео не должно превышать {MAX_OUTPUT_DURATION_SEC} сек.")

    upload_ids: List[str] = []
    for clip in audio_clips:
        upload_id = str(clip.get("upload_id") or "").strip()
        row = get_workspace_upload_row(uid, upload_id)
        if not row or str(row.get("file_type") or "") != "audio":
            raise HTTPException(status_code=400, detail=f"Аудиофайл не найден: {upload_id}")
        upload_ids.append(upload_id)

    for item in merge_items:
        item_type = str(item.get("type") or "").strip().lower()
        item_id = str(item.get("id") or "").strip()
        if item_type == "generation":
            row = get_workspace_generation_row(uid, item_id)
            if not row:
                raise HTTPException(status_code=400, detail=f"Видео из библиотеки не найдено: {item_id}")
        elif item_type == "upload":
            row = get_workspace_upload_row(uid, item_id)
            if not row or str(row.get("file_type") or "") != "video":
                raise HTTPException(status_code=400, detail=f"Загруженное видео не найдено: {item_id}")
            upload_ids.append(item_id)
        else:
            raise HTTPException(status_code=400, detail="merge_items содержит неверный type.")

    operation_type = resolve_operation_type(request_payload)
    source_prompt = str(source_row.get("prompt") or "").strip()
    preview_prompt = f"Montage · {source_prompt[:120]}".strip(" ·")

    generation_id = _insert_workspace_generation(
        {
            "user_id": str(uid),
            "provider": "editor",
            "model": "mini-editor-v1",
            "mode": "edit",
            "prompt": preview_prompt or "Montage · Edited video",
            "status": "queued",
            "aspect_ratio": source_row.get("aspect_ratio"),
            "duration_sec": source_row.get("duration_sec"),
            "resolution": source_row.get("resolution"),
            "enable_audio": source_row.get("enable_audio"),
            "origin": "workspace_edit",
            "parent_generation_id": source_generation_id,
            "operation_type": operation_type,
            "operations_json": request_payload,
        }
    )

    job_id = insert_workspace_edit_job_row(
        {
            "user_id": str(uid),
            "source_generation_id": source_generation_id,
            "result_generation_id": generation_id,
            "parent_generation_id": source_generation_id,
            "operation_type": operation_type,
            "payload_json": request_payload,
            "status": "queued",
            "created_at": _utc_now_iso(),
            "updated_at": _utc_now_iso(),
        }
    )

    _update_workspace_generation(
        generation_id,
        {
            "edit_job_id": job_id,
        },
    )

    await enqueue_job({"job_id": job_id, "kind": "workspace_video_edit"}, queue_name=VIDEO_EDIT_QUEUE_NAME)

    return {
        "ok": True,
        "job_id": job_id,
        "generation_id": generation_id,
        "status": "queued",
        "operation_type": operation_type,
    }


@router.get("/video/job/{job_id}")
async def workspace_video_job_status(
    job_id: str,
    user: Dict[str, Any] = Depends(get_current_workspace_user),
) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    row = get_workspace_edit_job_row(uid, str(job_id or "").strip())
    if not row:
        raise HTTPException(status_code=404, detail="Edit job not found.")

    item = None
    generation_id = str(row.get("result_generation_id") or "").strip()
    if generation_id and supabase is not None:
        try:
            resp = (
                supabase.table(_WORKSPACE_VIDEO_GENERATIONS_TABLE)
                .select(
                    "id,user_id,provider,model,mode,task_id,prompt,status,aspect_ratio,duration_sec,resolution,enable_audio,provider_video_url,storage_path,thumbnail_path,file_size_bytes,mime_type,error_code,error_message,origin,is_favorite,created_at,updated_at,completed_at"
                )
                .eq("id", generation_id)
                .eq("user_id", str(uid))
                .limit(1)
                .execute()
            )
            rows = getattr(resp, "data", None) or []
            if rows and isinstance(rows[0], dict):
                item = _serialize_workspace_generation(rows[0])
        except Exception:
            item = None

    return {
        "ok": True,
        "job": {
            "id": row.get("id"),
            "status": row.get("status"),
            "error_message": row.get("error_message"),
            "source_generation_id": row.get("source_generation_id"),
            "result_generation_id": row.get("result_generation_id"),
            "operation_type": row.get("operation_type"),
            "created_at": row.get("created_at"),
            "started_at": row.get("started_at"),
            "completed_at": row.get("completed_at"),
        },
        "item": item,
    }


_WORKSPACE_TTS_ALLOWED_MODELS = {"eleven_multilingual_v2", "eleven_flash_v2_5", "eleven_turbo_v2_5"}
_WORKSPACE_TTS_ALLOWED_FORMATS = {"mp3_44100_128", "mp3_44100_192"}
_WORKSPACE_TTS_ALLOWED_LANGUAGE_CODES = {
    "auto", "ru", "en", "uk", "de", "fr", "es", "it", "pt", "pl", "tr", "ar", "hi", "zh", "ja", "ko"
}


def _workspace_tts_language_code(value: Optional[str]) -> Optional[str]:
    code = str(value or "").strip().lower()
    if not code or code == "auto":
        return None
    if code not in _WORKSPACE_TTS_ALLOWED_LANGUAGE_CODES:
        raise HTTPException(status_code=400, detail="language_code is not allowed")
    return code


def _workspace_tts_voice_settings(payload: TTSGenerateIn) -> Optional[Dict[str, Any]]:
    if not bool(payload.manual_voice_settings):
        return None
    settings: Dict[str, Any] = {}
    if payload.stability is not None:
        settings["stability"] = float(payload.stability)
    if payload.similarity_boost is not None:
        settings["similarity_boost"] = float(payload.similarity_boost)
    if payload.style is not None:
        settings["style"] = float(payload.style)
    if payload.speed is not None:
        settings["speed"] = float(payload.speed)
    if payload.use_speaker_boost is not None:
        settings["use_speaker_boost"] = bool(payload.use_speaker_boost)
    return settings or None




def _normalize_music_ai_value(value: Any) -> str:
    return "udio" if str(value or "").strip().lower() == "udio" else "suno"


def _normalize_music_backend_value(ai: str, value: Any) -> str:
    raw = str(value or "").strip().lower()
    if ai == "udio":
        return "piapi"
    if raw in {"piapi", "sunoapi", "auto"}:
        return raw
    return "sunoapi"


def _normalize_music_mode_value(ai: str, value: Any) -> str:
    raw = str(value or "").strip().lower()
    if ai == "udio":
        return "idea"
    return "lyrics" if raw == "lyrics" else "idea"


def _workspace_music_prompt_text(payload: MusicGenerateIn, ai: str, mode: str) -> str:
    if ai == "suno" and mode == "lyrics":
        return str(payload.lyrics_text or "").strip()
    return str(payload.idea_text or "").strip()


def _workspace_music_cost_tokens(payload: MusicGenerateIn, ai: str, backend: str) -> int:
    return int(WORKSPACE_MUSIC_COST_TOKENS)


def _workspace_music_charge_reason(ai: str, backend: str) -> str:
    return "workspace_music"


def _serialize_workspace_music_track(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row.get("id"),
        "generation_id": row.get("generation_id"),
        "track_index": row.get("track_index"),
        "provider_track_id": row.get("provider_track_id"),
        "title": row.get("title"),
        "audio_url": row.get("audio_url"),
        "video_url": row.get("video_url"),
        "cover_url": row.get("cover_url"),
        "lyrics": row.get("lyrics"),
        "duration_sec": row.get("duration_sec"),
        "payload_json": row.get("payload_json"),
        "created_at": row.get("created_at"),
    }


def _serialize_workspace_music_generation(row: Dict[str, Any], tracks: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    out = {
        "id": row.get("id"),
        "user_id": row.get("user_id"),
        "ai": row.get("ai"),
        "backend": row.get("backend"),
        "mode": row.get("mode"),
        "title": row.get("title"),
        "tags": row.get("tags"),
        "language": row.get("language"),
        "mood": row.get("mood"),
        "references": row.get("references"),
        "idea_text": row.get("idea_text"),
        "lyrics_text": row.get("lyrics_text"),
        "instrumental": bool(row.get("instrumental")),
        "status": row.get("status"),
        "provider_task_id": row.get("provider_task_id"),
        "output_count": int(row.get("output_count") or 0),
        "error_code": row.get("error_code"),
        "error_message": row.get("error_message"),
        "origin": row.get("origin"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "completed_at": row.get("completed_at"),
    }
    if tracks is not None:
        out["tracks"] = tracks
        out["output_count"] = len(tracks)
        if tracks:
            out["first_audio_url"] = tracks[0].get("audio_url")
            out["first_cover_url"] = tracks[0].get("cover_url")
    return out


def _insert_workspace_music_generation(row: Dict[str, Any]) -> str:
    generation_id = str(row.get("id") or uuid4())
    payload = dict(row)
    payload["id"] = generation_id
    if supabase is None:
        return generation_id
    resp = supabase.table(_WORKSPACE_MUSIC_GENERATIONS_TABLE).insert(payload).execute()
    data = getattr(resp, "data", None) or []
    if data and isinstance(data[0], dict) and data[0].get("id"):
        return str(data[0]["id"])
    return generation_id


def _update_workspace_music_generation(generation_id: Optional[str], patch: Dict[str, Any]) -> None:
    if not generation_id or not patch or supabase is None:
        return
    safe_patch = dict(patch)
    safe_patch["updated_at"] = _utc_now_iso()
    supabase.table(_WORKSPACE_MUSIC_GENERATIONS_TABLE).update(safe_patch).eq("id", str(generation_id)).execute()


def _replace_workspace_music_tracks(generation_id: str, tracks: List[Dict[str, Any]]) -> None:
    if not generation_id or supabase is None:
        return
    try:
        supabase.table(_WORKSPACE_MUSIC_TRACKS_TABLE).delete().eq("generation_id", str(generation_id)).execute()
    except Exception:
        pass
    payload = []
    for idx, item in enumerate(tracks, start=1):
        payload.append({
            "generation_id": str(generation_id),
            "track_index": idx,
            "provider_track_id": str(item.get("provider_track_id") or "")[:255] or None,
            "title": str(item.get("title") or f"Track {idx}")[:200],
            "audio_url": item.get("audio_url"),
            "video_url": item.get("video_url"),
            "cover_url": item.get("cover_url"),
            "lyrics": item.get("lyrics"),
            "duration_sec": item.get("duration_sec"),
            "payload_json": item.get("payload_json"),
        })
    if payload:
        supabase.table(_WORKSPACE_MUSIC_TRACKS_TABLE).insert(payload).execute()


def _load_workspace_music_tracks(generation_ids: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    ids = [str(x).strip() for x in generation_ids if str(x).strip()]
    if not ids or supabase is None:
        return out
    resp = (
        supabase.table(_WORKSPACE_MUSIC_TRACKS_TABLE)
        .select("*")
        .in_("generation_id", ids)
        .order("track_index")
        .execute()
    )
    for row in (getattr(resp, "data", None) or []):
        if not isinstance(row, dict):
            continue
        gid = str(row.get("generation_id") or "").strip()
        if not gid:
            continue
        out.setdefault(gid, []).append(_serialize_workspace_music_track(row))
    return out


def _mark_workspace_music_failed(generation_id: Optional[str], error_message: str, error_code: Optional[str] = None) -> None:
    if not generation_id:
        return
    patch: Dict[str, Any] = {
        "status": "failed",
        "error_message": (error_message or "").strip()[:4000] or "Unknown error",
        "completed_at": _utc_now_iso(),
    }
    if error_code:
        patch["error_code"] = str(error_code)[:255]
    try:
        _update_workspace_music_generation(generation_id, patch)
    except Exception:
        pass


async def _workspace_piapi_create_task(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not PIAPI_API_KEY:
        raise RuntimeError("PIAPI_API_KEY is empty")
    url = f"{PIAPI_BASE_URL}/api/v1/task"
    headers = {"X-API-Key": PIAPI_API_KEY, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(url, headers=headers, json=payload)
    response.raise_for_status()
    return response.json()


async def _workspace_piapi_get_task(task_id: str) -> Dict[str, Any]:
    if not PIAPI_API_KEY:
        raise RuntimeError("PIAPI_API_KEY is empty")
    url = f"{PIAPI_BASE_URL}/api/v1/task/{task_id}"
    headers = {"X-API-Key": PIAPI_API_KEY}
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(url, headers=headers)
    response.raise_for_status()
    return response.json()


async def _workspace_piapi_poll_task(task_id: str, *, timeout_sec: int = 240, sleep_sec: float = 2.0) -> Dict[str, Any]:
    t0 = time.time()
    while True:
        last = await _workspace_piapi_get_task(task_id)
        status = str(((last.get("data") or {}).get("status") or "")).lower()
        if status in {"completed", "failed"}:
            return last
        if time.time() - t0 > timeout_sec:
            raise TimeoutError(f"PiAPI task timeout after {timeout_sec}s")
        await asyncio.sleep(sleep_sec)


def _workspace_piapi_extract_output_urls(task_json: Dict[str, Any]) -> List[str]:
    data = task_json.get("data") if isinstance(task_json, dict) else None
    if not isinstance(data, dict):
        return []
    out = data.get("output")
    if not isinstance(out, dict):
        return []
    urls: List[str] = []
    one = out.get("image_url")
    many = out.get("image_urls")
    if isinstance(one, str) and one.strip():
        urls.append(one.strip())
    if isinstance(many, list):
        for item in many:
            if isinstance(item, str) and item.strip():
                urls.append(item.strip())
    uniq: List[str] = []
    seen = set()
    for item in urls:
        if item not in seen:
            uniq.append(item)
            seen.add(item)
    return uniq


def _workspace_piapi_error_text(task_json: Dict[str, Any]) -> str:
    data = task_json.get("data") if isinstance(task_json, dict) else None
    if not isinstance(data, dict):
        return ""
    err = data.get("error")
    detail = data.get("detail")
    parts: List[str] = []
    if isinstance(err, dict):
        for key in ("message", "raw_message", "detail"):
            value = err.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
    if isinstance(detail, str) and detail.strip():
        parts.append(detail.strip())
    logs = data.get("logs")
    if isinstance(logs, list):
        for item in logs:
            if isinstance(item, str) and item.strip():
                parts.append(item.strip())
    seen = set()
    uniq: List[str] = []
    for item in parts:
        if item not in seen:
            uniq.append(item)
            seen.add(item)
    return " | ".join(uniq)[:2000]


def _workspace_nano_banana_pro_resolution(value: Any) -> str:
    resolution = str(value or "2K").strip().upper()
    if resolution not in {"1K", "2K", "4K"}:
        resolution = "2K"
    return resolution


def _workspace_nano_banana_pro_safety(value: Any) -> str:
    level = str(value or "high").strip().lower()
    if level not in {"low", "medium", "high"}:
        level = "high"
    return level


async def _workspace_run_nano_banana_pro_site(
    *,
    user_id: int,
    prompt: str,
    source_image_bytes: Optional[bytes],
    source_filename: Optional[str],
    resolution: str,
    aspect_ratio: Optional[str],
    safety_level: str,
) -> tuple[bytes, str]:
    clean_prompt = str(prompt or "").strip()
    if not clean_prompt:
        raise RuntimeError("Empty prompt")

    input_payload: Dict[str, Any] = {
        "prompt": clean_prompt,
        # Site-only normalization: current PiAPI Nano Banana Pro expects png reliably.
        "output_format": "png",
        "resolution": _workspace_nano_banana_pro_resolution(resolution),
        "safety_level": _workspace_nano_banana_pro_safety(safety_level),
    }

    ar = str(aspect_ratio or "").strip()

    if source_image_bytes:
        source_url = _upload_workspace_input_image(
            int(user_id),
            source_image_bytes,
            filename=source_filename,
            slot="nano_banana_pro_source",
        )
        # For i2i PiAPI needs a public source URL.
        # If the user explicitly selected a concrete ratio, forward it.
        # When match_input_image is selected, omit aspect_ratio and let the provider use the source image defaults.
        input_payload["image_urls"] = [source_url]
        if ar and ar != "match_input_image":
            input_payload["aspect_ratio"] = ar
    else:
        if not ar or ar == "match_input_image":
            ar = "16:9"
        input_payload["aspect_ratio"] = ar

    payload: Dict[str, Any] = {
        "model": "gemini",
        "task_type": "nano-banana-pro",
        "input": input_payload,
    }

    if not PIAPI_API_KEY:
        raise RuntimeError("PIAPI_API_KEY is empty")
    url = f"{PIAPI_BASE_URL}/api/v1/task"
    headers = {"X-API-Key": PIAPI_API_KEY, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=60.0) as client:
        created_response = await client.post(url, headers=headers, json=payload)
    try:
        created_json = created_response.json()
    except Exception:
        created_json = {"raw": created_response.text}
    if created_response.status_code >= 400:
        raise RuntimeError(f"PiAPI HTTP {created_response.status_code}: {json.dumps(created_json, ensure_ascii=False)[:2000]}")
    task_id = str((((created_json.get("data") or {}) if isinstance(created_json, dict) else {}).get("task_id") or "")).strip()
    if not task_id:
        raise RuntimeError(f"PiAPI did not return task_id: {json.dumps(created_json, ensure_ascii=False)[:1200]}")

    done = await _workspace_piapi_poll_task(task_id, timeout_sec=600, sleep_sec=5.0)
    status = str((((done.get("data") or {}) if isinstance(done, dict) else {}).get("status") or "")).strip().lower()
    if status != "completed":
        err = _workspace_piapi_error_text(done)
        raise RuntimeError(f"PiAPI status: {status or 'unknown'}. {err}".strip())

    output_urls = _workspace_piapi_extract_output_urls(done)
    if not output_urls:
        err = _workspace_piapi_error_text(done)
        raise RuntimeError(f"PiAPI completed but returned no images. {err}".strip())

    return await _download_workspace_image_bytes(output_urls[0])


async def _workspace_sunoapi_post(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not SUNOAPI_API_KEY:
        raise RuntimeError("SUNOAPI_API_KEY is empty")
    url = f"{SUNOAPI_BASE_URL}/{str(path or '').lstrip('/')}"
    headers = {"Authorization": f"Bearer {SUNOAPI_API_KEY}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(url, headers=headers, json=payload)
    response.raise_for_status()
    js = response.json()
    if js.get("code") != 200:
        raise RuntimeError(f"SunoAPI request failed: {js}")
    return js


async def _workspace_sunoapi_get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not SUNOAPI_API_KEY:
        raise RuntimeError("SUNOAPI_API_KEY is empty")
    url = f"{SUNOAPI_BASE_URL}/{str(path or '').lstrip('/')}"
    headers = {"Authorization": f"Bearer {SUNOAPI_API_KEY}"}
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.get(url, headers=headers, params=params or None)
    response.raise_for_status()
    js = response.json()
    if js.get("code") != 200:
        raise RuntimeError(f"SunoAPI request failed: {js}")
    return js


async def _workspace_sunoapi_create_task(path: str, payload: Dict[str, Any]) -> str:
    payload = dict(payload or {})
    payload["callBackUrl"] = _workspace_suno_callback_url()
    js = await _workspace_sunoapi_post(path, payload)
    task_id = str(((js.get("data") or {}).get("taskId")) or "").strip()
    if not task_id:
        raise RuntimeError(f"SunoAPI did not return taskId: {js}")
    return task_id


async def _workspace_sunoapi_generate_task(*, prompt: str, custom_mode: bool, instrumental: bool, model: str, title: str = "", style: str = "", negative_tags: str = "", vocal_gender: Optional[str] = None, style_weight: Optional[float] = None, weirdness_constraint: Optional[float] = None, audio_weight: Optional[float] = None, persona_id: str = "", persona_model: str = "style_persona") -> str:
    payload: Dict[str, Any] = {
        "prompt": prompt,
        "customMode": bool(custom_mode),
        "instrumental": bool(instrumental),
        "model": _normalize_suno_model(model),
    }
    if title:
        payload["title"] = title
    if style:
        payload["style"] = style
    if negative_tags:
        payload["negativeTags"] = negative_tags
    if vocal_gender:
        payload["vocalGender"] = vocal_gender
    if style_weight is not None:
        payload["styleWeight"] = _music_optional_float(style_weight)
    if weirdness_constraint is not None:
        payload["weirdnessConstraint"] = _music_optional_float(weirdness_constraint)
    if audio_weight is not None:
        payload["audioWeight"] = _music_optional_float(audio_weight)
    if persona_id:
        payload["personaId"] = persona_id
        payload["personaModel"] = _normalize_persona_model(persona_model)
    return await _workspace_sunoapi_create_task("generate", payload)


async def _workspace_sunoapi_get_task(task_id: str) -> Dict[str, Any]:
    return await _workspace_sunoapi_get("generate/record-info", {"taskId": task_id})


async def _workspace_sunoapi_poll_task(task_id: str, *, timeout_sec: Optional[int] = None, sleep_sec: float = 2.0) -> Dict[str, Any]:
    if timeout_sec is None:
        timeout_sec = SUNOAPI_POLL_TIMEOUT_SEC
    t0 = time.time()
    while True:
        last = await _workspace_sunoapi_get_task(task_id)
        data = last.get("data") or {}
        status = str(data.get("status") or "").upper().strip()
        if status in {"SUCCESS", "FAILED", "ERROR", "CREATE_TASK_FAILED", "GENERATE_LYRICS_FAILED", "CALLBACK_EXCEPTION", "SENSITIVE_WORD_ERROR"}:
            return last
        if time.time() - t0 > timeout_sec:
            raise TimeoutError(f"SunoAPI task timeout after {timeout_sec}s")
        await asyncio.sleep(sleep_sec)


async def _workspace_sunoapi_create_lyrics_task(prompt: str) -> str:
    return await _workspace_sunoapi_create_task("lyrics", {"prompt": prompt})


async def _workspace_sunoapi_get_lyrics_task(task_id: str) -> Dict[str, Any]:
    return await _workspace_sunoapi_get("lyrics/record-info", {"taskId": task_id})


async def _workspace_sunoapi_poll_lyrics_task(task_id: str, *, timeout_sec: int = 180, sleep_sec: float = 3.0) -> Dict[str, Any]:
    t0 = time.time()
    while True:
        last = await _workspace_sunoapi_get_lyrics_task(task_id)
        status = str(((last.get("data") or {}).get("status") or "")).upper().strip()
        if status in {"SUCCESS", "FAILED", "ERROR", "CREATE_TASK_FAILED", "GENERATE_LYRICS_FAILED", "CALLBACK_EXCEPTION", "SENSITIVE_WORD_ERROR"}:
            return last
        if time.time() - t0 > timeout_sec:
            raise TimeoutError(f"SunoAPI lyrics timeout after {timeout_sec}s")
        await asyncio.sleep(sleep_sec)


def _workspace_sunoapi_extract_lyrics_variants(task_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    data = task_json.get("data") or {}
    resp = data.get("response") or {}
    resp_data = resp.get("data") or []
    out: List[Dict[str, Any]] = []
    if isinstance(resp_data, list):
        for idx, item in enumerate(resp_data, start=1):
            if not isinstance(item, dict):
                continue
            out.append({
                "index": idx,
                "title": item.get("title") or f"Lyrics {idx}",
                "text": item.get("text") or "",
                "status": item.get("status") or "",
                "error_message": item.get("errorMessage") or "",
                "payload_json": item,
            })
    return out


def _workspace_sunoapi_extract_tracks(task_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    data = task_json.get("data") or {}
    resp = data.get("response") or {}
    resp_data = resp.get("data") or []
    if isinstance(resp_data, list):
        return [item for item in resp_data if isinstance(item, dict)]
    return []
def _workspace_pick_first_url(val: Any) -> str:
    if not val:
        return ""
    if isinstance(val, str):
        s = val.strip()
        return s if s.startswith(("http://", "https://")) else ""
    if isinstance(val, dict):
        for k in ("url", "audio_url", "audioUrl", "song_url", "songUrl", "song_path", "songPath", "mp3", "mp3_url", "file_url", "fileUrl", "download_url", "downloadUrl", "source_stream_audio_url", "sourceStreamAudioUrl", "video_url", "videoUrl", "image_url", "imageUrl"):
            v = val.get(k)
            if isinstance(v, str) and v.strip().startswith(("http://", "https://")):
                return v.strip()
        for value in val.values():
            found = _workspace_pick_first_url(value)
            if found:
                return found
    if isinstance(val, list):
        for item in val:
            found = _workspace_pick_first_url(item)
            if found:
                return found
    return ""


def _workspace_extract_audio_url(item: Dict[str, Any]) -> str:
    if not isinstance(item, dict):
        return ""
    for k in ("audio_url", "audioUrl", "song_url", "songUrl", "song_path", "songPath", "mp3_url", "mp3", "file_url", "fileUrl", "url", "source_stream_audio_url", "sourceStreamAudioUrl"):
        v = item.get(k)
        if isinstance(v, str) and v.strip().startswith(("http://", "https://")):
            return v.strip()
    for k in ("audio", "audio_urls", "audios", "urls", "songs"):
        found = _workspace_pick_first_url(item.get(k))
        if found:
            return found
    return ""


def _workspace_build_piapi_music_payload(payload: MusicGenerateIn, ai: str, mode: str) -> Dict[str, Any]:
    if ai == "udio":
        udio_prompt = _workspace_music_prompt_text(payload, ai, mode) or "Modern atmospheric music with emotional melody"
        return {
            "model": "music-u",
            "task_type": "generate_music",
            "input": {
                "gpt_description_prompt": udio_prompt,
                "lyrics_type": "instrumental" if payload.instrumental else "generate",
            },
            "config": {"service_mode": "public"},
        }

    input_block: Dict[str, Any] = {
        "make_instrumental": bool(payload.instrumental),
    }
    if payload.title:
        input_block["title"] = str(payload.title).strip()
    if payload.tags:
        input_block["tags"] = str(payload.tags).strip()
    if mode == "lyrics":
        input_block["prompt"] = str(payload.lyrics_text or "").strip()
    else:
        input_block["gpt_description_prompt"] = str(payload.idea_text or "").strip()

    return {
        "model": "suno",
        "task_type": "music",
        "input": input_block,
        "config": {"service_mode": "public"},
    }


async def _run_workspace_music_provider(payload: MusicGenerateIn, ai: str, backend: str) -> Dict[str, Any]:
    mode_value = _normalize_music_mode_value(ai, payload.mode)
    if backend == "sunoapi":
        prompt_text = _workspace_music_prompt_text(payload, ai, mode_value) or "A modern catchy song with clear structure and strong hook"
        task_id = await _workspace_sunoapi_generate_task(
            prompt=prompt_text,
            custom_mode=bool(mode_value == "lyrics"),
            instrumental=bool(payload.instrumental),
            model=_normalize_suno_model(getattr(payload, "model", "V4_5")),
            title=str(payload.title or "").strip(),
            style=str(payload.tags or "").strip(),
            negative_tags=str(getattr(payload, "negative_tags", "") or "").strip(),
            vocal_gender=_normalize_vocal_gender(getattr(payload, "vocal_gender", None)),
            style_weight=getattr(payload, "style_weight", 0.65),
            weirdness_constraint=getattr(payload, "weirdness_constraint", 0.65),
            audio_weight=getattr(payload, "audio_weight", 0.65),
            persona_id=str(getattr(payload, "persona_id", "") or "").strip(),
            persona_model=str(getattr(payload, "persona_model", "style_persona") or "style_persona").strip(),
        )
        done = await _workspace_sunoapi_poll_task(task_id, timeout_sec=SUNOAPI_POLL_TIMEOUT_SEC, sleep_sec=2.0)
        data = done.get("data") or {}
        status = str(data.get("status") or "").upper().strip()
        if status != "SUCCESS":
            raise RuntimeError(f"SunoAPI status: {status}")
        tracks_raw = _workspace_sunoapi_extract_tracks(done)
        tracks = []
        for idx, item in enumerate(tracks_raw[:2], start=1):
            tracks.append({
                "provider_track_id": item.get("id") or item.get("audioId") or item.get("songId"),
                "title": item.get("title") or payload.title or f"Track {idx}",
                "audio_url": _workspace_extract_audio_url(item),
                "video_url": _workspace_pick_first_url(item.get("video_url") or item.get("video") or item.get("mp4") or item.get("videoUrl")),
                "cover_url": _workspace_pick_first_url(item.get("image_url") or item.get("image") or item.get("cover_url") or item.get("cover")),
                "lyrics": item.get("lyrics"),
                "payload_json": item,
            })
        if not tracks:
            raise RuntimeError("SunoAPI completed but returned no tracks")
        return {"provider_task_id": task_id, "tracks": tracks}

    task_payload = _workspace_build_piapi_music_payload(payload, ai, mode_value)
    created = await _workspace_piapi_create_task(task_payload)
    task_id = str(((created.get("data") or {}).get("task_id")) or "").strip()
    if not task_id:
        raise RuntimeError(f"PiAPI did not return task_id: {created}")
    done = await _workspace_piapi_poll_task(task_id, timeout_sec=PIAPI_POLL_TIMEOUT_SEC, sleep_sec=2.0)
    data = done.get("data") or {}
    status = str(data.get("status") or "").lower()
    if status != "completed":
        err = ((data.get("error") or {}).get("message")) or "unknown error"
        raise RuntimeError(f"PiAPI status: {status}. {err}")
    out = data.get("output") or []
    if isinstance(out, dict):
        out = [out]
    tracks = []
    for idx, item in enumerate(out[:2], start=1):
        if not isinstance(item, dict):
            continue
        tracks.append({
            "provider_track_id": item.get("id") or item.get("task_id") or item.get("song_id"),
            "title": item.get("title") or payload.title or f"Track {idx}",
            "audio_url": _workspace_extract_audio_url(item),
            "video_url": _workspace_pick_first_url(item.get("video_url") or item.get("video") or item.get("mp4") or item.get("videoUrl")),
            "cover_url": _workspace_pick_first_url(item.get("image_url") or item.get("cover_url") or item.get("cover")),
            "lyrics": item.get("lyrics"),
            "payload_json": item,
        })
    if not tracks:
        raise RuntimeError("PiAPI completed but returned no tracks")
    return {"provider_task_id": task_id, "tracks": tracks}


async def _workspace_sunoapi_start_extend_task(payload: MusicExtendIn) -> str:
    body: Dict[str, Any] = {
        "audioId": str(payload.audio_id or "").strip(),
        "defaultParamFlag": bool(payload.use_custom_params),
        "model": _normalize_suno_model(payload.model),
    }
    if payload.use_custom_params:
        body["continueAt"] = float(payload.continue_at or 0)
        if payload.prompt:
            body["prompt"] = str(payload.prompt).strip()
        if payload.title:
            body["title"] = str(payload.title).strip()
        if payload.style:
            body["style"] = str(payload.style).strip()
        if payload.negative_tags:
            body["negativeTags"] = str(payload.negative_tags).strip()
        vg = _normalize_vocal_gender(payload.vocal_gender)
        if vg:
            body["vocalGender"] = vg
        body["styleWeight"] = _music_optional_float(payload.style_weight)
        body["weirdnessConstraint"] = _music_optional_float(payload.weirdness_constraint)
        body["audioWeight"] = _music_optional_float(payload.audio_weight)
        if payload.persona_id:
            body["personaId"] = str(payload.persona_id).strip()
            body["personaModel"] = _normalize_persona_model(payload.persona_model)
    return await _workspace_sunoapi_create_task("generate/extend", body)


async def _workspace_sunoapi_start_upload_cover_task(*, upload_url: str, prompt: str, title: str, style: str, model: str, custom_mode: bool, instrumental: bool, negative_tags: str = "", vocal_gender: Optional[str] = None, style_weight: Optional[float] = None, weirdness_constraint: Optional[float] = None, audio_weight: Optional[float] = None, persona_id: str = "", persona_model: str = "style_persona") -> str:
    body: Dict[str, Any] = {
        "uploadUrl": upload_url,
        "customMode": bool(custom_mode),
        "instrumental": bool(instrumental),
        "model": _normalize_suno_model(model),
        "prompt": prompt,
    }
    if title:
        body["title"] = title
    if style:
        body["style"] = style
    if negative_tags:
        body["negativeTags"] = negative_tags
    if vocal_gender:
        body["vocalGender"] = vocal_gender
    if style_weight is not None:
        body["styleWeight"] = _music_optional_float(style_weight)
    if weirdness_constraint is not None:
        body["weirdnessConstraint"] = _music_optional_float(weirdness_constraint)
    if audio_weight is not None:
        body["audioWeight"] = _music_optional_float(audio_weight)
    if persona_id:
        body["personaId"] = persona_id
        body["personaModel"] = _normalize_persona_model(persona_model)
    return await _workspace_sunoapi_create_task("generate/upload-cover", body)


async def _workspace_sunoapi_start_upload_extend_task(*, upload_url: str, prompt: str, title: str, style: str, model: str, use_custom_params: bool, instrumental: bool, continue_at: Optional[float] = None, negative_tags: str = "", vocal_gender: Optional[str] = None, style_weight: Optional[float] = None, weirdness_constraint: Optional[float] = None, audio_weight: Optional[float] = None, persona_id: str = "", persona_model: str = "style_persona") -> str:
    body: Dict[str, Any] = {
        "uploadUrl": upload_url,
        "defaultParamFlag": bool(use_custom_params),
        "instrumental": bool(instrumental),
        "model": _normalize_suno_model(model),
    }
    if prompt:
        body["prompt"] = prompt
    if use_custom_params:
        if title:
            body["title"] = title
        if style:
            body["style"] = style
        if continue_at is not None:
            body["continueAt"] = float(continue_at)
        if negative_tags:
            body["negativeTags"] = negative_tags
        if vocal_gender:
            body["vocalGender"] = vocal_gender
        if style_weight is not None:
            body["styleWeight"] = _music_optional_float(style_weight)
        if weirdness_constraint is not None:
            body["weirdnessConstraint"] = _music_optional_float(weirdness_constraint)
        if audio_weight is not None:
            body["audioWeight"] = _music_optional_float(audio_weight)
        if persona_id:
            body["personaId"] = persona_id
            body["personaModel"] = _normalize_persona_model(persona_model)
    return await _workspace_sunoapi_create_task("generate/upload-extend", body)


async def _workspace_sunoapi_start_add_vocals_task(*, upload_url: str, prompt: str, title: str, style: str, model: str, negative_tags: str = "", vocal_gender: Optional[str] = None, style_weight: Optional[float] = None, weirdness_constraint: Optional[float] = None, audio_weight: Optional[float] = None) -> str:
    body: Dict[str, Any] = {
        "uploadUrl": upload_url,
        "prompt": prompt,
        "title": title,
        "style": style,
        "model": _normalize_suno_model(model),
    }
    if negative_tags:
        body["negativeTags"] = negative_tags
    if vocal_gender:
        body["vocalGender"] = vocal_gender
    if style_weight is not None:
        body["styleWeight"] = _music_optional_float(style_weight)
    if weirdness_constraint is not None:
        body["weirdnessConstraint"] = _music_optional_float(weirdness_constraint)
    if audio_weight is not None:
        body["audioWeight"] = _music_optional_float(audio_weight)
    return await _workspace_sunoapi_create_task("generate/add-vocals", body)


async def _workspace_sunoapi_get_timestamped_lyrics(task_id: str, audio_id: str) -> Dict[str, Any]:
    js = await _workspace_sunoapi_post("generate/get-timestamped-lyrics", {"taskId": task_id, "audioId": audio_id})
    return js.get("data") or {}


async def _workspace_sunoapi_generate_persona(payload: MusicPersonaGenerateIn) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "taskId": payload.task_id,
        "audioId": payload.audio_id,
        "name": payload.name,
        "description": payload.description,
        "vocalStart": payload.vocal_start,
        "vocalEnd": payload.vocal_end,
    }
    if payload.style:
        body["style"] = payload.style
    js = await _workspace_sunoapi_post("generate/generate-persona", body)
    return js.get("data") or {}


def _workspace_normalize_suno_task_status(task_json: Dict[str, Any]) -> Dict[str, Any]:
    data = task_json.get("data") or {}
    provider_status = str(data.get("status") or "").upper().strip()
    if provider_status == "SUCCESS":
        status = "completed"
    elif provider_status in {"FAILED", "ERROR", "CREATE_TASK_FAILED", "GENERATE_LYRICS_FAILED", "CALLBACK_EXCEPTION", "SENSITIVE_WORD_ERROR"}:
        status = "failed"
    else:
        status = "processing"
    tracks_raw = _workspace_sunoapi_extract_tracks(task_json)
    tracks: List[Dict[str, Any]] = []
    for idx, item in enumerate(tracks_raw[:8], start=1):
        tracks.append({
            "provider_track_id": item.get("id") or item.get("audioId") or item.get("songId"),
            "title": item.get("title") or f"Track {idx}",
            "audio_url": _workspace_extract_audio_url(item),
            "video_url": _workspace_pick_first_url(item.get("video_url") or item.get("video") or item.get("mp4") or item.get("videoUrl")),
            "cover_url": _workspace_pick_first_url(item.get("image_url") or item.get("image") or item.get("cover_url") or item.get("cover")),
            "lyrics": item.get("lyrics"),
            "payload_json": item,
        })
    return {
        "task_id": str(data.get("taskId") or "").strip(),
        "status": status,
        "provider_status": provider_status or "PENDING",
        "error_code": data.get("errorCode"),
        "error_message": data.get("errorMessage"),
        "tracks": tracks,
        "raw": data,
    }
async def _run_workspace_music_job(*, generation_id: str, user_id: int, payload: MusicGenerateIn, charge_tokens: int = 0, charge_ref_id: str = "") -> None:
    ai = _normalize_music_ai_value(payload.ai)
    backend = _normalize_music_backend_value(ai, payload.backend)
    mode = _normalize_music_mode_value(ai, payload.mode)
    provider_order = [backend] if backend != "auto" else ["piapi", "sunoapi"]
    if ai == "udio":
        provider_order = ["piapi"]

    last_error: Optional[Exception] = None
    for provider in provider_order:
        try:
            _update_workspace_music_generation(generation_id, {"status": "processing", "backend": provider, "mode": mode})
            result = await _run_workspace_music_provider(payload, ai, provider)
            tracks = result.get("tracks") or []
            _replace_workspace_music_tracks(generation_id, tracks)
            _update_workspace_music_generation(generation_id, {
                "status": "completed",
                "backend": provider,
                "mode": mode,
                "provider_task_id": result.get("provider_task_id"),
                "output_count": len(tracks),
                "completed_at": _utc_now_iso(),
                "error_message": None,
                "error_code": None,
            })
            return
        except Exception as e:
            last_error = e
            _update_workspace_music_generation(generation_id, {
                "provider_task_id": None,
                "error_message": str(e)[:4000],
            })
            if provider != provider_order[-1]:
                continue

    if charge_tokens > 0:
        try:
            add_tokens(int(user_id), int(charge_tokens), reason="workspace_music_refund", ref_id=charge_ref_id or uuid4().hex, meta={"error": str(last_error)[:300], "origin": "workspace_music"})
        except TypeError:
            add_tokens(int(user_id), int(charge_tokens), reason="workspace_music_refund")
    _mark_workspace_music_failed(generation_id, str(last_error or "Unknown music error"), error_code="provider_error")


@router.get("/tts/voices")
async def workspace_tts_voices(user: Dict[str, Any] = Depends(get_current_workspace_user)) -> List[Dict[str, Any]]:
    return ALLOWED_VOICES



@router.post("/tts/run")
async def workspace_tts_run(payload: TTSGenerateIn, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    if payload.voice_id not in ALLOWED_VOICE_IDS:
        raise HTTPException(status_code=400, detail="voice_id is not allowed")
    if payload.model_id not in _WORKSPACE_TTS_ALLOWED_MODELS:
        raise HTTPException(status_code=400, detail="model_id is not allowed")
    if payload.output_format not in _WORKSPACE_TTS_ALLOWED_FORMATS:
        raise HTTPException(status_code=400, detail="output_format is not allowed")

    language_code = _workspace_tts_language_code(payload.language_code)
    voice_settings = _workspace_tts_voice_settings(payload)

    uid = int(user["telegram_user_id"])
    voice_meta = next((item for item in ALLOWED_VOICES if item.get("voice_id") == payload.voice_id), None) or {}
    voice_name = str(voice_meta.get("name") or payload.voice_id)
    now_iso = _utc_now_iso()
    generation_id = _insert_workspace_voice_generation({
        "user_id": str(uid),
        "provider": "elevenlabs",
        "model": payload.model_id,
        "voice_id": payload.voice_id,
        "voice_name": voice_name,
        "text": payload.text,
        "status": "processing",
        "output_format": payload.output_format,
        "origin": "workspace_voice",
        "created_at": now_iso,
        "updated_at": now_iso,
    })

    try:
        tts = _get_tts()
        audio_bytes = await tts.tts(
            text=payload.text,
            voice_id=payload.voice_id,
            model_id=payload.model_id,
            output_format=payload.output_format,
            language_code=language_code,
            voice_settings=voice_settings,
        )
        ext = _workspace_voice_ext(payload.output_format)
        mime_type = _workspace_voice_content_type(payload.output_format)
        output_path = _workspace_voice_output_path(uid, ext)
        audio_url = upload_bytes_to_supabase(output_path, audio_bytes, mime_type)
        done_iso = _utc_now_iso()
        _update_workspace_voice_generation(
            generation_id,
            {
                "status": "completed",
                "storage_path": output_path,
                "audio_url": audio_url,
                "download_url": audio_url,
                "file_size_bytes": len(audio_bytes or b""),
                "mime_type": mime_type,
                "error_code": None,
                "error_message": None,
                "updated_at": done_iso,
                "completed_at": done_iso,
            },
        )
        return {
            "ok": True,
            "generation_id": generation_id,
            "provider": "elevenlabs",
            "model": payload.model_id,
            "voice_id": payload.voice_id,
            "voice_name": voice_name,
            "output_format": payload.output_format,
            "language_code": language_code,
            "voice_settings": voice_settings,
            "audio_url": audio_url,
            "download_url": audio_url,
            "status": "completed",
            "status_text": "Звук готов.",
            "created_at": done_iso,
            "completed_at": done_iso,
        }
    except HTTPException as e:
        _mark_workspace_voice_generation_failed(generation_id, str(e.detail), error_code="http_error")
        raise
    except Exception as e:
        _mark_workspace_voice_generation_failed(generation_id, str(e), error_code="provider_error")
        raise HTTPException(status_code=500, detail=f"Voice generation failed: {e}")


@router.get("/tts/history")
async def workspace_tts_history(
    limit: int = 20,
    offset: int = 0,
    user: Dict[str, Any] = Depends(get_current_workspace_user),
) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    safe_limit = max(1, min(int(limit or 20), 100))
    safe_offset = max(0, int(offset or 0))

    if supabase is None:
        return {"ok": True, "items": [], "limit": safe_limit, "offset": safe_offset}

    try:
        resp = (
            supabase.table(_WORKSPACE_VOICE_GENERATIONS_TABLE)
            .select("*")
            .eq("user_id", str(uid))
            .is_("deleted_at", "null")
            .order("created_at", desc=True)
            .range(safe_offset, safe_offset + safe_limit - 1)
            .execute()
        )
        rows = getattr(resp, "data", None) or []
        items = [_serialize_workspace_voice_generation(row) for row in rows if isinstance(row, dict)]
        return {
            "ok": True,
            "items": items,
            "limit": safe_limit,
            "offset": safe_offset,
            "count": len(items),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Voice history load failed: {e}")


@router.get("/tts/history/{generation_id}")
async def workspace_tts_history_item(
    generation_id: str,
    user: Dict[str, Any] = Depends(get_current_workspace_user),
) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    generation_id_text = str(generation_id or "").strip()
    if not generation_id_text:
        raise HTTPException(status_code=400, detail="Missing generation_id")

    if supabase is None:
        raise HTTPException(status_code=404, detail="Voice generation not found")

    try:
        resp = (
            supabase.table(_WORKSPACE_VOICE_GENERATIONS_TABLE)
            .select("*")
            .eq("id", generation_id_text)
            .eq("user_id", str(uid))
            .is_("deleted_at", "null")
            .limit(1)
            .execute()
        )
        rows = getattr(resp, "data", None) or []
        if not rows or not isinstance(rows[0], dict):
            raise HTTPException(status_code=404, detail="Voice generation not found")
        return {"ok": True, "item": _serialize_workspace_voice_generation(rows[0])}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Voice history item load failed: {e}")


@router.delete("/tts/history/{generation_id}")
async def workspace_tts_history_delete_item(
    generation_id: str,
    user: Dict[str, Any] = Depends(get_current_workspace_user),
) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    generation_id_text = str(generation_id or "").strip()
    if not generation_id_text:
        raise HTTPException(status_code=400, detail="Missing generation_id")

    if supabase is None:
        raise HTTPException(status_code=404, detail="Voice generation not found")

    try:
        resp = (
            supabase.table(_WORKSPACE_VOICE_GENERATIONS_TABLE)
            .select("id,user_id,storage_path,deleted_at")
            .eq("id", generation_id_text)
            .eq("user_id", str(uid))
            .is_("deleted_at", "null")
            .limit(1)
            .execute()
        )
        rows = getattr(resp, "data", None) or []
        if not rows or not isinstance(rows[0], dict):
            raise HTTPException(status_code=404, detail="Voice generation not found")
        row = rows[0]

        storage_path = str(row.get("storage_path") or "").strip()
        if storage_path:
            try:
                supabase.storage.from_(_WORKSPACE_VOICE_BUCKET).remove([storage_path])
            except Exception:
                pass

        now_iso = _utc_now_iso()
        (
            supabase.table(_WORKSPACE_VOICE_GENERATIONS_TABLE)
            .update({"deleted_at": now_iso, "updated_at": now_iso, "storage_path": None, "audio_url": None, "download_url": None})
            .eq("id", generation_id_text)
            .eq("user_id", str(uid))
            .execute()
        )
        return {"ok": True, "generation_id": generation_id_text}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Voice history delete failed: {e}")


@router.get("/image/history")
async def workspace_image_history(
    limit: int = 20,
    offset: int = 0,
    user: Dict[str, Any] = Depends(get_current_workspace_user),
) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    safe_limit = max(1, min(int(limit or 20), 100))
    safe_offset = max(0, int(offset or 0))

    if supabase is None:
        return {"ok": True, "items": [], "limit": safe_limit, "offset": safe_offset}

    try:
        resp = (
            supabase.table(_WORKSPACE_IMAGE_GENERATIONS_TABLE)
            .select("*")
            .eq("user_id", str(uid))
            .is_("deleted_at", "null")
            .order("created_at", desc=True)
            .range(safe_offset, safe_offset + safe_limit - 1)
            .execute()
        )
        rows = getattr(resp, "data", None) or []
        items = [_serialize_workspace_image_generation(row) for row in rows if isinstance(row, dict)]
        return {
            "ok": True,
            "items": items,
            "limit": safe_limit,
            "offset": safe_offset,
            "count": len(items),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Image history load failed: {e}")


@router.get("/image/history/{generation_id}")
async def workspace_image_history_item(
    generation_id: str,
    user: Dict[str, Any] = Depends(get_current_workspace_user),
) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    generation_id_text = str(generation_id or "").strip()
    if not generation_id_text:
        raise HTTPException(status_code=400, detail="Missing generation_id")

    if supabase is None:
        raise HTTPException(status_code=404, detail="Image generation not found")

    try:
        resp = (
            supabase.table(_WORKSPACE_IMAGE_GENERATIONS_TABLE)
            .select("*")
            .eq("id", generation_id_text)
            .eq("user_id", str(uid))
            .is_("deleted_at", "null")
            .limit(1)
            .execute()
        )
        rows = getattr(resp, "data", None) or []
        if not rows or not isinstance(rows[0], dict):
            raise HTTPException(status_code=404, detail="Image generation not found")
        return {"ok": True, "item": _serialize_workspace_image_generation(rows[0])}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Image history item load failed: {e}")


@router.delete("/image/history/{generation_id}")
async def workspace_image_history_delete_item(
    generation_id: str,
    user: Dict[str, Any] = Depends(get_current_workspace_user),
) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    generation_id_text = str(generation_id or "").strip()
    if not generation_id_text:
        raise HTTPException(status_code=400, detail="Missing generation_id")

    if supabase is None:
        raise HTTPException(status_code=404, detail="Image generation not found")

    try:
        resp = (
            supabase.table(_WORKSPACE_IMAGE_GENERATIONS_TABLE)
            .select("id,user_id,storage_path,deleted_at")
            .eq("id", generation_id_text)
            .eq("user_id", str(uid))
            .is_("deleted_at", "null")
            .limit(1)
            .execute()
        )
        rows = getattr(resp, "data", None) or []
        if not rows or not isinstance(rows[0], dict):
            raise HTTPException(status_code=404, detail="Image generation not found")
        row = rows[0]

        bucket_name = (os.getenv("SUPABASE_BUCKET") or "").strip()
        storage_path = str(row.get("storage_path") or "").strip()
        if storage_path and bucket_name:
            try:
                supabase.storage.from_(bucket_name).remove([storage_path])
            except Exception:
                pass

        now_iso = _utc_now_iso()
        (
            supabase.table(_WORKSPACE_IMAGE_GENERATIONS_TABLE)
            .update({"deleted_at": now_iso, "updated_at": now_iso, "storage_path": None, "image_url": None, "download_url": None})
            .eq("id", generation_id_text)
            .eq("user_id", str(uid))
            .execute()
        )
        return {"ok": True, "generation_id": generation_id_text}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Image history delete failed: {e}")


@router.post("/image/run")
async def workspace_image_run(
    request: Request,
    user: Dict[str, Any] = Depends(get_current_workspace_user),
) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    form = await request.form()

    provider = str(form.get("provider") or "").strip().lower()
    model = str(form.get("model") or "").strip()
    mode = str(form.get("mode") or "").strip().lower()
    prompt = str(form.get("prompt") or "").strip()
    resolution = str(form.get("resolution") or "2K").strip().upper() or "2K"
    aspect_ratio = str(form.get("aspect_ratio") or "").strip() or "match_input_image"
    safety_level = str(form.get("safety_level") or "high").strip().lower() or "high"
    poster_style = str(form.get("poster_style") or "").strip()
    style_preset = str(form.get("style_preset") or "").strip()
    mood_preset = str(form.get("mood_preset") or "").strip()
    preset_slug = str(form.get("preset_slug") or "standard").strip().lower() or "standard"

    if not provider:
        raise HTTPException(status_code=400, detail="Missing provider")
    if not model:
        raise HTTPException(status_code=400, detail="Missing model")
    if not mode:
        raise HTTPException(status_code=400, detail="Missing mode")
    if provider != "topaz_photo" and not prompt:
        raise HTTPException(status_code=400, detail="Missing prompt")

    supported = {"nano_banana", "nano_banana_pro", "posters", "photosession", "two_images", "text_to_image", "topaz_photo"}
    if provider not in supported:
        raise HTTPException(status_code=400, detail=f"Provider {provider} is not supported in /image/run")

    source_upload = form.get("source_image")
    base_upload = form.get("base_image")
    source_image = await _read_optional_upload_bytes(source_upload)
    base_image = await _read_optional_upload_bytes(base_upload)

    if provider == "nano_banana" and not source_image:
        raise HTTPException(status_code=400, detail="Для Nano Banana нужен source image.")
    if provider == "nano_banana_pro" and mode == "image_to_image" and not source_image:
        raise HTTPException(status_code=400, detail="Для Image→Image нужен source image.")
    if provider == "posters" and mode == "photo_edit" and not source_image:
        raise HTTPException(status_code=400, detail="Для Photo Edit нужен source image.")
    if provider == "photosession" and not source_image:
        raise HTTPException(status_code=400, detail="Для нейро фотосессии нужен source image.")
    if provider == "two_images" and (not source_image or not base_image):
        raise HTTPException(status_code=400, detail="Для режима Картинка + Картинка нужны base image и source image.")
    if provider == "topaz_photo" and not source_image:
        raise HTTPException(status_code=400, detail="Для Topaz Photo Upscale нужен source image.")

    if provider in {"nano_banana_pro", "text_to_image"} and mode in {"text_to_image", "t2i"} and aspect_ratio == "match_input_image":
        aspect_ratio = "16:9"

    run_prompt = _build_workspace_image_prompt(
        provider=provider,
        mode=mode,
        prompt=prompt,
        poster_style=poster_style,
        style_preset=style_preset,
        mood_preset=mood_preset,
    )
    if provider != "topaz_photo" and not run_prompt:
        raise HTTPException(status_code=400, detail="Empty prompt")

    ensure_user_row(uid)
    try:
        bal = float(get_balance(uid) or 0)
    except Exception:
        bal = 0
    cost = int(_workspace_image_cost(provider, mode, preset_slug))
    if cost > 0 and bal < cost:
        raise HTTPException(status_code=402, detail=f"Недостаточно токенов. Нужно: {cost} ток.")

    charged = False
    reason = _workspace_image_charge_reason(provider, mode)
    ref_id = uuid4().hex if cost > 0 and reason else ""
    generation_id = _insert_workspace_image_generation(
        {
            "user_id": str(uid),
            "provider": provider,
            "model": model,
            "mode": mode,
            "prompt": prompt,
            "status": "processing",
            "resolution": resolution,
            "aspect_ratio": aspect_ratio,
            "safety_level": safety_level,
            "poster_style": poster_style,
            "style_preset": style_preset,
            "mood_preset": mood_preset,
            "preset_slug": preset_slug if provider == "topaz_photo" else None,
            "origin": "workspace_image",
        }
    )

    try:
        if cost > 0 and reason:
            try:
                add_tokens(uid, -cost, reason=reason, ref_id=ref_id, meta={"origin": "workspace_image", "provider": provider, "mode": mode})
            except TypeError:
                add_tokens(uid, -int(cost), reason=reason)
            charged = True

        before_image_url: Optional[str] = None
        after_image_url: Optional[str] = None
        compare_mode = False

        if provider == "nano_banana":
            out_bytes, ext = await run_nano_banana(source_image, run_prompt, output_format="jpg", aspect_ratio=aspect_ratio)
            engine = "nano_banana"
        elif provider == "photosession":
            from main import ark_edit_image

            source_url = _upload_workspace_input_image(
                uid,
                source_image,
                filename=getattr(source_upload, "filename", None),
                slot="photosession_source",
            )
            out_bytes = await ark_edit_image(
                source_image_bytes=b"",
                prompt=run_prompt,
                size=_workspace_ark_size(resolution),
                source_image_url=source_url,
            )
            ext = _workspace_detect_image_ext(out_bytes, default="jpg")
            engine = "modelark_seedream"
        elif provider == "text_to_image":
            from main import ark_text_to_image

            out_bytes = await ark_text_to_image(run_prompt, size=_workspace_ark_size(resolution))
            ext = _workspace_detect_image_ext(out_bytes, default="jpg")
            engine = "modelark_seedream"
        elif provider == "topaz_photo":
            try:
                preset_settings = get_photo_preset_settings(preset_slug)
            except Exception:
                raise HTTPException(status_code=400, detail=f"Unknown Topaz preset: {preset_slug}")

            source_url = await _upload_workspace_topaz_input_image(
                uid,
                source_image,
                filename=getattr(source_upload, "filename", None),
                slot=f"topaz_{preset_slug}",
            )
            topaz_result = await _run_workspace_topaz_with_retry(
                TopazImageParams(
                    image_url=source_url,
                    enhance_model=str(preset_settings.get("enhance_model") or "Standard V2"),
                    upscale_factor=str(preset_settings.get("upscale_factor") or "2x"),
                    output_format=str(preset_settings.get("output_format") or "jpg"),
                    subject_detection=str(preset_settings.get("subject_detection") or "Foreground"),
                    face_enhancement=bool(preset_settings.get("face_enhancement")),
                    face_enhancement_creativity=float(preset_settings.get("face_enhancement_creativity") or 0.0),
                    face_enhancement_strength=float(preset_settings.get("face_enhancement_strength") or 0.8),
                )
            )
            out_bytes, ext = await _download_workspace_image_bytes(topaz_result.output_url)
            engine = "topaz_photo_replicate"
            before_image_url = source_url
            compare_mode = True
        else:
            input_image = source_image
            if provider == "two_images":
                input_image = _compose_workspace_pair_image(base_image, source_image)
                aspect_ratio = "match_input_image"
            out_bytes, ext = await _workspace_run_nano_banana_pro_site(
                user_id=uid,
                prompt=run_prompt,
                source_image_bytes=input_image,
                source_filename=getattr(source_upload, "filename", None),
                resolution=resolution,
                aspect_ratio=aspect_ratio,
                safety_level=safety_level,
            )
            engine = "nano_banana_pro"

        output_path = _workspace_image_output_path(uid, ext)
        image_url = upload_bytes_to_supabase(output_path, out_bytes, _workspace_image_content_type(ext))
        after_image_url = image_url
        now_iso = _utc_now_iso()
        _update_workspace_image_generation(
            generation_id,
            {
                "status": "completed",
                "storage_path": output_path,
                "image_url": image_url,
                "download_url": image_url,
                "file_size_bytes": len(out_bytes or b""),
                "mime_type": _workspace_image_content_type(ext),
                "error_code": None,
                "error_message": None,
                "preset_slug": preset_slug if provider == "topaz_photo" else None,
                "source_image_url": before_image_url if provider == "topaz_photo" else None,
                "before_image_url": before_image_url if provider == "topaz_photo" else None,
                "after_image_url": after_image_url if provider == "topaz_photo" else image_url,
                "compare_mode": compare_mode if provider == "topaz_photo" else False,
                "updated_at": now_iso,
                "completed_at": now_iso,
            },
        )
        try:
            balance_tokens = int(get_balance(uid) or 0)
        except Exception:
            balance_tokens = None
        return {
            "ok": True,
            "generation_id": generation_id,
            "provider": provider,
            "model": model,
            "mode": mode,
            "engine": engine,
            "tokens_required": cost,
            "image_url": image_url,
            "download_url": image_url,
            "before_image_url": before_image_url,
            "after_image_url": after_image_url or image_url,
            "compare_mode": bool(compare_mode and before_image_url and (after_image_url or image_url)),
            "preset_slug": preset_slug if provider == "topaz_photo" else None,
            "status": "completed",
            "status_text": "Изображение готово.",
            "balance_tokens": balance_tokens,
        }
    except HTTPException as e:
        if charged:
            try:
                try:
                    add_tokens(uid, cost, reason=f"{reason}_refund", ref_id=ref_id, meta={"origin": "workspace_image", "error": str(e.detail)})
                except TypeError:
                    add_tokens(uid, int(cost), reason=f"{reason}_refund")
            except Exception:
                pass
        _mark_workspace_image_generation_failed(generation_id, str(e.detail), error_code="http_error")
        raise
    except Exception as e:
        if charged:
            try:
                try:
                    add_tokens(uid, cost, reason=f"{reason}_refund", ref_id=ref_id, meta={"origin": "workspace_image", "error": str(e)})
                except TypeError:
                    add_tokens(uid, int(cost), reason=f"{reason}_refund")
            except Exception:
                pass
        _mark_workspace_image_generation_failed(generation_id, str(e), error_code="provider_error")
        raise HTTPException(status_code=500, detail=f"Image generation failed: {e}")


@router.post("/tts/generate")
async def workspace_tts_generate(payload: TTSGenerateIn, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Response:
    if payload.voice_id not in ALLOWED_VOICE_IDS:
        raise HTTPException(status_code=400, detail="voice_id is not allowed")
    if payload.model_id not in _WORKSPACE_TTS_ALLOWED_MODELS:
        raise HTTPException(status_code=400, detail="model_id is not allowed")
    if payload.output_format not in _WORKSPACE_TTS_ALLOWED_FORMATS:
        raise HTTPException(status_code=400, detail="output_format is not allowed")

    language_code = _workspace_tts_language_code(payload.language_code)
    voice_settings = _workspace_tts_voice_settings(payload)
    tts = _get_tts()
    audio_bytes = await tts.tts(
        text=payload.text,
        voice_id=payload.voice_id,
        model_id=payload.model_id,
        output_format=payload.output_format,
        language_code=language_code,
        voice_settings=voice_settings,
    )
    media_type = "audio/mpeg" if payload.output_format.startswith("mp3") else "application/octet-stream"
    return Response(content=audio_bytes, media_type=media_type)


@router.post("/songwriter")
async def workspace_songwriter(payload: SongwriterPayload, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    user_text = (payload.text or "").strip()
    if not user_text:
        raise HTTPException(status_code=400, detail="Missing 'text'")
    history = []
    if payload.history:
        history = [{"role": t.role, "content": t.content} for t in payload.history if t.role in ("user", "assistant") and (t.content or "").strip()]
    answer = await openai_chat_answer(user_text=user_text, system_prompt=_songwriter_prompt_with_context(payload), history=history, temperature=0.6, max_tokens=900)
    return {"answer": answer}


@router.post("/music/run")
async def workspace_music_run(payload: MusicGenerateIn, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    ai = _normalize_music_ai_value(payload.ai)
    backend = _normalize_music_backend_value(ai, payload.backend)
    mode = _normalize_music_mode_value(ai, payload.mode)
    prompt_text = _workspace_music_prompt_text(payload, ai, mode)
    if not prompt_text:
        raise HTTPException(status_code=400, detail="Music prompt is empty")

    ensure_user_row(uid)
    try:
        balance = int(get_balance(uid) or 0)
    except Exception:
        balance = 0
    cost = _workspace_music_cost_tokens(payload, ai, backend)
    if cost > 0 and balance < cost:
        raise HTTPException(status_code=402, detail=f"Недостаточно токенов. Нужно: {cost} ток.")

    charged = False
    ref_id = uuid4().hex if cost > 0 else ""
    if cost > 0:
        try:
            add_tokens(uid, -int(cost), reason=_workspace_music_charge_reason(ai, backend), ref_id=ref_id, meta={"origin": "workspace_music", "ai": ai, "backend": backend, "mode": mode})
            charged = True
        except TypeError:
            add_tokens(uid, -int(cost), reason=_workspace_music_charge_reason(ai, backend))
            charged = True

    generation_id = _insert_workspace_music_generation({
        "user_id": str(uid),
        "ai": ai,
        "backend": backend,
        "mode": mode,
        "title": str(payload.title or "").strip()[:200],
        "tags": str(payload.tags or "").strip()[:400],
        "language": str(payload.language or "").strip()[:32],
        "mood": str(payload.mood or "").strip()[:200],
        "references": str(payload.references or "").strip()[:1000],
        "idea_text": str(payload.idea_text or "").strip(),
        "lyrics_text": str(payload.lyrics_text or "").strip(),
        "instrumental": bool(payload.instrumental),
        "status": "queued",
        "origin": "workspace",
        "charge_tokens": int(cost if charged else 0),
    })

    asyncio.create_task(
        _run_workspace_music_job(
            generation_id=generation_id,
            user_id=uid,
            payload=payload,
            charge_tokens=(int(cost) if charged else 0),
            charge_ref_id=ref_id,
        )
    )

    return {"ok": True, "generation_id": generation_id, "status": "queued", "cost_tokens": int(cost if charged else 0)}


@router.post("/music/lyrics/generate")
async def workspace_music_generate_lyrics(payload: MusicLyricsGenerateIn, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    _ = int(user["telegram_user_id"])
    task_id = await _workspace_sunoapi_create_lyrics_task(str(payload.prompt or "").strip())
    done = await _workspace_sunoapi_poll_lyrics_task(task_id)
    data = done.get("data") or {}
    status = str(data.get("status") or "").upper().strip()
    if status != "SUCCESS":
        raise HTTPException(status_code=400, detail=data.get("errorMessage") or f"Lyrics generation failed: {status}")
    items = _workspace_sunoapi_extract_lyrics_variants(done)
    return {"ok": True, "task_id": task_id, "items": items, "text": (items[0].get("text") if items else "")}


@router.post("/music/timestamped-lyrics")
async def workspace_music_timestamped_lyrics(payload: MusicTimestampLyricsIn, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    _ = int(user["telegram_user_id"])
    data = await _workspace_sunoapi_get_timestamped_lyrics(str(payload.task_id).strip(), str(payload.audio_id).strip())
    return {"ok": True, "data": data}


@router.post("/music/persona/generate")
async def workspace_music_generate_persona(payload: MusicPersonaGenerateIn, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    _ = int(user["telegram_user_id"])
    data = await _workspace_sunoapi_generate_persona(payload)
    return {"ok": True, "data": data}


@router.post("/music/extend/start")
async def workspace_music_extend_start(payload: MusicExtendIn, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    _ = int(user["telegram_user_id"])
    task_id = await _workspace_sunoapi_start_extend_task(payload)
    return {"ok": True, "task_id": task_id, "status": "queued"}


@router.get("/music/task-status")
async def workspace_music_task_status(task_id: str, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    _ = int(user["telegram_user_id"])
    task_id_text = str(task_id or "").strip()
    if not task_id_text:
        raise HTTPException(status_code=400, detail="Missing task_id")
    task = await _workspace_sunoapi_get_task(task_id_text)
    item = _workspace_normalize_suno_task_status(task)
    return {"ok": True, "item": item}


@router.post("/music/upload-cover/start")
async def workspace_music_upload_cover_start(
    file: UploadFile = File(...),
    prompt: str = Form(""),
    title: str = Form(""),
    style: str = Form(""),
    model: str = Form("V4_5"),
    custom_mode: bool = Form(True),
    instrumental: bool = Form(False),
    negative_tags: str = Form(""),
    vocal_gender: str = Form(""),
    style_weight: float = Form(0.65),
    weirdness_constraint: float = Form(0.65),
    audio_weight: float = Form(0.65),
    persona_id: str = Form(""),
    persona_model: str = Form("style_persona"),
    user: Dict[str, Any] = Depends(get_current_workspace_user),
) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    raw = await file.read()
    uploaded = _upload_workspace_music_source_file(file_bytes=raw, filename=file.filename or "audio.bin", content_type=(file.content_type or "audio/mpeg"), user_id=uid, slot="cover")
    task_id = await _workspace_sunoapi_start_upload_cover_task(
        upload_url=uploaded["upload_url"],
        prompt=str(prompt or "").strip(),
        title=str(title or "").strip(),
        style=str(style or "").strip(),
        model=model,
        custom_mode=bool(custom_mode),
        instrumental=bool(instrumental),
        negative_tags=str(negative_tags or "").strip(),
        vocal_gender=_normalize_vocal_gender(vocal_gender),
        style_weight=style_weight,
        weirdness_constraint=weirdness_constraint,
        audio_weight=audio_weight,
        persona_id=str(persona_id or "").strip(),
        persona_model=str(persona_model or "style_persona").strip(),
    )
    return {"ok": True, "task_id": task_id, "status": "queued", "upload_url": uploaded["upload_url"]}


@router.post("/music/upload-extend/start")
async def workspace_music_upload_extend_start(
    file: UploadFile = File(...),
    prompt: str = Form(""),
    title: str = Form(""),
    style: str = Form(""),
    model: str = Form("V4_5"),
    use_custom_params: bool = Form(True),
    instrumental: bool = Form(False),
    continue_at: float = Form(60.0),
    negative_tags: str = Form(""),
    vocal_gender: str = Form(""),
    style_weight: float = Form(0.65),
    weirdness_constraint: float = Form(0.65),
    audio_weight: float = Form(0.65),
    persona_id: str = Form(""),
    persona_model: str = Form("style_persona"),
    user: Dict[str, Any] = Depends(get_current_workspace_user),
) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    raw = await file.read()
    uploaded = _upload_workspace_music_source_file(file_bytes=raw, filename=file.filename or "audio.bin", content_type=(file.content_type or "audio/mpeg"), user_id=uid, slot="extend")
    task_id = await _workspace_sunoapi_start_upload_extend_task(
        upload_url=uploaded["upload_url"],
        prompt=str(prompt or "").strip(),
        title=str(title or "").strip(),
        style=str(style or "").strip(),
        model=model,
        use_custom_params=bool(use_custom_params),
        instrumental=bool(instrumental),
        continue_at=continue_at,
        negative_tags=str(negative_tags or "").strip(),
        vocal_gender=_normalize_vocal_gender(vocal_gender),
        style_weight=style_weight,
        weirdness_constraint=weirdness_constraint,
        audio_weight=audio_weight,
        persona_id=str(persona_id or "").strip(),
        persona_model=str(persona_model or "style_persona").strip(),
    )
    return {"ok": True, "task_id": task_id, "status": "queued", "upload_url": uploaded["upload_url"]}


@router.post("/music/add-vocals/start")
async def workspace_music_add_vocals_start(
    file: UploadFile = File(...),
    prompt: str = Form(...),
    title: str = Form(...),
    style: str = Form(...),
    model: str = Form("V4_5"),
    negative_tags: str = Form(""),
    vocal_gender: str = Form(""),
    style_weight: float = Form(0.65),
    weirdness_constraint: float = Form(0.65),
    audio_weight: float = Form(0.65),
    user: Dict[str, Any] = Depends(get_current_workspace_user),
) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    raw = await file.read()
    uploaded = _upload_workspace_music_source_file(file_bytes=raw, filename=file.filename or "audio.bin", content_type=(file.content_type or "audio/mpeg"), user_id=uid, slot="add_vocals")
    task_id = await _workspace_sunoapi_start_add_vocals_task(
        upload_url=uploaded["upload_url"],
        prompt=str(prompt or "").strip(),
        title=str(title or "").strip(),
        style=str(style or "").strip(),
        model=model,
        negative_tags=str(negative_tags or "").strip(),
        vocal_gender=_normalize_vocal_gender(vocal_gender),
        style_weight=style_weight,
        weirdness_constraint=weirdness_constraint,
        audio_weight=audio_weight,
    )
    return {"ok": True, "task_id": task_id, "status": "queued", "upload_url": uploaded["upload_url"]}


@router.get("/music/history")
async def workspace_music_history(
    limit: int = 20,
    offset: int = 0,
    user: Dict[str, Any] = Depends(get_current_workspace_user),
) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    safe_limit = max(1, min(int(limit or 20), 100))
    safe_offset = max(0, int(offset or 0))

    if supabase is None:
        return {"ok": True, "items": [], "limit": safe_limit, "offset": safe_offset}

    try:
        resp = (
            supabase.table(_WORKSPACE_MUSIC_GENERATIONS_TABLE)
            .select("*")
            .eq("user_id", str(uid))
            .is_("deleted_at", "null")
            .order("created_at", desc=True)
            .range(safe_offset, safe_offset + safe_limit - 1)
            .execute()
        )
        rows = getattr(resp, "data", None) or []
        ids = [str(row.get("id")) for row in rows if isinstance(row, dict) and row.get("id")]
        tracks_map = _load_workspace_music_tracks(ids)
        items = [_serialize_workspace_music_generation(row, tracks_map.get(str(row.get("id")), [])) for row in rows if isinstance(row, dict)]
        return {"ok": True, "items": items, "limit": safe_limit, "offset": safe_offset, "count": len(items)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Music history load failed: {e}")


@router.get("/music/history/{generation_id}")
async def workspace_music_history_item(
    generation_id: str,
    user: Dict[str, Any] = Depends(get_current_workspace_user),
) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    generation_id_text = str(generation_id or "").strip()
    if not generation_id_text:
        raise HTTPException(status_code=400, detail="Missing generation_id")

    if supabase is None:
        raise HTTPException(status_code=404, detail="Music generation not found")

    try:
        resp = (
            supabase.table(_WORKSPACE_MUSIC_GENERATIONS_TABLE)
            .select("*")
            .eq("id", generation_id_text)
            .eq("user_id", str(uid))
            .is_("deleted_at", "null")
            .limit(1)
            .execute()
        )
        rows = getattr(resp, "data", None) or []
        if not rows or not isinstance(rows[0], dict):
            raise HTTPException(status_code=404, detail="Music generation not found")
        tracks_map = _load_workspace_music_tracks([generation_id_text])
        item = _serialize_workspace_music_generation(rows[0], tracks_map.get(generation_id_text, []))
        return {"ok": True, "item": item}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Music history item load failed: {e}")


@router.delete("/music/history/{generation_id}")
async def workspace_music_history_delete_item(
    generation_id: str,
    user: Dict[str, Any] = Depends(get_current_workspace_user),
) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    generation_id_text = str(generation_id or "").strip()
    if not generation_id_text:
        raise HTTPException(status_code=400, detail="Missing generation_id")

    if supabase is None:
        raise HTTPException(status_code=404, detail="Music generation not found")

    try:
        resp = (
            supabase.table(_WORKSPACE_MUSIC_GENERATIONS_TABLE)
            .select("id,user_id,deleted_at")
            .eq("id", generation_id_text)
            .eq("user_id", str(uid))
            .is_("deleted_at", "null")
            .limit(1)
            .execute()
        )
        rows = getattr(resp, "data", None) or []
        if not rows or not isinstance(rows[0], dict):
            raise HTTPException(status_code=404, detail="Music generation not found")
        supabase.table(_WORKSPACE_MUSIC_GENERATIONS_TABLE).update({"deleted_at": _utc_now_iso(), "updated_at": _utc_now_iso()}).eq("id", generation_id_text).execute()
        return {"ok": True, "generation_id": generation_id_text}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Music history delete failed: {e}")



@router.get("/prompts/categories")
async def workspace_prompts_categories(user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    return prompts_categories()


@router.get("/prompts/groups")
async def workspace_prompts_groups(category: str, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    return prompts_groups(category=category)


@router.get("/prompts/items")
async def workspace_prompts_items(group_id: str, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    return prompts_items(group_id=group_id)
