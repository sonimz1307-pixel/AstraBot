from __future__ import annotations

import io
import base64
import math
import json
import mimetypes
import os
import re
import tempfile
import subprocess
import shutil
import wave
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
from chat_file_text import extract_file_text
from chat_attachment_storage import (
    CHAT_ATTACHMENTS_BUCKET,
    is_chat_storage_configured,
    upload_chat_attachment_bytes,
)
from kie_claude_chat import (
    KIE_CLAUDE_DISPLAY_NAME,
    KIE_CLAUDE_HISTORY_MESSAGES,
    KIE_CLAUDE_MODEL_ID,
    KIE_CLAUDE_SUMMARY_MAX_CHARS,
    is_kie_claude_model,
    kie_claude_answer,
    kie_claude_summarize_dialogue,
)
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
from billing_db import add_tokens, ensure_user_row, get_balance, get_balance_history
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
from veo_billing import calc_veo_charge
from grok_video_replicate import (
    GrokVideoError,
    grok_tokens_for_duration,
    normalize_grok_aspect_ratio,
    normalize_grok_duration,
    normalize_grok_provider_mode,
    normalize_grok_resolution,
    run_grok_image_to_video,
    run_grok_text_to_video,
)
from seedance_kie import (
    SeedanceKieError,
    normalize_seedance_kie_aspect_ratio,
    normalize_seedance_kie_duration,
    normalize_seedance_kie_mode,
    normalize_seedance_kie_model,
    run_seedance_kie_image_to_video,
    run_seedance_kie_omni_reference,
    run_seedance_kie_text_to_video,
    seedance_kie_tokens_for_duration,
    seedance_kie_video_reference_surcharge,
)
from pixverse_c1 import (
    PixVerseC1Error,
    create_pixverse_c1_fusion,
    create_pixverse_c1_image_to_video,
    create_pixverse_c1_text_to_video,
    create_pixverse_c1_transition,
    normalize_pixverse_c1_aspect_ratio,
    normalize_pixverse_c1_duration,
    normalize_pixverse_c1_mode,
    normalize_pixverse_c1_quality,
    pixverse_c1_tokens_for_duration,
    upload_pixverse_image,
    wait_for_pixverse_video,
)
from kling3_pricing import calculate_kling3_price
from kling3_kie_flow import normalize_kling3_kie_elements, upload_kling3_kie_input_bytes
from kling3_kie_pricing import (
    calculate_kling3_kie_price,
    kling3_kie_billable_seconds,
    normalize_kling3_kie_aspect_ratio,
    normalize_kling3_kie_duration,
    normalize_kling3_kie_generation_mode,
    normalize_kling3_kie_mode,
    normalize_kling3_kie_shots,
)
from songwriter_prompt import SONGWRITER_SYSTEM_PROMPT
from queue_redis import enqueue_job
from chat_job_store import create_chat_job_status, get_chat_job_status, set_chat_job_status
from nano_banana import run_nano_banana
from nano_banana_pro import handle_nano_banana_pro
from nano_banana_pro_new_kie import handle_nano_banana_pro_new, normalize_nano_banana_pro_new_aspect_ratio, normalize_nano_banana_pro_new_resolution
from switchx_service import SwitchXClient, SwitchXError
from topaz_image_replicate import TopazImageParams, run_topaz_image_upscale
from topaz_pricing import get_photo_preset_settings, get_photo_preset_tokens
from yookassa_flow import create_yookassa_payment
from app.services.legnext_midjourney import (
    LegnextMidjourneyError,
    build_midjourney_v7_prompt,
    create_midjourney_diffusion,
    create_midjourney_reroll,
    create_midjourney_variation,
    get_midjourney_job,
    normalize_midjourney_speed_mode,
)
from app.services.video_editor_service import (
    VIDEO_EDIT_QUEUE_NAME,
    MAX_AUDIO_CLIPS,
    MAX_MERGE_ITEMS,
    MAX_OUTPUT_DURATION_SEC,
    create_workspace_upload_record,
    extract_first_frame_bytes,
    get_workspace_edit_job_row,
    get_workspace_generation_row,
    get_workspace_upload_row,
    insert_workspace_edit_job_row,
    probe_media,
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

WORKSPACE_MEDIA_QUEUE_NAME = (os.getenv("WORKSPACE_MEDIA_QUEUE_NAME", "workspace_media") or "workspace_media").strip() or "workspace_media"
KLING3_KIE_QUEUE_NAME = (os.getenv("KLING3_KIE_QUEUE_NAME", "kling3_kie") or "kling3_kie").strip() or "kling3_kie"
WORKSPACE_IMAGE_QUEUE_NAME = (os.getenv("WORKSPACE_IMAGE_QUEUE_NAME", "workspace_image") or "workspace_image").strip() or "workspace_image"
WORKSPACE_CHAT_OPENAI_QUEUE_NAME = (os.getenv("WORKSPACE_CHAT_OPENAI_QUEUE_NAME", "workspace_chat_openai") or "workspace_chat_openai").strip() or "workspace_chat_openai"
WORKSPACE_CHAT_CLAUDE_QUEUE_NAME = (os.getenv("WORKSPACE_CHAT_CLAUDE_QUEUE_NAME", "workspace_chat_claude") or "workspace_chat_claude").strip() or "workspace_chat_claude"
SEEDANCE_AUDIO_MAX_DURATION_SEC = 15.0
SEEDANCE_AUDIO_ALLOWED_EXTS = {"mp3", "wav"}
SEEDANCE_AUDIO_ALLOWED_MIME_TYPES = {"audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav", "audio/wave"}
SEEDANCE_VIDEO_ALLOWED_EXTS = {"mp4", "mov"}
SEEDANCE_VIDEO_ALLOWED_MIME_TYPES = {"video/mp4", "video/quicktime"}
SEEDANCE_VIDEO_TOTAL_MAX_DURATION_SEC = 15.4
SWITCHX_TOKENS_PER_SEC_720 = max(1, int(os.getenv("SWITCHX_TOKENS_PER_SEC_720", "1") or "1"))
SWITCHX_TOKENS_PER_SEC_1080 = max(1, int(os.getenv("SWITCHX_TOKENS_PER_SEC_1080", "2") or "2"))


def _guess_seedance_audio_ext(*, filename: Any = None, content_type: Any = None, raw: bytes = b"") -> str:
    name = str(filename or "").strip().lower()
    if name.endswith(".mp3"):
        return "mp3"
    if name.endswith(".wav"):
        return "wav"
    ctype = str(content_type or "").strip().lower()
    if ctype in {"audio/mpeg", "audio/mp3"}:
        return "mp3"
    if ctype in {"audio/wav", "audio/x-wav", "audio/wave"}:
        return "wav"
    head = bytes((raw or b"")[:32])
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head.startswith(b"ID3"):
        return "mp3"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "mp3"
    return ""


def _probe_seedance_audio_duration_seconds(raw: bytes, ext: str) -> float:
    suffix = f".{(ext or 'bin').strip('.').lower() or 'bin'}"
    tmp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(raw)
            tmp.flush()
            tmp_path = tmp.name
        try:
            meta = probe_media(tmp_path)
            duration = float(meta.get("duration") or meta.get("duration_sec") or 0.0)
            if duration > 0:
                return duration
        except Exception:
            pass
        if ext == "wav":
            with wave.open(tmp_path, "rb") as wav_file:
                frames = float(wav_file.getnframes() or 0)
                rate = float(wav_file.getframerate() or 0)
                return (frames / rate) if rate > 0 else 0.0
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
    return 0.0


def _normalize_seedance_audio_bytes(raw: bytes, ext: str) -> bytes:
    normalized_ext = str(ext or "").strip().lower()
    if normalized_ext != "wav":
        return raw
    if not shutil.which("ffmpeg"):
        return raw
    src_path: Optional[str] = None
    dst_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as src:
            src.write(raw)
            src.flush()
            src_path = src.name
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as dst:
            dst_path = dst.name
        proc = subprocess.run(
            [
                "ffmpeg", "-y", "-i", src_path,
                "-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2",
                dst_path,
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            return raw
        with open(dst_path, "rb") as fh:
            return fh.read() or raw
    except Exception:
        return raw
    finally:
        for path in (src_path, dst_path):
            if path:
                try:
                    os.unlink(path)
                except Exception:
                    pass


def _prepare_seedance_audio_file(upload: Any, raw: bytes) -> tuple[bytes, str, float]:
    ext = _guess_seedance_audio_ext(
        filename=getattr(upload, "filename", None),
        content_type=getattr(upload, "content_type", None),
        raw=raw,
    )
    if ext not in SEEDANCE_AUDIO_ALLOWED_EXTS:
        raise HTTPException(status_code=400, detail="Для Seedance 2.0 audio refs доступны только MP3 или WAV.")
    normalized_raw = _normalize_seedance_audio_bytes(raw, ext)
    duration_sec = _probe_seedance_audio_duration_seconds(normalized_raw, ext)
    if duration_sec <= 0:
        raise HTTPException(status_code=400, detail="Не удалось определить длительность audio reference для Seedance 2.0.")
    if duration_sec > SEEDANCE_AUDIO_MAX_DURATION_SEC:
        raise HTTPException(status_code=400, detail="Для Seedance 2.0 audio reference должен быть не длиннее 15 секунд.")
    return normalized_raw, ext, duration_sec


def _guess_seedance_video_ext(*, filename: Any = None, content_type: Any = None, raw: bytes = b"") -> str:
    name = str(filename or "").strip().lower()
    if name.endswith(".mp4"):
        return "mp4"
    if name.endswith(".mov"):
        return "mov"
    ctype = str(content_type or "").strip().lower()
    if ctype == "video/mp4":
        return "mp4"
    if ctype == "video/quicktime":
        return "mov"
    head = bytes((raw or b"")[:32])
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand == b"qt  ":
            return "mov"
        return "mp4"
    return ""


def _probe_seedance_video_duration_seconds(raw: bytes, ext: str) -> float:
    suffix = f".{(ext or 'bin').strip('.').lower() or 'bin'}"
    tmp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(raw)
            tmp.flush()
            tmp_path = tmp.name
        try:
            meta = probe_media(tmp_path)
            duration = float(meta.get("duration") or meta.get("duration_sec") or 0.0)
            if duration > 0:
                return duration
        except Exception:
            pass
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
    return 0.0


def _prepare_seedance_video_file(upload: Any, raw: bytes) -> tuple[bytes, str, float]:
    ext = _guess_seedance_video_ext(
        filename=getattr(upload, "filename", None),
        content_type=getattr(upload, "content_type", None),
        raw=raw,
    )
    if ext not in SEEDANCE_VIDEO_ALLOWED_EXTS:
        raise HTTPException(status_code=400, detail="Для Seedance 2.0 video refs доступны только MP4 или MOV.")
    duration_sec = _probe_seedance_video_duration_seconds(raw, ext)
    if duration_sec <= 0:
        raise HTTPException(status_code=400, detail="Не удалось определить длительность video reference для Seedance 2.0.")
    if duration_sec > SEEDANCE_VIDEO_TOTAL_MAX_DURATION_SEC:
        raise HTTPException(status_code=400, detail="Для Seedance 2.0 video reference не должен быть длиннее 15.4 секунды.")
    return raw, ext, duration_sec


CHAT_MODEL_LABEL_DEFAULT = "gpt-4o-mini"
PROMPT_MODEL_LABEL = "gpt-5.4"
MAX_CHAT_ATTACHMENTS = 5
MAX_CHAT_IMAGE_ATTACHMENTS = 4
MAX_CHAT_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_CHAT_ATTACHMENT_TEXT_PER_FILE = 50000
MAX_CHAT_ATTACHMENT_TEXT_TOTAL = 50000
MAX_CHAT_SUMMARY_CHARS = KIE_CLAUDE_SUMMARY_MAX_CHARS
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



def _sanitize_chat_summary(value: Any) -> str:
    text = str(value or "").replace("\x00", "").strip()
    if len(text) > MAX_CHAT_SUMMARY_CHARS:
        return text[:MAX_CHAT_SUMMARY_CHARS]
    return text


def _dedupe_latest_user_from_history(history: List[Dict[str, str]], latest_text: str) -> List[Dict[str, str]]:
    if not history:
        return []
    latest = str(latest_text or "").strip()
    if latest and history[-1].get("role") == "user":
        last_content = str(history[-1].get("content") or "").strip()
        if last_content == latest or last_content.startswith(latest + "\n\n📎 Файлы:"):
            return history[:-1]
    return history


async def _prepare_workspace_claude_memory(history: List[Dict[str, str]], summary: str) -> Dict[str, Any]:
    cleaned = [m for m in (history or []) if isinstance(m, dict) and m.get("role") in ("user", "assistant") and str(m.get("content") or "").strip()]
    current_summary = _sanitize_chat_summary(summary)
    recent = cleaned[-KIE_CLAUDE_HISTORY_MESSAGES:]
    overflow = cleaned[:-KIE_CLAUDE_HISTORY_MESSAGES]
    if overflow:
        try:
            current_summary = await kie_claude_summarize_dialogue(
                messages=overflow,
                previous_summary=current_summary,
                max_chars=MAX_CHAT_SUMMARY_CHARS,
            )
        except Exception:
            current_summary = _sanitize_chat_summary(current_summary)
    return {"summary": _sanitize_chat_summary(current_summary), "history": recent}


def _resolve_workspace_chat_model(requested_model: Any, mode: str) -> Dict[str, str]:
    mode_value = _normalize_chat_mode_value(mode)
    requested = str(requested_model or "").strip()
    if mode_value == "prompt_builder":
        return {"label": PROMPT_MODEL_LABEL, "actual": PROMPT_BUILDER_MODEL}
    if is_kie_claude_model(requested):
        return {"label": KIE_CLAUDE_DISPLAY_NAME, "actual": KIE_CLAUDE_MODEL_ID}
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


def _build_prompt_builder_system_prompt(model_label: str, image_refs: List[str], audio_refs: List[str]) -> str:
    ref_hint = ""
    if image_refs or audio_refs:
        seedance_bits: List[str] = []
        if image_refs:
            seedance_bits.append("изображения: " + ", ".join(image_refs))
        if audio_refs:
            seedance_bits.append("аудио: " + ", ".join(audio_refs))
        ref_hint = (
            " Если пользователь просит prompt для Seedance и приложены референсы, используй теги "
            + "; ".join(seedance_bits)
            + ". Используй только реально загруженные теги. Для Kling используй формат @image_1, @image_2. Для Veo, Sora и Nano Banana не придумывай inline-теги, если они не нужны."
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
    if ctype.startswith("audio/") or ext in {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}:
        return "audio"
    if ctype.startswith("video/") or ext in {".mp4", ".mov", ".webm", ".mkv"}:
        return "video"
    if ext == ".pdf" or ctype == "application/pdf":
        return "pdf"
    if ext == ".docx" or ctype == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        return "docx"
    if ext in _TEXT_ATTACHMENT_EXTS or ctype.startswith("text/") or ctype in {
        "application/json", "application/xml", "text/csv", "application/javascript"
    }:
        return "text"
    return "binary"


async def _prepare_workspace_chat_attachments(files: List[UploadFile], *, user_id: Any = None, origin: str = "workspace") -> Dict[str, Any]:
    """Prepare chat attachments without putting raw bytes into Redis.

    Raw files are uploaded to Supabase Storage. The async chat worker receives only
    storage refs plus extracted text/context. For the legacy synchronous /chat route
    we still keep image_bytes_list in memory only; it is never enqueued to Redis.
    """
    prepared: List[Dict[str, Any]] = []
    notices: List[str] = []
    text_blocks: List[str] = []
    image_bytes_list: List[bytes] = []
    image_storage_refs: List[Dict[str, Any]] = []
    total_text = 0

    storage_enabled = is_chat_storage_configured()
    if files and not storage_enabled:
        notices.append(
            "Supabase Storage для chat attachments не настроен: файлы будут обработаны, "
            "но ссылки storage_path не будут созданы."
        )

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
            notices.append(f"{filename}: файл больше 10 МБ, пропущен.")
            prepared.append(item)
            continue

        if storage_enabled:
            try:
                storage_ref = await upload_chat_attachment_bytes(
                    raw,
                    filename=filename,
                    content_type=content_type,
                    user_id=user_id,
                    origin=origin,
                )
                item.update(storage_ref)
            except Exception as exc:
                notices.append(f"{filename}: не удалось загрузить в Supabase Storage ({exc}).")
        else:
            item["storage_bucket"] = CHAT_ATTACHMENTS_BUCKET
            item["storage_path"] = ""

        if kind == "image":
            if len(image_bytes_list) < MAX_CHAT_IMAGE_ATTACHMENTS:
                # Only for the old synchronous endpoint. Async jobs use image_storage_refs instead.
                image_bytes_list.append(raw)
                if item.get("storage_path"):
                    image_storage_refs.append({
                        "name": filename,
                        "kind": kind,
                        "content_type": content_type,
                        "size_bytes": size_bytes,
                        "storage_bucket": item.get("storage_bucket") or CHAT_ATTACHMENTS_BUCKET,
                        "storage_path": item.get("storage_path") or "",
                        "storage_url": item.get("storage_url") or "",
                    })
                item["parsed"] = True
            else:
                notices.append(
                    f"{filename}: превышен лимит изображений, учитываю только первые {MAX_CHAT_IMAGE_ATTACHMENTS}."
                )
            prepared.append(item)
            continue

        extracted = ""
        if kind in {"text", "docx", "pdf"}:
            _kind, extracted, notice = extract_file_text(raw, filename, content_type)
            if notice:
                notices.append(notice)
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

    summary_lines = []
    for item in prepared:
        line = f"- {item['name']} · {item['kind']} · {max(1, round((item['size_bytes'] or 0) / 1024))} KB"
        if item.get("storage_path"):
            line += f" · storage: {item.get('storage_bucket')}/{item.get('storage_path')}"
        summary_lines.append(line)

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
        "image_storage_refs": image_storage_refs,
    }


def _chat_models() -> List[str]:
    out: List[str] = []
    for m in [OPENAI_CHAT_MODEL, PROMPT_BUILDER_MODEL, KIE_CLAUDE_MODEL_ID]:
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
WORKSPACE_UDIO_COST_TOKENS = max(0, int(os.getenv("WORKSPACE_UDIO_COST_TOKENS", "0") or "0"))
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

_WORKSPACE_IMAGE_OPTIONAL_COLUMNS = {
    "preset_slug",
    "source_image_url",
    "before_image_url",
    "after_image_url",
    "compare_mode",
    "provider_task_id",
    "image_urls_json",
    "storage_paths_json",
    "available_actions_json",
    "parent_generation_id",
    "action_type",
    "selected_image_no",
    "negative_prompt",
    "mj_stylize",
    "mj_chaos",
    "mj_raw",
    "mj_speed_mode",
    "mj_seed",
    "style_ref_urls_json",
    "omni_ref_url",
}

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


def _parse_json_list_form(value: Any) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    text = str(value or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except Exception:
        return []
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    return []


def _normalize_workspace_video_resolution(provider: str, model: str, resolution: Any) -> str:
    value = str(resolution or "").strip().lower()
    if provider == "kling" and model == "kling-3.0-new":
        return normalize_kling3_kie_mode(resolution or "pro")
    if provider == "veo":
        return "1080p" if model == "veo-3.1-pro" else "720p"
    if provider == "grok":
        return normalize_grok_resolution(value or "480p")
    if provider == "seedance_kie":
        normalized_model = normalize_seedance_kie_model(model)
        return "480p" if normalized_model == "seedance-kie-fast" else "720p"
    if provider == "pixverse_c1":
        return normalize_pixverse_c1_quality(value or "720p")
    if value in {"720", "720p"}:
        return "720"
    if value in {"1080", "1080p"}:
        return "1080"
    return "720"


def _normalize_switchx_resolution(value: Any) -> str:
    text = str(value or "1080").strip().lower()
    return "720" if text in {"720", "720p"} else "1080"


def _normalize_switchx_alpha_mode(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in {"auto", "fill"} else "auto"


def _switchx_tokens_per_sec(resolution: Any) -> int:
    return SWITCHX_TOKENS_PER_SEC_720 if _normalize_switchx_resolution(resolution) == "720" else SWITCHX_TOKENS_PER_SEC_1080


def _normalize_switchx_source_duration_seconds(value: Any) -> int:
    try:
        raw = float(value or 0)
    except Exception:
        raw = 0.0
    if raw <= 0:
        return 0
    return max(1, int(math.floor(raw + 0.5)))


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
    provider_mode: str,
    start_frame: Optional[bytes],
    end_frame: Optional[bytes],
    last_frame: Optional[bytes],
    avatar_image: Optional[bytes],
    motion_video: Optional[bytes],
    reference_images: List[bytes],
    reference_audio_clips: List[bytes],
    reference_video_clips: List[bytes],
    source_video_upload_id: Optional[str] = None,
    reference_image_url: Optional[str] = None,
    switchx_alpha_mode: Optional[str] = None,
    switchx_select_mask_url: Optional[str] = None,
    charge_tokens: int = 0,
    charge_ref_id: str = "",
    refund_reason: str = "workspace_video_refund",
) -> None:
    try:
        provider_mode = normalize_grok_provider_mode(provider_mode or "normal")
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

        elif provider == "grok":
            if mode == "image_to_video":
                if not start_frame:
                    raise RuntimeError("Для Grok Image→Video нужен start_frame")
                provider_video_url = await run_grok_image_to_video(
                    user_id=user_id,
                    image_bytes=start_frame,
                    prompt=prompt,
                    duration=duration,
                    resolution=resolution,
                    aspect_ratio=aspect_ratio,
                    provider_mode=provider_mode,
                )
            else:
                provider_video_url = await run_grok_text_to_video(
                    prompt=prompt,
                    duration=duration,
                    resolution=resolution,
                    aspect_ratio=aspect_ratio,
                    provider_mode=provider_mode,
                )

        elif provider == "pixverse_c1":
            pixverse_video_id = ""
            normalized_quality = normalize_pixverse_c1_quality(resolution)
            normalized_duration = normalize_pixverse_c1_duration(duration)
            normalized_mode = normalize_pixverse_c1_mode(mode)
            normalized_aspect_ratio = normalize_pixverse_c1_aspect_ratio(aspect_ratio)
            if normalized_mode == "image_to_video":
                if not start_frame:
                    raise RuntimeError("Для PixVerse C1 Image→Video нужен start_frame")
                uploaded = await upload_pixverse_image(image_bytes=start_frame, filename_hint="pixverse_start_frame")
                pixverse_video_id = await create_pixverse_c1_image_to_video(
                    prompt=prompt,
                    duration=normalized_duration,
                    quality=normalized_quality,
                    start_frame_img_id=int(uploaded["img_id"]),
                    generate_audio=True,
                )
            elif normalized_mode == "transition":
                if not start_frame or not last_frame:
                    raise RuntimeError("Для PixVerse C1 Transition нужны первый и последний кадр")
                first_uploaded = await upload_pixverse_image(image_bytes=start_frame, filename_hint="pixverse_first_frame")
                last_uploaded = await upload_pixverse_image(image_bytes=last_frame, filename_hint="pixverse_last_frame")
                pixverse_video_id = await create_pixverse_c1_transition(
                    prompt=prompt,
                    duration=normalized_duration,
                    quality=normalized_quality,
                    first_frame_img_id=int(first_uploaded["img_id"]),
                    last_frame_img_id=int(last_uploaded["img_id"]),
                    generate_audio=True,
                )
            elif normalized_mode == "fusion":
                refs_payload: List[Dict[str, Any]] = []
                for idx, raw in enumerate((reference_images or [])[:7], start=1):
                    if not raw:
                        continue
                    uploaded = await upload_pixverse_image(image_bytes=raw, filename_hint=f"pixverse_ref_{idx}")
                    refs_payload.append(
                        {
                            "img_id": int(uploaded["img_id"]),
                            "ref_name": f"image{idx}",
                            "type": "subject",
                        }
                    )
                if not refs_payload:
                    raise RuntimeError("Для PixVerse C1 Fusion нужен хотя бы один reference image")
                pixverse_video_id = await create_pixverse_c1_fusion(
                    prompt=prompt,
                    duration=normalized_duration,
                    quality=normalized_quality,
                    aspect_ratio=normalized_aspect_ratio,
                    image_references=refs_payload,
                    generate_audio=True,
                )
            else:
                pixverse_video_id = await create_pixverse_c1_text_to_video(
                    prompt=prompt,
                    duration=normalized_duration,
                    quality=normalized_quality,
                    aspect_ratio=normalized_aspect_ratio,
                    generate_audio=True,
                )
            if not pixverse_video_id:
                raise RuntimeError("PixVerse C1 не вернул video_id")
            _update_workspace_generation(generation_id, {"task_id": str(pixverse_video_id), "status": "processing"})
            provider_video_url = await wait_for_pixverse_video(pixverse_video_id)

        elif provider == "seedance_kie":
            if mode == "image_to_video" and not (reference_images or start_frame or last_frame):
                raise RuntimeError("Для Seedance 2.0 Image→Video нужен хотя бы один image reference")
            if mode == "image_to_video":
                provider_video_url = await run_seedance_kie_image_to_video(
                    user_id=user_id,
                    model=model,
                    prompt=prompt,
                    duration=duration,
                    aspect_ratio=aspect_ratio,
                    start_frame=start_frame,
                    last_frame=last_frame,
                    reference_images=reference_images,
                    reference_audios=reference_audio_clips,
                )
            elif mode == "omni_reference":
                provider_video_url = await run_seedance_kie_omni_reference(
                    user_id=user_id,
                    model=model,
                    prompt=prompt,
                    duration=duration,
                    aspect_ratio=aspect_ratio,
                    reference_images=reference_images,
                    reference_videos=reference_video_clips,
                    reference_audios=reference_audio_clips,
                )
            else:
                provider_video_url = await run_seedance_kie_text_to_video(
                    model=model,
                    prompt=prompt,
                    duration=duration,
                    aspect_ratio=aspect_ratio,
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

        elif provider == "switchx":
            upload_id = str(source_video_upload_id or "").strip()
            ref_url = str(reference_image_url or "").strip()
            alpha_mode = _normalize_switchx_alpha_mode(switchx_alpha_mode)
            select_mask_url = str(switchx_select_mask_url or "").strip()
            if not upload_id:
                raise RuntimeError("Для SwitchX нужен source video upload id")
            if not ref_url:
                raise RuntimeError("Для SwitchX нужен reference image")
            if alpha_mode == "select" and not select_mask_url:
                raise RuntimeError("Для SwitchX Select нужен alpha mask 1-го кадра")
            row = get_workspace_upload_row(int(user_id), upload_id)
            if not row:
                raise RuntimeError(f"SwitchX source video not found: {upload_id}")
            access = _build_workspace_video_access_urls(
                storage_path=row.get("storage_path"),
                fallback_url=row.get("download_url") or row.get("video_url"),
                expires_in=3600,
            )
            source_url = str(access.get("download_url") or access.get("video_url") or "").strip()
            if not source_url:
                raise RuntimeError("SwitchX source video URL missing")
            local_source_path = ""
            source_bytes = b""
            source_content_type = str(row.get("mime_type") or "video/mp4").split(";", 1)[0].strip() or "video/mp4"
            try:
                local_source_path, _, downloaded_ctype = await _download_video_to_tempfile(source_url)
                if downloaded_ctype:
                    source_content_type = downloaded_ctype
                meta = probe_media(local_source_path)
                frame_count = int(meta.get("frame_count") or 0)
                width = int(meta.get("width") or 0)
                height = int(meta.get("height") or 0)
                if frame_count > 240:
                    raise RuntimeError(f"SwitchX принимает максимум 240 кадров. У этого видео: {frame_count}.")
                if width > 0 and height > 0 and (width * height) > 2770000:
                    raise RuntimeError(f"SwitchX принимает максимум 2,770,000 px на кадр. Сейчас: {width}×{height}.")
                with open(local_source_path, "rb") as fh:
                    source_bytes = fh.read()
            finally:
                if local_source_path:
                    try:
                        os.remove(local_source_path)
                    except Exception:
                        pass
            if not source_bytes:
                raise RuntimeError("SwitchX source video is empty")
            ref_bytes, ref_ext = await _download_workspace_image_bytes(ref_url)
            ref_content_type = _workspace_image_content_type(ref_ext)
            alpha_uri = None
            client = SwitchXClient()
            source_upload = await client.create_and_upload(
                filename=str(row.get("filename") or f"switchx_source_{upload_id}.mp4"),
                file_bytes=source_bytes,
                content_type=source_content_type,
            )
            ref_upload = await client.create_and_upload(
                filename=f"switchx_reference.{ref_ext or 'png'}",
                file_bytes=ref_bytes,
                content_type=ref_content_type,
            )
            if alpha_mode == "select":
                alpha_bytes, alpha_ext = await _download_workspace_image_bytes(select_mask_url)
                alpha_upload = await client.create_and_upload(
                    filename=f"switchx_alpha.{alpha_ext or 'png'}",
                    file_bytes=alpha_bytes,
                    content_type=_workspace_image_content_type(alpha_ext),
                )
                alpha_uri = alpha_upload.beeble_uri
            created = await client.start_generation(
                source_uri=source_upload.beeble_uri,
                reference_image_uri=ref_upload.beeble_uri,
                prompt=str(prompt or "").strip(),
                alpha_mode=alpha_mode,
                alpha_uri=alpha_uri,
                max_resolution=int(_normalize_switchx_resolution(resolution)),
                idempotency_key=generation_id,
            )
            if created.id:
                _update_workspace_generation(generation_id, {"task_id": str(created.id), "status": "processing"})
            done = await client.wait_until_done(created.id, timeout_sec=3600, poll_sec=8.0)
            if str(done.status or "").strip().lower() != "completed":
                raise RuntimeError(done.error or f"SwitchX status: {done.status}")
            provider_video_url = str(done.render_url or "").strip()
            if not provider_video_url:
                raise RuntimeError("SwitchX completed but returned no render url")

        else:
            raise RuntimeError(f"Провайдер {provider} пока не поддержан в workspace video run")

        if not provider_video_url:
            raise RuntimeError("Provider did not return video url")

        await _finalize_workspace_generation_from_url(
            generation_id=generation_id,
            user_id=user_id,
            provider_video_url=provider_video_url,
        )
    except (Kling3Error, KlingFlowError, VeoFlowError, GrokVideoError, PixVerseC1Error, SwitchXError, ValueError, RuntimeError, TimeoutError) as e:
        _mark_workspace_generation_failed(generation_id, str(e), error_code="provider_error")
        if int(charge_tokens or 0) > 0:
            try:
                add_tokens(
                    int(user_id),
                    int(charge_tokens),
                    reason=str(refund_reason or "workspace_video_refund"),
                    ref_id=charge_ref_id or uuid4().hex,
                    meta={"origin": "workspace_video", "generation_id": generation_id, "error": str(e)[:300]},
                )
            except Exception:
                pass
    except Exception as e:
        _mark_workspace_generation_failed(generation_id, f"Internal run error: {e}", error_code="internal_error")
        if int(charge_tokens or 0) > 0:
            try:
                add_tokens(
                    int(user_id),
                    int(charge_tokens),
                    reason=str(refund_reason or "workspace_video_refund"),
                    ref_id=charge_ref_id or uuid4().hex,
                    meta={"origin": "workspace_video", "generation_id": generation_id, "error": str(e)[:300]},
                )
            except Exception:
                pass



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
    summary: Optional[str] = Field(default="", max_length=10000)
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


class WorkspaceImageActionIn(BaseModel):
    generation_id: str
    action: str
    image_no: Optional[int] = None
    variation_type: Optional[str] = None
    speed_mode: Optional[str] = None


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


def _workspace_json_field(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return default
        try:
            parsed = json.loads(raw)
        except Exception:
            return default
        if isinstance(default, list):
            return parsed if isinstance(parsed, list) else default
        if isinstance(default, dict):
            return parsed if isinstance(parsed, dict) else default
        return parsed
    return default


def _workspace_boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _workspace_speed_mode(value: Any, default: str = "fast") -> str:
    return normalize_midjourney_speed_mode(value, default=default)


def _workspace_midjourney_action_cost(action_type: str, speed_mode: str) -> int:
    action = str(action_type or "generate").strip().lower() or "generate"
    speed = _workspace_speed_mode(speed_mode)
    if action in {"variation", "variation_subtle", "variation_strong"}:
        return 2 if speed == "turbo" else 1
    if action == "reroll":
        return 2 if speed == "turbo" else 1
    return 2 if speed == "turbo" else 1


def _workspace_midjourney_action_reason(action_type: str) -> str:
    action = str(action_type or "generate").strip().lower() or "generate"
    if action in {"variation", "variation_subtle", "variation_strong"}:
        return "workspace_midjourney_variation"
    if action == "reroll":
        return "workspace_midjourney_reroll"
    return "workspace_midjourney_generate"



def _serialize_workspace_image_generation(row: Dict[str, Any]) -> Dict[str, Any]:
    image_urls = _workspace_json_field(row.get("image_urls_json"), [])
    if not image_urls:
        image_urls = [str(item or "").strip() for item in (row.get("image_urls") or []) if str(item or "").strip()]
    image_url = _first_nonempty(row.get("download_url"), row.get("image_url"), row.get("after_image_url"), image_urls[0] if image_urls else None)
    before_image_url = _first_nonempty(row.get("before_image_url"), row.get("source_image_url"))
    after_image_url = _first_nonempty(row.get("after_image_url"), image_url)
    compare_mode = bool(row.get("compare_mode")) and bool(before_image_url and after_image_url)
    available_actions = _workspace_json_field(row.get("available_actions_json"), {})
    style_ref_urls = _workspace_json_field(row.get("style_ref_urls_json"), [])
    storage_paths = _workspace_json_field(row.get("storage_paths_json"), [])
    speed_mode = _workspace_speed_mode(row.get("mj_speed_mode"), default="fast")
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
        "storage_paths": storage_paths,
        "image_url": image_url,
        "image_urls": image_urls,
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
        "provider_task_id": row.get("provider_task_id"),
        "available_actions": available_actions,
        "parent_generation_id": row.get("parent_generation_id"),
        "action_type": row.get("action_type"),
        "selected_image_no": row.get("selected_image_no"),
        "negative_prompt": row.get("negative_prompt"),
        "mj_stylize": row.get("mj_stylize"),
        "mj_chaos": row.get("mj_chaos"),
        "mj_raw": _workspace_boolish(row.get("mj_raw")),
        "mj_speed_mode": speed_mode,
        "mj_seed": row.get("mj_seed"),
        "style_ref_urls": style_ref_urls,
        "omni_ref_url": row.get("omni_ref_url"),
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
    if provider == "midjourney":
        return base
    return base



def _workspace_gpt_image_2_size(aspect_ratio: Any, mode: str = "text_to_image") -> str:
    ratio = str(aspect_ratio or "").strip()
    mode_value = str(mode or "").strip().lower()
    if mode_value == "image_to_image" and ratio == "match_input_image":
        return "1024x1024"
    mapping = {
        "1:1": "1024x1024",
        "4:5": "1024x1280",
        "9:16": "864x1536",
        "16:9": "1536x864",
        "3:4": "1024x1360",
        "4:3": "1360x1024",
    }
    return mapping.get(ratio, "1024x1024")


def _workspace_image_cost(provider: str, mode: str, preset_slug: str = "", resolution: str = "2K", speed_mode: str = "fast", action_type: str = "generate") -> int:
    provider_key = str(provider or "").strip().lower()
    mode_key = str(mode or "").strip().lower()
    preset_key = str(preset_slug or "").strip().lower()
    resolution_key = str(resolution or "2K").strip().upper() or "2K"

    if provider_key == "nano_banana":
        return 1
    if provider_key == "nano_banana_2":
        return 2 if resolution_key == "4K" else 1
    if provider_key == "nano_banana_pro":
        return 2
    if provider_key == "nano_banana_pro_new":
        return 2 if resolution_key == "4K" else 1
    if provider_key == "seedream":
        return 0 if mode_key in {"text_to_image", "t2i"} else 1
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
    if provider_key == "gpt_image_2":
        return 0
    if provider_key == "midjourney":
        return _workspace_midjourney_action_cost(action_type, speed_mode)

    raise HTTPException(status_code=400, detail=f"Unsupported image provider: {provider_key} / {mode_key}")


def _workspace_image_charge_reason(provider: str, mode: str, action_type: str = "generate") -> Optional[str]:
    provider_key = str(provider or "").strip().lower()
    mode_key = str(mode or "").strip().lower()

    if provider_key == "nano_banana":
        return "nano_banana"
    if provider_key == "nano_banana_2":
        return "nano_banana_2"
    if provider_key == "nano_banana_pro":
        return "nano_banana_pro"
    if provider_key == "nano_banana_pro_new":
        return "nano_banana_pro_new"
    if provider_key == "seedream":
        if mode_key in {"single", "seedream_45", "seedream_single"}:
            return "seedream_45_single"
        if mode_key in {"image_to_image", "i2i"}:
            return "two_photos"
        return None
    if provider_key == "photosession":
        return "photosession_generation"
    if provider_key == "two_images":
        return "two_photos"
    if provider_key == "topaz_photo":
        return "workspace_topaz_photo"
    if provider_key == "gpt_image_2":
        return None
    if provider_key == "midjourney":
        return _workspace_midjourney_action_reason(action_type)

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


@router.get("/balance/history")
async def workspace_balance_history(limit: int = 30, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    uid = int(user.get("workspace_user_id") or user["telegram_user_id"])
    ensure_user_row(uid)
    items = get_balance_history(uid, limit=limit)
    balance = int(get_balance(uid) or 0)
    return {"ok": True, "items": items, "balance_tokens": balance}


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
        summary_value = _sanitize_chat_summary(form.get("summary"))
        temperature = _clamp_float(form.get("temperature"), 0.6, 0.0, 1.5)
        max_tokens = _clamp_int(form.get("max_tokens"), 900, 150, 4000)
        resolved_model = _resolve_workspace_chat_model(form.get("model"), mode)
        files = [f for f in form.getlist("files") if getattr(f, "filename", None)]
    else:
        payload = WorkspaceChatIn.model_validate(await request.json())
        text_value = payload.text.strip()
        mode = _normalize_chat_mode_value(payload.mode)
        history = [{"role": item.role, "content": item.content} for item in (payload.history or []) if item.role in ("user", "assistant")]
        summary_value = _sanitize_chat_summary(payload.summary)
        temperature = payload.temperature
        max_tokens = payload.max_tokens
        resolved_model = _resolve_workspace_chat_model(payload.model, mode)

    if not text_value and not files:
        raise HTTPException(status_code=400, detail="Введите текст или прикрепите хотя бы один файл.")

    prepared_files = await _prepare_workspace_chat_attachments(files, user_id=user.get("telegram_user_id"), origin="workspace-sync") if files else {"items": [], "context": "", "image_bytes_list": [], "image_storage_refs": []}
    image_refs = [f"@image{i}" for i in range(1, len(prepared_files.get("image_bytes_list") or []) + 1)]
    audio_count = sum(1 for item in (prepared_files.get("items") or []) if str(item.get("kind") or "") == "audio")
    audio_refs = [f"@audio{i}" for i in range(1, audio_count + 1)]

    user_text = text_value or "Проанализируй приложенные файлы и кратко скажи, что в них находится, затем предложи полезные следующие шаги."
    if prepared_files.get("context"):
        user_text = f"{user_text}\n\n{prepared_files['context']}"

    history = _dedupe_latest_user_from_history(history, text_value)

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
        system_prompt = _build_prompt_builder_system_prompt(model_label, image_refs, audio_refs)
    else:
        system_prompt = (
            "Ты — AstraBot Workspace Assistant. "
            "Помогай как product-minded AI co-pilot: сценарии, промпты, creative direction, тексты, планы и упаковка идей в рабочий пайплайн. "
            "Пиши по делу, понятно и удобно для дальнейшего запуска в video/image/voice/music студиях. "
            f"Если пользователь спрашивает, какая модель выбрана в интерфейсе, отвечай только названием модели: {model_label}."
        )

    response_summary = summary_value
    if is_kie_claude_model(model_actual) and mode == "chat":
        memory = await _prepare_workspace_claude_memory(history, summary_value)
        response_summary = memory.get("summary") or ""
        answer = await kie_claude_answer(
            user_text=user_text,
            system_prompt=system_prompt,
            history=memory.get("history") or [],
            summary=response_summary,
            max_tokens=1500,
            thinking=True,
        )
    else:
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
        "summary": response_summary,
        "attachments": prepared_files.get("items") or [],
        "is_prompt": _is_prompt_builder_output(answer) if mode == "prompt_builder" else False,
    }


@router.post("/chat/async")
async def workspace_chat_async(request: Request, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    content_type = (request.headers.get("content-type") or "").lower()
    files: List[UploadFile] = []

    if "multipart/form-data" in content_type:
        form = await request.form()
        text_value = str(form.get("text") or "").strip()
        mode = _normalize_chat_mode_value(form.get("mode"))
        history = _sanitize_chat_history(form.get("history"))
        summary_value = _sanitize_chat_summary(form.get("summary"))
        temperature = _clamp_float(form.get("temperature"), 0.6, 0.0, 1.5)
        max_tokens = _clamp_int(form.get("max_tokens"), 900, 150, 4000)
        resolved_model = _resolve_workspace_chat_model(form.get("model"), mode)
        files = [f for f in form.getlist("files") if getattr(f, "filename", None)]
    else:
        payload = WorkspaceChatIn.model_validate(await request.json())
        text_value = payload.text.strip()
        mode = _normalize_chat_mode_value(payload.mode)
        history = [{"role": item.role, "content": item.content} for item in (payload.history or []) if item.role in ("user", "assistant")]
        summary_value = _sanitize_chat_summary(payload.summary)
        temperature = payload.temperature
        max_tokens = payload.max_tokens
        resolved_model = _resolve_workspace_chat_model(payload.model, mode)

    if not text_value and not files:
        raise HTTPException(status_code=400, detail="Введите текст или прикрепите хотя бы один файл.")

    prepared_files = await _prepare_workspace_chat_attachments(files, user_id=user.get("telegram_user_id"), origin="workspace-async") if files else {"items": [], "context": "", "image_bytes_list": [], "image_storage_refs": []}
    image_refs = [f"@image{i}" for i in range(1, len(prepared_files.get("image_bytes_list") or []) + 1)]
    audio_count = sum(1 for item in (prepared_files.get("items") or []) if str(item.get("kind") or "") == "audio")
    audio_refs = [f"@audio{i}" for i in range(1, audio_count + 1)]

    user_text = text_value or "Проанализируй приложенные файлы и кратко скажи, что в них находится, затем предложи полезные следующие шаги."
    if prepared_files.get("context"):
        user_text = f"{user_text}\n\n{prepared_files['context']}"

    history = _dedupe_latest_user_from_history(history, text_value)
    model_label = resolved_model["label"]
    model_actual = resolved_model["actual"]

    if mode == "prompt_builder":
        if not _is_prompt_builder_request(text_value, bool(files)):
            answer = _prompt_builder_redirect_message()
            job_id = str(uuid4())
            await create_chat_job_status(job_id, {
                "status": "completed",
                "ok": True,
                "user_id": str(user.get("telegram_user_id") or ""),
                "answer": answer,
                "mode": mode,
                "model": model_label,
                "resolved_model": model_actual,
                "attachments": prepared_files.get("items") or [],
                "is_prompt": False,
            })
            return {"ok": True, "job_id": job_id, "status": "completed", "answer": answer, "is_prompt": False}
        system_prompt = _build_prompt_builder_system_prompt(model_label, image_refs, audio_refs)
    else:
        system_prompt = (
            "Ты — AstraBot Workspace Assistant. "
            "Помогай как product-minded AI co-pilot: сценарии, промпты, creative direction, тексты, планы и упаковка идей в рабочий пайплайн. "
            "Пиши по делу, понятно и удобно для дальнейшего запуска в video/image/voice/music студиях. "
            f"Если пользователь спрашивает, какая модель выбрана в интерфейсе, отвечай только названием модели: {model_label}."
        )

    if is_kie_claude_model(model_actual) and mode == "chat":
        model_key = "claude"
        queue_name = WORKSPACE_CHAT_CLAUDE_QUEUE_NAME
        history_for_model = history[-KIE_CLAUDE_HISTORY_MESSAGES:]
        response_summary = summary_value[:KIE_CLAUDE_SUMMARY_MAX_CHARS]
    else:
        model_key = "openai"
        queue_name = WORKSPACE_CHAT_OPENAI_QUEUE_NAME
        history_for_model = history
        response_summary = summary_value

    job_id = str(uuid4())
    await create_chat_job_status(job_id, {
        "status": "queued",
        "ok": True,
        "user_id": str(user.get("telegram_user_id") or ""),
        "mode": mode,
        "model": model_label,
        "resolved_model": model_actual,
        "attachments": prepared_files.get("items") or [],
    })
    job = {
        "kind": "workspace_ai_chat",
        "job_id": job_id,
        "user_id": str(user.get("telegram_user_id") or ""),
        "model_key": model_key,
        "model_label": model_label,
        "model_actual": model_actual,
        "mode": mode,
        "user_text": user_text,
        "system_prompt": system_prompt,
        "history": history_for_model,
        "summary": response_summary,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "attachments": prepared_files.get("items") or [],
        "image_storage_refs": prepared_files.get("image_storage_refs") or [],
        "is_prompt_builder": mode == "prompt_builder",
    }
    try:
        await enqueue_job(job, queue_name=queue_name)
    except Exception as exc:
        await set_chat_job_status(job_id, status="failed", ok=False, error=str(exc))
        raise HTTPException(status_code=503, detail=f"Чат-очередь недоступна: {exc}")

    return {"ok": True, "job_id": job_id, "status": "queued"}


@router.get("/chat/status/{job_id}")
async def workspace_chat_status(job_id: str, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    data = await get_chat_job_status(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="chat job not found")
    owner_id = str(data.get("user_id") or "")
    current_id = str(user.get("telegram_user_id") or "")
    if owner_id and current_id and owner_id != current_id:
        raise HTTPException(status_code=404, detail="chat job not found")
    return data


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


def _workspace_video_charge_spec(
    *,
    provider: str,
    model: str,
    mode: str,
    duration: int,
    resolution: str,
    enable_audio: bool,
    quality: str,
    has_seedance_video_reference: bool = False,
    kling3_kie_multi_shots: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    duration = max(1, int(duration or 0))

    if provider == "kling":
        if model == "kling-3.0-new":
            normalized_mode = normalize_kling3_kie_mode(resolution or "pro")
            normalized_generation_mode = normalize_kling3_kie_generation_mode(mode)
            shots = normalize_kling3_kie_shots(kling3_kie_multi_shots or []) if normalized_generation_mode == "multi_shot" else []
            bill_seconds = kling3_kie_billable_seconds(duration=duration, multi_shots=shots if shots else None)
            tokens = int(calculate_kling3_kie_price(
                mode=normalized_mode,
                enable_audio=bool(enable_audio),
                duration=bill_seconds,
                multi_shots=shots if shots else None,
            ))
            return {
                "tokens": tokens,
                "charge_reason": "kling3_kie_create",
                "refund_reason": "kling3_kie_refund",
                "meta": {
                    "origin": "workspace_video",
                    "provider": provider,
                    "model": "Kling 3.0 - New",
                    "provider_model": model,
                    "generation_mode": normalized_generation_mode,
                    "duration": bill_seconds,
                    "mode": normalized_mode,
                    "enable_audio": bool(enable_audio),
                    "multi_shots": len(shots),
                },
            }
        if model == "kling-3.0":
            tokens = int(calculate_kling3_price(
                resolution=str(resolution or "720").replace("p", ""),
                enable_audio=bool(enable_audio),
                duration=duration,
            ))
            return {
                "tokens": tokens,
                "charge_reason": "kling3_create",
                "refund_reason": "kling3_refund",
                "meta": {
                    "origin": "workspace_video",
                    "provider": provider,
                    "model": model,
                    "mode": mode,
                    "duration": duration,
                    "resolution": str(resolution or "720"),
                    "enable_audio": bool(enable_audio),
                },
            }
        if model == "kling-2.5":
            tokens = int(duration)
            return {
                "tokens": tokens,
                "charge_reason": "kling_video",
                "refund_reason": "kling_video_refund",
                "meta": {"origin": "workspace_video", "provider": provider, "model": model, "mode": mode, "duration": duration},
            }
        if model == "kling-1.6":
            rate = 1 if quality == "standard" else 2
            tokens = int(duration) * int(rate)
            return {
                "tokens": tokens,
                "charge_reason": "kling_video",
                "refund_reason": "kling_video_refund",
                "meta": {"origin": "workspace_video", "provider": provider, "model": model, "mode": mode, "duration": duration, "quality": quality},
            }
        if model == "motion-control":
            return {
                "tokens": 0,
                "charge_reason": "",
                "refund_reason": "workspace_video_refund",
                "meta": {"origin": "workspace_video", "provider": provider, "model": model, "mode": mode, "billing": "todo_motion_control"},
            }

    if provider == "veo":
        ch = calc_veo_charge(
            veo_model=("pro" if model == "veo-3.1-pro" else "fast"),
            model_slug=model,
            generate_audio=bool(enable_audio),
            duration_sec=duration,
        )
        return {
            "tokens": int(ch.total_tokens),
            "charge_reason": "veo_video",
            "refund_reason": "veo_video_refund",
            "meta": {
                "origin": "workspace_video",
                "provider": provider,
                "model": model,
                "mode": mode,
                "duration": int(ch.duration_sec),
                "tokens_per_sec": int(ch.tokens_per_sec),
                "enable_audio": bool(enable_audio),
                "tier": ch.tier,
            },
        }

    if provider == "grok":
        normalized_resolution = normalize_grok_resolution(resolution)
        seconds_per_token = 6 if normalized_resolution == "720p" else 12
        tokens = int(grok_tokens_for_duration(duration, normalized_resolution))
        return {
            "tokens": tokens,
            "charge_reason": "grok_video",
            "refund_reason": "grok_video_refund",
            "meta": {
                "origin": "workspace_video",
                "provider": provider,
                "model": model,
                "mode": mode,
                "duration": duration,
                "resolution": normalized_resolution,
                "seconds_per_token": seconds_per_token,
                "rate": tokens / max(1, int(duration or 1)),
            },
        }

    if provider == "pixverse_c1":
        normalized_mode = normalize_pixverse_c1_mode(mode)
        normalized_duration = normalize_pixverse_c1_duration(duration)
        normalized_quality = normalize_pixverse_c1_quality(resolution)
        tokens = int(pixverse_c1_tokens_for_duration(normalized_quality, normalized_duration))
        return {
            "tokens": tokens,
            "charge_reason": "pixverse_c1_video",
            "refund_reason": "pixverse_c1_video_refund",
            "meta": {
                "origin": "workspace_video",
                "provider": provider,
                "model": "c1",
                "mode": normalized_mode,
                "duration": normalized_duration,
                "resolution": normalized_quality,
                "generate_audio": True,
            },
        }

    if provider == "seedance_kie":
        normalized_model = normalize_seedance_kie_model(model)
        normalized_mode = normalize_seedance_kie_mode(mode)
        normalized_duration = normalize_seedance_kie_duration(duration)
        base_tokens = int(seedance_kie_tokens_for_duration(normalized_model, normalized_duration))
        video_ref_surcharge = int(seedance_kie_video_reference_surcharge(normalized_model)) if normalized_mode == "omni_reference" and has_seedance_video_reference else 0
        tokens = base_tokens + video_ref_surcharge
        return {
            "tokens": tokens,
            "charge_reason": "seedance_kie_video",
            "refund_reason": "seedance_kie_video_refund",
            "meta": {
                "origin": "workspace_video",
                "provider": provider,
                "model": normalized_model,
                "mode": normalized_mode,
                "duration": normalized_duration,
                "resolution": ("480p" if normalized_model == "seedance-kie-fast" else "720p"),
                "generate_audio": True,
                "base_tokens": base_tokens,
                "video_reference_surcharge_tokens": video_ref_surcharge,
                "has_video_reference": bool(has_seedance_video_reference),
            },
        }

    if provider == "seedance":
        rate = 1 if model == "seedance-fast" else 2
        tokens = int(duration) * int(rate)
        return {
            "tokens": tokens,
            "charge_reason": "seedance_video",
            "refund_reason": "seedance_video_refund",
            "meta": {"origin": "workspace_video", "provider": provider, "model": model, "mode": mode, "duration": duration, "rate": rate},
        }

    if provider == "sora":
        cost_map = {4: 5, 8: 10, 12: 15}
        tokens = int(cost_map.get(int(duration), 5))
        return {
            "tokens": tokens,
            "charge_reason": "sora_video",
            "refund_reason": "sora_video_refund",
            "meta": {"origin": "workspace_video", "provider": provider, "model": model, "mode": mode, "duration": duration},
        }

    if provider == "switchx":
        normalized_resolution = _normalize_switchx_resolution(resolution)
        rate = _switchx_tokens_per_sec(normalized_resolution)
        tokens = max(1, int(duration) * int(rate))
        return {
            "tokens": tokens,
            "charge_reason": "switchx_video",
            "refund_reason": "switchx_video_refund",
            "meta": {
                "origin": "workspace_video",
                "provider": provider,
                "model": model,
                "mode": mode,
                "duration": duration,
                "resolution": normalized_resolution,
                "tokens_per_sec": rate,
            },
        }

    return {"tokens": 0, "charge_reason": "", "refund_reason": "workspace_video_refund", "meta": {"origin": "workspace_video", "provider": provider, "model": model, "mode": mode}}


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
    provider_mode = str(form.get("provider_mode") or form.get("grok_provider_mode") or "normal").strip().lower() or "normal"
    enable_audio = _parse_form_bool(form.get("enable_audio"))
    quality = str(form.get("quality") or "pro").strip().lower() or "pro"
    kling3_kie_multi_shots = normalize_kling3_kie_shots(_parse_json_list_form(form.get("multi_shots_json") or form.get("multi_prompt") or form.get("multi_shots")))
    kling3_kie_elements = _parse_json_list_form(form.get("kling_elements_json") or form.get("kling_elements"))

    if not provider:
        raise HTTPException(status_code=400, detail="Missing provider")
    if not model:
        raise HTTPException(status_code=400, detail="Missing model")
    if not mode:
        raise HTTPException(status_code=400, detail="Missing mode")
    if not prompt and provider != "seedance":
        raise HTTPException(status_code=400, detail="Missing prompt")

    supported = {"kling", "veo", "grok", "seedance", "seedance_kie", "sora", "switchx", "pixverse_c1"}
    if provider not in supported:
        raise HTTPException(status_code=400, detail=f"Provider {provider} is not supported in /video/run yet")

    start_file = form.get("start_frame")
    end_file = form.get("end_frame")
    last_file = form.get("last_frame")
    avatar_file = form.get("avatar_image")
    motion_file = form.get("motion_video")
    source_video_file = form.get("source_video")
    switchx_select_mask_file = form.get("switchx_select_mask")
    ref_files = [f for f in form.getlist("reference_images") if getattr(f, "filename", None)]
    ref_audio_files = [f for f in form.getlist("reference_audios") if getattr(f, "filename", None)]
    ref_video_files = [f for f in form.getlist("reference_videos") if getattr(f, "filename", None)]
    source_video_upload_id = str(form.get("source_video_upload_id") or "").strip()
    direct_reference_image_url = str(form.get("reference_image_url") or "").strip()
    switchx_alpha_mode = _normalize_switchx_alpha_mode(form.get("switchx_alpha_mode"))

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
    source_video = await _read_optional(source_video_file)
    switchx_select_mask = await _read_optional(switchx_select_mask_file)
    print("[switchx form]", {
        "provider": provider,
        "mode": mode,
        "switchx_alpha_mode": switchx_alpha_mode,
        "source_video_upload_id": source_video_upload_id,
        "has_source_video_file": bool(source_video),
        "has_direct_reference_image_url": bool(direct_reference_image_url),
        "reference_images_count": len(ref_files),
        "mask_bytes": len(switchx_select_mask or b""),
        "mask_filename": getattr(switchx_select_mask_file, "filename", None),
    }, flush=True)
    reference_images: List[bytes] = []
    for rf in ref_files:
        raw = await rf.read()
        if raw:
            reference_images.append(raw)
    reference_audios: List[bytes] = []
    reference_audio_names: List[str] = []
    reference_audio_types: List[str] = []
    for idx, af in enumerate(ref_audio_files, start=1):
        raw = await af.read()
        if not raw:
            continue
        normalized_audio, normalized_ext, _duration_sec = _prepare_seedance_audio_file(af, raw)
        reference_audios.append(normalized_audio)
        base_name = Path(str(getattr(af, "filename", None) or f"seedance_ref_audio_{idx}")).stem or f"seedance_ref_audio_{idx}"
        reference_audio_names.append(f"{base_name}.{normalized_ext}")
        reference_audio_types.append("audio/mpeg" if normalized_ext == "mp3" else "audio/wav")

    reference_videos: List[bytes] = []
    reference_video_names: List[str] = []
    reference_video_types: List[str] = []
    reference_video_total_duration_sec = 0.0
    for idx, vf in enumerate(ref_video_files, start=1):
        raw = await vf.read()
        if not raw:
            continue
        normalized_video, normalized_ext, duration_sec = _prepare_seedance_video_file(vf, raw)
        reference_videos.append(normalized_video)
        base_name = Path(str(getattr(vf, "filename", None) or f"seedance_ref_video_{idx}")).stem or f"seedance_ref_video_{idx}"
        reference_video_names.append(f"{base_name}.{normalized_ext}")
        reference_video_types.append("video/mp4" if normalized_ext == "mp4" else "video/quicktime")
        reference_video_total_duration_sec += float(duration_sec or 0.0)

    source_upload_row: Optional[Dict[str, Any]] = None
    if provider == "switchx" and not source_video_upload_id and source_video:
        source_upload_row = create_workspace_upload_record(
            user_id=uid,
            filename=getattr(source_video_file, "filename", None) or "switchx_source.mp4",
            content_type=getattr(source_video_file, "content_type", None) or "video/mp4",
            raw_bytes=source_video,
        )
        source_video_upload_id = str(source_upload_row.get("id") or "").strip()

    if provider == "kling" and mode in {"image_to_video", "multi_shot"} and model in {"kling-1.6", "kling-2.5", "kling-3.0"} and not start_frame:
        raise HTTPException(status_code=400, detail="Для Image→Video нужен start frame.")
    if provider == "kling" and model == "kling-3.0-new":
        mode = normalize_kling3_kie_generation_mode(mode)
        resolution = normalize_kling3_kie_mode(resolution or "pro")
        duration = normalize_kling3_kie_duration(duration)
        aspect_ratio = normalize_kling3_kie_aspect_ratio(aspect_ratio)
        if mode == "image_to_video" and not start_frame:
            raise HTTPException(status_code=400, detail="Для Kling 3.0 - New Image→Video нужен start frame.")
        if mode == "multi_shot":
            if len(kling3_kie_multi_shots) < 2:
                raise HTTPException(status_code=400, detail="Для Kling 3.0 - New Multi-shot нужно минимум 2 шота.")
            total_ms = sum(int(item.get("duration") or 0) for item in kling3_kie_multi_shots)
            if total_ms < 3 or total_ms > 15:
                raise HTTPException(status_code=400, detail="Суммарная длительность Multi-shot должна быть 3–15 сек.")
            duration = total_ms
            end_frame = None
            last_frame = None
    if provider == "veo" and mode == "image_to_video" and not start_frame:
        raise HTTPException(status_code=400, detail="Для Veo Image→Video нужен start frame.")
    if provider == "grok":
        if mode not in {"text_to_video", "image_to_video"}:
            raise HTTPException(status_code=400, detail="Grok поддерживает только Text→Video и Image→Video.")
        duration = normalize_grok_duration(duration)
        resolution = normalize_grok_resolution(resolution)
        aspect_ratio = normalize_grok_aspect_ratio(aspect_ratio)
        provider_mode = normalize_grok_provider_mode(provider_mode)
        if mode == "image_to_video" and not start_frame:
            raise HTTPException(status_code=400, detail="Для Grok Image→Video нужен start frame.")
    if provider == "seedance_kie":
        model = normalize_seedance_kie_model(model)
        mode = normalize_seedance_kie_mode(mode)
        duration = normalize_seedance_kie_duration(duration)
        resolution = _normalize_workspace_video_resolution(provider, model, resolution)
        aspect_ratio = normalize_seedance_kie_aspect_ratio(aspect_ratio)
        enable_audio = True
        if mode == "image_to_video":
            total_image_refs = len(reference_images) + (1 if start_frame else 0) + (1 if last_frame else 0)
            if total_image_refs < 1:
                raise HTTPException(status_code=400, detail="Для Seedance 2.0 Image→Video нужен хотя бы один image reference.")
            if total_image_refs > 7:
                raise HTTPException(status_code=400, detail="Для Seedance 2.0 доступно максимум 7 image references суммарно.")
            if len(reference_audios) > 3:
                raise HTTPException(status_code=400, detail="Для Seedance 2.0 доступно максимум 3 audio references.")
            reference_videos = []
        elif mode == "omni_reference":
            total_refs = len(reference_images) + len(reference_videos) + len(reference_audios)
            if total_refs < 1:
                raise HTTPException(status_code=400, detail="Для Seedance 2.0 Omni Reference нужен хотя бы один reference.")
            if total_refs > 12:
                raise HTTPException(status_code=400, detail="Для Seedance 2.0 Omni Reference доступно максимум 12 refs суммарно.")
            if len(reference_audios) > 3:
                raise HTTPException(status_code=400, detail="Для Seedance 2.0 доступно максимум 3 audio references.")
            if reference_audios and not (reference_images or reference_videos):
                raise HTTPException(status_code=400, detail="Для Seedance 2.0 Omni Reference audio-only не поддерживается: нужен хотя бы один image или video reference.")
            if reference_video_total_duration_sec > SEEDANCE_VIDEO_TOTAL_MAX_DURATION_SEC:
                raise HTTPException(status_code=400, detail="Для Seedance 2.0 суммарная длина всех video references должна быть не больше 15.4 секунды.")
            start_frame = None
            end_frame = None
            last_frame = None
        else:
            reference_images = []
            reference_audios = []
            reference_videos = []
            start_frame = None
            last_frame = None
    if provider == "pixverse_c1":
        model = "c1"
        mode = normalize_pixverse_c1_mode(mode)
        duration = normalize_pixverse_c1_duration(duration)
        resolution = _normalize_workspace_video_resolution(provider, model, resolution)
        enable_audio = True
        if mode in {"text_to_video", "fusion"}:
            aspect_ratio = normalize_pixverse_c1_aspect_ratio(aspect_ratio)
        else:
            aspect_ratio = normalize_pixverse_c1_aspect_ratio(aspect_ratio)
        if mode == "image_to_video":
            if not start_frame:
                raise HTTPException(status_code=400, detail="Для PixVerse C1 Image→Video нужен start frame.")
            reference_images = []
            reference_audios = []
            last_frame = None
        elif mode == "transition":
            if not start_frame or not last_frame:
                raise HTTPException(status_code=400, detail="Для PixVerse C1 Transition нужны первый и последний кадр.")
            reference_images = []
            reference_audios = []
        elif mode == "fusion":
            if not reference_images:
                raise HTTPException(status_code=400, detail="Для PixVerse C1 Fusion нужен хотя бы один reference image.")
            if len(reference_images) > 7:
                raise HTTPException(status_code=400, detail="Для PixVerse C1 Fusion доступно максимум 7 reference images.")
            reference_audios = []
            start_frame = None
            end_frame = None
            last_frame = None
        else:
            reference_images = []
            reference_audios = []
            start_frame = None
            end_frame = None
            last_frame = None
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
    if provider == "switchx":
        resolution = _normalize_switchx_resolution(resolution)
        switchx_alpha_mode = _normalize_switchx_alpha_mode(switchx_alpha_mode)
        if mode != "video_swap":
            raise HTTPException(status_code=400, detail="SwitchX в workspace поддерживает только режим video_swap.")
        if not source_video_upload_id:
            raise HTTPException(status_code=400, detail="Для SwitchX нужно исходное видео.")
        if not direct_reference_image_url and not reference_images:
            raise HTTPException(status_code=400, detail="Для SwitchX нужен reference image или AI-референс.")
        if switchx_alpha_mode == "select" and not switchx_select_mask:
            raise HTTPException(status_code=400, detail="Для SwitchX Select нужна PNG/JPG маска 1-го кадра.")
        if source_upload_row is None:
            source_upload_row = get_workspace_upload_row(uid, source_video_upload_id)
        if not source_upload_row:
            raise HTTPException(status_code=400, detail="Исходное видео для SwitchX не найдено.")
        if str(source_upload_row.get("file_type") or "") != "video":
            raise HTTPException(status_code=400, detail="SwitchX source upload должен быть видео.")
        duration = _normalize_switchx_source_duration_seconds(source_upload_row.get("duration_sec"))
        if duration <= 0:
            raise HTTPException(status_code=400, detail="Не удалось определить длительность исходного видео для SwitchX.")

    ensure_user_row(uid)
    try:
        balance = int(get_balance(uid) or 0)
    except Exception:
        balance = 0

    charge = _workspace_video_charge_spec(
        provider=provider,
        model=model,
        mode=mode,
        duration=duration,
        resolution=resolution,
        enable_audio=enable_audio,
        quality=quality,
        has_seedance_video_reference=bool(reference_videos),
        kling3_kie_multi_shots=kling3_kie_multi_shots,
    )
    cost_tokens = int(charge.get("tokens") or 0)
    charge_reason = str(charge.get("charge_reason") or "")
    refund_reason = str(charge.get("refund_reason") or "workspace_video_refund")
    charge_meta = dict(charge.get("meta") or {})
    charge_ref_id = uuid4().hex if cost_tokens > 0 and charge_reason else ""

    if cost_tokens > 0 and balance < cost_tokens:
        raise HTTPException(status_code=402, detail=f"Недостаточно токенов. Нужно: {cost_tokens} ток.")

    charged = False
    generation_id: Optional[str] = None
    try:
        if cost_tokens > 0 and charge_reason:
            try:
                add_tokens(uid, -int(cost_tokens), reason=charge_reason, ref_id=charge_ref_id, meta=charge_meta)
            except TypeError:
                add_tokens(uid, -int(cost_tokens), reason=charge_reason)
            charged = True

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

        start_frame_url = _upload_workspace_input_image(uid, start_frame, filename=getattr(start_file, "filename", None), slot="video_start_frame") if start_frame else None
        end_frame_url = _upload_workspace_input_image(uid, end_frame, filename=getattr(end_file, "filename", None), slot="video_end_frame") if end_frame else None
        last_frame_url = _upload_workspace_input_image(uid, last_frame, filename=getattr(last_file, "filename", None), slot="video_last_frame") if last_frame else None
        avatar_image_url = _upload_workspace_input_image(uid, avatar_image, filename=getattr(avatar_file, "filename", None), slot="video_avatar_image") if avatar_image else None
        reference_image_urls = [
            _upload_workspace_input_image(uid, raw, filename=getattr(ref_files[idx], "filename", None) if idx < len(ref_files) else None, slot=f"video_reference_{idx + 1}")
            for idx, raw in enumerate(reference_images)
            if raw
        ]
        reference_audio_upload_ids: List[str] = []
        for idx, raw in enumerate(reference_audios):
            upload_row = create_workspace_upload_record(
                user_id=uid,
                filename=reference_audio_names[idx] if idx < len(reference_audio_names) else f"seedance_ref_audio_{idx + 1}.wav",
                content_type=reference_audio_types[idx] if idx < len(reference_audio_types) else "audio/wav",
                raw_bytes=raw,
            )
            upload_id = str(upload_row.get("id") or "").strip()
            if upload_id:
                reference_audio_upload_ids.append(upload_id)

        reference_video_upload_ids: List[str] = []
        for idx, raw in enumerate(reference_videos):
            upload_row = create_workspace_upload_record(
                user_id=uid,
                filename=reference_video_names[idx] if idx < len(reference_video_names) else f"seedance_ref_video_{idx + 1}.mp4",
                content_type=reference_video_types[idx] if idx < len(reference_video_types) else "video/mp4",
                raw_bytes=raw,
            )
            upload_id = str(upload_row.get("id") or "").strip()
            if upload_id:
                reference_video_upload_ids.append(upload_id)

        motion_video_upload_id = None
        if motion_video:
            uploaded_motion = create_workspace_upload_record(
                user_id=uid,
                filename=getattr(motion_file, "filename", None) or "motion_video.mp4",
                content_type=getattr(motion_file, "content_type", None) or "video/mp4",
                raw_bytes=motion_video,
            )
            motion_video_upload_id = str(uploaded_motion.get("id") or "").strip() or None

        switchx_reference_image_url = ((reference_image_urls[0] if reference_image_urls else None) or direct_reference_image_url) if provider == "switchx" else None
        switchx_select_mask_url = None
        if provider == "switchx" and switchx_alpha_mode == "select" and switchx_select_mask:
            switchx_select_mask_url = _upload_workspace_input_image(uid, switchx_select_mask, filename=getattr(switchx_select_mask_file, "filename", None), slot="switchx_select_mask")

        if provider == "switchx":
            print("[switchx queued job]", {
                "generation_id": generation_id,
                "alpha_mode": switchx_alpha_mode,
                "select_mask_url": switchx_select_mask_url,
                "reference_image_url": switchx_reference_image_url,
                "source_video_upload_id": source_video_upload_id,
                "resolution": resolution,
                "prompt_len": len(str(prompt or "")),
            }, flush=True)

        if provider == "kling" and model == "kling-3.0-new":
            enriched_elements: List[Dict[str, Any]] = []
            for idx, element in enumerate(kling3_kie_elements[:3]):
                if not isinstance(element, dict):
                    continue
                item = dict(element)
                image_urls = [str(u or "").strip() for u in (item.get("element_input_urls") or item.get("image_urls") or []) if str(u or "").strip()]
                video_urls = [str(u or "").strip() for u in (item.get("element_input_video_urls") or item.get("video_urls") or []) if str(u or "").strip()]
                for upload in form.getlist(f"kling_element_images_{idx}"):
                    if not getattr(upload, "filename", None):
                        continue
                    raw = await upload.read()
                    if raw:
                        image_urls.append(upload_kling3_kie_input_bytes(raw, filename=getattr(upload, "filename", None), content_type=getattr(upload, "content_type", None), prefix="kling3-kie/elements"))
                for upload in form.getlist(f"kling_element_videos_{idx}"):
                    if not getattr(upload, "filename", None):
                        continue
                    raw = await upload.read()
                    if raw:
                        video_urls.append(upload_kling3_kie_input_bytes(raw, filename=getattr(upload, "filename", None), content_type=getattr(upload, "content_type", None), prefix="kling3-kie/elements"))
                item["element_input_urls"] = image_urls[:4]
                item["element_input_video_urls"] = video_urls[:1]
                enriched_elements.append(item)
            kling3_kie_elements = normalize_kling3_kie_elements(enriched_elements)

        job = {
            "job_id": uuid4().hex,
            "kind": "workspace_video_run",
            "generation_id": generation_id,
            "user_id": uid,
            "provider": provider,
            "model": model,
            "mode": mode,
            "prompt": prompt,
            "duration": duration,
            "resolution": resolution,
            "aspect_ratio": aspect_ratio,
            "enable_audio": bool(enable_audio),
            "quality": quality,
            "provider_mode": provider_mode,
            "start_frame_url": start_frame_url,
            "end_frame_url": end_frame_url,
            "last_frame_url": last_frame_url,
            "avatar_image_url": avatar_image_url,
            "motion_video_upload_id": motion_video_upload_id,
            "source_video_upload_id": source_video_upload_id,
            "reference_image_url": switchx_reference_image_url,
            "reference_image_urls": reference_image_urls,
            "switchx_alpha_mode": switchx_alpha_mode if provider == "switchx" else None,
            "switchx_select_mask_url": switchx_select_mask_url,
            "reference_audio_upload_ids": reference_audio_upload_ids,
            "reference_video_upload_ids": reference_video_upload_ids,
            "charge_tokens": int(cost_tokens) if charged else 0,
            "charge_ref_id": charge_ref_id,
            "refund_reason": refund_reason,
            "origin": "workspace",
        }
        if provider == "kling" and model == "kling-3.0-new":
            job["kind"] = "workspace_kling3_kie_run"
            job["kie_mode"] = resolution
            job["multi_shots"] = kling3_kie_multi_shots
            job["kling_elements"] = kling3_kie_elements
            job["mode"] = mode
        target_queue = KLING3_KIE_QUEUE_NAME if (provider == "kling" and model == "kling-3.0-new") else WORKSPACE_MEDIA_QUEUE_NAME
        await enqueue_job(job, queue_name=target_queue)

        try:
            balance_tokens = int(get_balance(uid) or 0)
        except Exception:
            balance_tokens = None
        return {
            "ok": True,
            "generation_id": generation_id,
            "task_id": generation_id,
            "status": "queued",
            "status_text": "Генерация поставлена в очередь. Видео появится в рабочей зоне автоматически.",
            "balance_tokens": balance_tokens,
            "cost_tokens": int(cost_tokens) if charged else 0,
            "source_video_upload_id": source_video_upload_id or None,
            "reference_image_url": switchx_reference_image_url,
        }
    except HTTPException:
        raise
    except Exception as e:
        if charged and cost_tokens > 0:
            try:
                add_tokens(uid, int(cost_tokens), reason=refund_reason, ref_id=charge_ref_id or uuid4().hex, meta={"origin": "workspace_video", "stage": "route", "error": str(e)[:300]})
            except Exception:
                pass
        if generation_id:
            _mark_workspace_generation_failed(generation_id, str(e), error_code="queue_error")
        raise HTTPException(status_code=500, detail=f"Video run failed: {e}")


@router.post("/video/switchx/reference/run")
async def workspace_switchx_reference_run(
    request: Request,
    user: Dict[str, Any] = Depends(get_current_workspace_user),
) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    form = await request.form()
    ref_prompt = str(form.get("ref_prompt") or form.get("prompt") or "").strip()
    safety_level = str(form.get("safety_level") or "high").strip().lower() or "high"
    source_video_upload_id = str(form.get("source_video_upload_id") or "").strip()
    source_video_file = form.get("source_video")
    if not ref_prompt:
        raise HTTPException(status_code=400, detail="Missing ref_prompt")
    source_video = None
    if source_video_file and getattr(source_video_file, "filename", None):
        source_video = await source_video_file.read()
    source_upload_row = None
    if not source_video_upload_id and source_video:
        source_upload_row = create_workspace_upload_record(
            user_id=uid,
            filename=getattr(source_video_file, "filename", None) or "switchx_source.mp4",
            content_type=getattr(source_video_file, "content_type", None) or "video/mp4",
            raw_bytes=source_video,
        )
        source_video_upload_id = str(source_upload_row.get("id") or "").strip()
    if not source_video_upload_id:
        raise HTTPException(status_code=400, detail="Для создания AI-референса нужно исходное видео.")
    if source_upload_row is None:
        source_upload_row = get_workspace_upload_row(uid, source_video_upload_id)
    if not source_upload_row:
        raise HTTPException(status_code=404, detail="Исходное видео не найдено.")

    ensure_user_row(uid)
    cost = int(_workspace_image_cost("nano_banana_pro", "image_to_image", "standard", "2K"))
    try:
        balance = int(get_balance(uid) or 0)
    except Exception:
        balance = 0
    if cost > 0 and balance < cost:
        raise HTTPException(status_code=402, detail=f"Недостаточно токенов. Нужно: {cost} ток.")

    ref_id = uuid4().hex if cost > 0 else ""
    if cost > 0:
        try:
            add_tokens(uid, -cost, reason="nano_banana_pro", ref_id=ref_id, meta={"origin": "workspace_switchx_ref", "source_video_upload_id": source_video_upload_id})
        except TypeError:
            add_tokens(uid, -cost, reason="nano_banana_pro")

    generation_id = _insert_workspace_image_generation(
        {
            "user_id": str(uid),
            "provider": "nano_banana_pro",
            "model": "nano-banana-pro",
            "mode": "image_to_image",
            "prompt": ref_prompt,
            "status": "queued",
            "resolution": "2K",
            "aspect_ratio": "match_input_image",
            "safety_level": safety_level,
            "origin": "workspace_switchx_ref",
        }
    )
    try:
        await enqueue_job(
            {
                "job_id": uuid4().hex,
                "kind": "workspace_switchx_ref_run",
                "generation_id": generation_id,
                "user_id": uid,
                "source_video_upload_id": source_video_upload_id,
                "prompt": ref_prompt,
                "resolution": "2K",
                "aspect_ratio": "match_input_image",
                "safety_level": safety_level,
                "charge_tokens": cost,
                "charge_ref_id": ref_id,
                "refund_reason": "nano_banana_pro_refund",
                "origin": "workspace_switchx_ref",
            },
            queue_name=WORKSPACE_MEDIA_QUEUE_NAME,
        )
    except Exception as e:
        _mark_workspace_image_generation_failed(generation_id, str(e), error_code="queue_error")
        if cost > 0:
            try:
                add_tokens(uid, cost, reason="nano_banana_pro_refund", ref_id=ref_id or uuid4().hex, meta={"origin": "workspace_switchx_ref", "stage": "enqueue", "error": str(e)[:300]})
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=f"SwitchX AI reference queue failed: {e}")

    try:
        balance_tokens = int(get_balance(uid) or 0)
    except Exception:
        balance_tokens = None
    return {
        "ok": True,
        "generation_id": generation_id,
        "status": "queued",
        "status_text": "AI-референс поставлен в очередь.",
        "source_video_upload_id": source_video_upload_id,
        "cost_tokens": cost,
        "balance_tokens": balance_tokens,
    }


@router.get("/video/switchx/reference/{generation_id}")
async def workspace_switchx_reference_status(
    generation_id: str,
    user: Dict[str, Any] = Depends(get_current_workspace_user),
) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    generation_id_text = str(generation_id or "").strip()
    if not generation_id_text:
        raise HTTPException(status_code=400, detail="Missing generation_id")
    if supabase is None:
        raise HTTPException(status_code=404, detail="Reference generation not found")
    try:
        resp = (
            supabase.table(_WORKSPACE_IMAGE_GENERATIONS_TABLE)
            .select("*")
            .eq("id", generation_id_text)
            .eq("user_id", str(uid))
            .limit(1)
            .execute()
        )
        rows = getattr(resp, "data", None) or []
        if not rows or not isinstance(rows[0], dict):
            raise HTTPException(status_code=404, detail="Reference generation not found")
        return {"ok": True, "item": _serialize_workspace_image_generation(rows[0])}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Reference status load failed: {e}")


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
    if ai == "udio":
        return int(WORKSPACE_UDIO_COST_TOKENS)
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


def _workspace_nano_banana_pro_new_resolution(value: Any) -> str:
    return normalize_nano_banana_pro_new_resolution(value, default="2K")


def _workspace_nano_banana_pro_new_aspect_ratio(value: Any, default: str = "16:9") -> str:
    return normalize_nano_banana_pro_new_aspect_ratio(value, default=default)


async def _workspace_run_nano_banana_pro_new_site(
    *,
    user_id: int,
    prompt: str,
    source_image_bytes: Optional[bytes],
    source_filename: Optional[str],
    source_image_urls: Optional[list[str]] = None,
    resolution: str,
    aspect_ratio: Optional[str],
) -> tuple[bytes, str]:
    clean_prompt = str(prompt or "").strip()
    if not clean_prompt:
        raise RuntimeError("Empty prompt")

    normalized_urls = [str(item or "").strip() for item in (source_image_urls or []) if str(item or "").strip()][:8]
    if normalized_urls:
        normalized_aspect = _workspace_nano_banana_pro_new_aspect_ratio(aspect_ratio, default="match_input_image")
        if normalized_aspect == "match_input_image":
            normalized_aspect = None
    elif source_image_bytes:
        normalized_urls = [_upload_workspace_input_image(
            int(user_id),
            source_image_bytes,
            filename=source_filename,
            slot="nano_banana_pro_new_source",
        )]
        normalized_aspect = _workspace_nano_banana_pro_new_aspect_ratio(aspect_ratio, default="match_input_image")
        if normalized_aspect == "match_input_image":
            normalized_aspect = None
    else:
        normalized_aspect = _workspace_nano_banana_pro_new_aspect_ratio(aspect_ratio, default="16:9")
        if normalized_aspect == "match_input_image":
            normalized_aspect = "16:9"

    return await handle_nano_banana_pro_new(
        clean_prompt,
        source_image_urls=normalized_urls,
        resolution=_workspace_nano_banana_pro_new_resolution(resolution),
        output_format="png",
        aspect_ratio=normalized_aspect,
    )


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
    if not isinstance(task_json, dict):
        return []

    audio_keys = {
        "audio_url", "audioUrl", "stream_audio_url", "streamAudioUrl",
        "source_audio_url", "sourceAudioUrl", "source_stream_audio_url", "sourceStreamAudioUrl",
        "song_url", "songUrl", "song_path", "songPath",
        "mp3_url", "mp3", "file_url", "fileUrl", "url",
    }
    hint_keys = audio_keys | {
        "image_url", "imageUrl", "cover", "cover_url", "coverUrl",
        "title", "prompt", "tags", "lyrics", "duration",
        "video_url", "videoUrl", "mv", "model_name", "modelName",
    }

    def _track_score(obj: Any) -> int:
        if not isinstance(obj, dict):
            return 0
        score = 0
        for key in hint_keys:
            if key in obj and obj.get(key) not in (None, "", [], {}):
                score += 2 if key in audio_keys else 1
        return score

    def _coerce_list(val: Any) -> List[Dict[str, Any]]:
        if isinstance(val, list):
            items = [x for x in val if isinstance(x, dict)]
            return [x for x in items if _track_score(x) > 0]
        if isinstance(val, dict):
            inner = val.get("data")
            if isinstance(inner, list):
                items = [x for x in inner if isinstance(x, dict)]
                return [x for x in items if _track_score(x) > 0]
            if _track_score(val) > 0:
                return [val]
        return []

    candidates: List[Any] = []
    data = task_json.get("data")
    if isinstance(data, dict):
        response = data.get("response") if isinstance(data.get("response"), dict) else {}
        response_data = response.get("data") if isinstance(response, dict) else None
        candidates.extend([
            data.get("data"),
            data.get("response"),
            response_data,
            response_data.get("data") if isinstance(response_data, dict) else None,
            data.get("output"),
            data.get("result"),
        ])
    candidates.extend([
        task_json.get("output"),
        task_json.get("result"),
    ])

    best: List[Dict[str, Any]] = []
    best_score = -1
    for cand in candidates:
        items = _coerce_list(cand)
        if not items:
            continue
        score = sum(_track_score(x) for x in items)
        if score > best_score:
            best = items
            best_score = score
    if best:
        return best

    def _scan(obj: Any) -> List[Dict[str, Any]]:
        if isinstance(obj, dict):
            if _track_score(obj) > 0:
                return [obj]
            for value in obj.values():
                found = _scan(value)
                if found:
                    return found
        elif isinstance(obj, list):
            dict_items = [x for x in obj if isinstance(x, dict)]
            scored_items = [x for x in dict_items if _track_score(x) > 0]
            if scored_items:
                return scored_items
            for item in obj:
                found = _scan(item)
                if found:
                    return found
        return []

    return _scan(task_json)
def _workspace_pick_first_url(val: Any) -> str:
    if not val:
        return ""
    if isinstance(val, str):
        s = val.strip()
        return s if s.startswith(("http://", "https://")) else ""
    if isinstance(val, dict):
        for k in (
            "url", "audio_url", "audioUrl", "stream_audio_url", "streamAudioUrl",
            "source_audio_url", "sourceAudioUrl", "source_stream_audio_url", "sourceStreamAudioUrl",
            "song_url", "songUrl", "song_path", "songPath",
            "mp3", "mp3_url", "file_url", "fileUrl", "download_url", "downloadUrl",
            "video_url", "videoUrl", "image_url", "imageUrl",
        ):
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
    for k in (
        "audio_url", "audioUrl", "stream_audio_url", "streamAudioUrl",
        "source_audio_url", "sourceAudioUrl", "source_stream_audio_url", "sourceStreamAudioUrl",
        "song_url", "songUrl", "song_path", "songPath",
        "mp3_url", "mp3", "file_url", "fileUrl", "download_url", "downloadUrl", "url",
    ):
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
    tracks_raw = _workspace_sunoapi_extract_tracks(done)
    tracks = []
    for idx, item in enumerate(tracks_raw[:2], start=1):
        if not isinstance(item, dict):
            continue
        tracks.append({
            "provider_track_id": item.get("id") or item.get("task_id") or item.get("song_id") or item.get("audioId") or item.get("songId"),
            "title": item.get("title") or payload.title or f"Track {idx}",
            "audio_url": _workspace_extract_audio_url(item),
            "video_url": _workspace_pick_first_url(item.get("video_url") or item.get("video") or item.get("mp4") or item.get("videoUrl")),
            "cover_url": _workspace_pick_first_url(item.get("image_url") or item.get("image") or item.get("cover_url") or item.get("cover")),
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
        "status": "queued",
        "output_format": payload.output_format,
        "origin": "workspace_voice",
        "created_at": now_iso,
        "updated_at": now_iso,
    })

    try:
        await enqueue_job(
            {
                "job_id": uuid4().hex,
                "kind": "workspace_tts_run",
                "generation_id": generation_id,
                "user_id": uid,
                "payload": payload.model_dump(),
                "origin": "workspace_voice",
            },
            queue_name=WORKSPACE_MEDIA_QUEUE_NAME,
        )
    except Exception as e:
        _mark_workspace_voice_generation_failed(generation_id, str(e), error_code="queue_error")
        raise HTTPException(status_code=500, detail=f"Voice generation failed: {e}")

    return {
        "ok": True,
        "generation_id": generation_id,
        "provider": "elevenlabs",
        "model": payload.model_id,
        "voice_id": payload.voice_id,
        "voice_name": voice_name,
        "output_format": payload.output_format,
        "status": "queued",
        "status_text": "Озвучка поставлена в очередь. Файл появится в рабочей зоне автоматически.",
        "created_at": now_iso,
    }


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
        storage_paths = _workspace_json_field(row.get("storage_paths_json"), [])
        paths_to_delete = [str(item or "").strip() for item in storage_paths if str(item or "").strip()]
        if storage_path and storage_path not in paths_to_delete:
            paths_to_delete.append(storage_path)
        if paths_to_delete and bucket_name:
            try:
                supabase.storage.from_(bucket_name).remove(paths_to_delete)
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
    negative_prompt = str(form.get("negative_prompt") or "").strip()
    mj_stylize_raw = form.get("mj_stylize")
    mj_chaos_raw = form.get("mj_chaos")
    mj_raw = _workspace_boolish(form.get("mj_raw"))
    mj_speed_mode = _workspace_speed_mode(form.get("mj_speed_mode") or form.get("speed_mode") or "fast")
    mj_seed = str(form.get("mj_seed") or "").strip()

    if not provider:
        raise HTTPException(status_code=400, detail="Missing provider")
    if not model:
        raise HTTPException(status_code=400, detail="Missing model")
    if not mode:
        raise HTTPException(status_code=400, detail="Missing mode")
    if provider != "topaz_photo" and not prompt:
        raise HTTPException(status_code=400, detail="Missing prompt")

    supported = {"nano_banana", "nano_banana_2", "nano_banana_pro", "nano_banana_pro_new", "seedream", "posters", "photosession", "two_images", "text_to_image", "gpt_image_2", "topaz_photo", "midjourney"}
    if provider not in supported:
        raise HTTPException(status_code=400, detail=f"Provider {provider} is not supported in /image/run")

    source_uploads_raw = [item for item in form.getlist("source_image") if item]
    base_upload = form.get("base_image")
    source_image_uploads: list[tuple[Any, bytes, Optional[str]]] = []
    source_upload = source_uploads_raw[0] if source_uploads_raw else None
    source_image: Optional[bytes] = None

    if provider == "nano_banana_pro_new":
        for upload in source_uploads_raw[:8]:
            raw = await _read_optional_upload_bytes(upload)
            if raw:
                source_image_uploads.append((upload, raw, getattr(upload, "filename", None)))
        if source_image_uploads:
            source_upload = source_image_uploads[0][0]
            source_image = source_image_uploads[0][1]
    else:
        source_upload = source_uploads_raw[0] if source_uploads_raw else form.get("source_image")
        source_image = await _read_optional_upload_bytes(source_upload)

    base_image = await _read_optional_upload_bytes(base_upload)

    style_ref_uploads_raw = [item for item in form.getlist("style_ref_image") if item]
    style_ref_urls: list[str] = []
    omni_ref_upload = form.get("omni_ref_image")
    omni_ref_image = await _read_optional_upload_bytes(omni_ref_upload)
    omni_ref_url = None

    if provider == "nano_banana" and not source_image:
        raise HTTPException(status_code=400, detail="Для Nano Banana нужен source image.")
    if provider == "nano_banana_2" and mode == "image_to_image" and not source_image:
        raise HTTPException(status_code=400, detail="Для Nano Banana 2 Image→Image нужен source image.")
    if provider == "nano_banana_pro" and mode == "image_to_image" and not source_image:
        raise HTTPException(status_code=400, detail="Для Image→Image нужен source image.")
    if provider == "nano_banana_pro_new" and mode == "image_to_image" and not source_image_uploads:
        raise HTTPException(status_code=400, detail="Для Nano Banana Pro - NEW Image→Image нужен хотя бы 1 reference image.")
    if provider == "seedream" and mode in {"single", "seedream_45", "seedream_single"} and not source_image:
        raise HTTPException(status_code=400, detail="Для Seedream 4.5 нужен source image.")
    if provider == "seedream" and mode in {"image_to_image", "i2i"} and (not source_image or not base_image):
        raise HTTPException(status_code=400, detail="Для режима Картинка + Картинка нужны base image и source image.")
    if provider == "posters" and mode == "photo_edit" and not source_image:
        raise HTTPException(status_code=400, detail="Для Photo Edit нужен source image.")
    if provider == "photosession" and not source_image:
        raise HTTPException(status_code=400, detail="Для нейро фотосессии нужен source image.")
    if provider == "gpt_image_2" and mode == "image_to_image" and not source_image:
        raise HTTPException(status_code=400, detail="Для GPT Image 2.0 Image→Image нужен source image.")
    if provider == "two_images" and (not source_image or not base_image):
        raise HTTPException(status_code=400, detail="Для режима Картинка + Картинка нужны base image и source image.")
    if provider == "topaz_photo" and not source_image:
        raise HTTPException(status_code=400, detail="Для Topaz Photo Upscale нужен source image.")
    if provider == "midjourney":
        mode = "text_to_image"
        model = model or "midjourney-v7"

    if provider in {"nano_banana_2", "nano_banana_pro", "nano_banana_pro_new", "text_to_image", "seedream"} and mode in {"text_to_image", "t2i"} and aspect_ratio == "match_input_image":
        aspect_ratio = "9:16" if provider == "seedream" else "16:9"

    try:
        mj_stylize = max(0, min(1000, int(mj_stylize_raw if mj_stylize_raw not in {None, ""} else 100)))
    except Exception:
        mj_stylize = 100
    try:
        mj_chaos = max(0, min(100, int(mj_chaos_raw if mj_chaos_raw not in {None, ""} else 0)))
    except Exception:
        mj_chaos = 0

    run_prompt = _build_workspace_image_prompt(
        provider=provider,
        mode=mode,
        prompt=prompt,
        poster_style=poster_style,
        style_preset=style_preset,
        mood_preset=mood_preset,
    )
    if provider == "midjourney":
        for index, upload in enumerate(style_ref_uploads_raw[:4], start=1):
            raw = await _read_optional_upload_bytes(upload)
            if not raw:
                continue
            style_ref_urls.append(_upload_workspace_input_image(uid, raw, filename=getattr(upload, "filename", None), slot=f"workspace_midjourney_style_{index}"))
        omni_ref_url = _upload_workspace_input_image(uid, omni_ref_image, filename=getattr(omni_ref_upload, "filename", None), slot="workspace_midjourney_omni") if omni_ref_image else None
        run_prompt = build_midjourney_v7_prompt(
            prompt=prompt,
            aspect_ratio=aspect_ratio or "1:1",
            stylize=mj_stylize,
            chaos=mj_chaos,
            raw_mode=mj_raw,
            negative_prompt=negative_prompt,
            seed=mj_seed,
            speed_mode=mj_speed_mode,
            style_ref_urls=style_ref_urls,
            omni_ref_url=omni_ref_url,
        )

    if provider != "topaz_photo" and not run_prompt:
        raise HTTPException(status_code=400, detail="Empty prompt")

    ensure_user_row(uid)
    try:
        bal = float(get_balance(uid) or 0)
    except Exception:
        bal = 0
    cost = int(_workspace_image_cost(provider, mode, preset_slug, resolution, speed_mode=mj_speed_mode, action_type="generate"))
    if cost > 0 and bal < cost:
        raise HTTPException(status_code=402, detail=f"Недостаточно токенов. Нужно: {cost} ток.")

    charged = False
    reason = _workspace_image_charge_reason(provider, mode, action_type="generate")
    ref_id = uuid4().hex if cost > 0 and reason else ""
    generation_id = _insert_workspace_image_generation(
        {
            "user_id": str(uid),
            "provider": provider,
            "model": model,
            "mode": mode,
            "prompt": prompt,
            "status": "queued",
            "resolution": resolution,
            "aspect_ratio": aspect_ratio,
            "safety_level": safety_level,
            "poster_style": poster_style,
            "style_preset": style_preset,
            "mood_preset": mood_preset,
            "preset_slug": preset_slug if provider == "topaz_photo" else None,
            "negative_prompt": negative_prompt if provider == "midjourney" else None,
            "mj_stylize": mj_stylize if provider == "midjourney" else None,
            "mj_chaos": mj_chaos if provider == "midjourney" else None,
            "mj_raw": mj_raw if provider == "midjourney" else None,
            "mj_speed_mode": mj_speed_mode if provider == "midjourney" else None,
            "mj_seed": mj_seed if provider == "midjourney" else None,
            "style_ref_urls_json": style_ref_urls if provider == "midjourney" else None,
            "omni_ref_url": omni_ref_url if provider == "midjourney" else None,
            "action_type": "generate" if provider == "midjourney" else None,
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

        source_image_urls = []
        if provider == "nano_banana_pro_new":
            for index, (_upload_obj, raw_bytes, upload_name) in enumerate(source_image_uploads[:8], start=1):
                source_image_urls.append(_upload_workspace_input_image(uid, raw_bytes, filename=upload_name, slot=f"workspace_image_source_{index}"))
        source_image_url = source_image_urls[0] if source_image_urls else (_upload_workspace_input_image(uid, source_image, filename=getattr(source_upload, "filename", None), slot="workspace_image_source") if source_image else None)
        base_image_url = _upload_workspace_input_image(uid, base_image, filename=getattr(base_upload, "filename", None), slot="workspace_image_base") if base_image else None

        await enqueue_job(
            {
                "job_id": uuid4().hex,
                "kind": "workspace_image_run",
                "generation_id": generation_id,
                "user_id": uid,
                "provider": provider,
                "model": model,
                "mode": mode,
                "prompt": prompt,
                "run_prompt": run_prompt,
                "resolution": resolution,
                "aspect_ratio": aspect_ratio,
                "safety_level": safety_level,
                "poster_style": poster_style,
                "style_preset": style_preset,
                "mood_preset": mood_preset,
                "preset_slug": preset_slug,
                "source_image_url": source_image_url,
                "source_image_urls": source_image_urls,
                "base_image_url": base_image_url,
                "source_filename": getattr(source_upload, "filename", None),
                "base_filename": getattr(base_upload, "filename", None),
                "negative_prompt": negative_prompt,
                "mj_stylize": mj_stylize,
                "mj_chaos": mj_chaos,
                "mj_raw": mj_raw,
                "mj_speed_mode": mj_speed_mode,
                "mj_seed": mj_seed,
                "style_ref_urls": style_ref_urls,
                "omni_ref_url": omni_ref_url if provider == "midjourney" else None,
                "mj_action": "generate" if provider == "midjourney" else None,
                "charge_tokens": int(cost if charged else 0),
                "charge_ref_id": ref_id,
                "refund_reason": f"{reason}_refund" if reason else "workspace_image_refund",
                "origin": "workspace_image",
            },
            queue_name=WORKSPACE_IMAGE_QUEUE_NAME,
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
            "tokens_required": cost,
            "status": "queued",
            "status_text": "Изображение поставлено в очередь. Результат появится в рабочей зоне автоматически.",
            "balance_tokens": balance_tokens,
        }
    except HTTPException:
        if charged:
            try:
                try:
                    add_tokens(uid, cost, reason=f"{reason}_refund", ref_id=ref_id or uuid4().hex, meta={"origin": "workspace_image", "stage": "route_http_exception"})
                except TypeError:
                    add_tokens(uid, int(cost), reason=f"{reason}_refund")
            except Exception:
                pass
        _mark_workspace_image_generation_failed(generation_id, "Queueing failed", error_code="queue_error")
        raise
    except Exception as e:
        if charged:
            try:
                try:
                    add_tokens(uid, cost, reason=f"{reason}_refund", ref_id=ref_id or uuid4().hex, meta={"origin": "workspace_image", "error": str(e)})
                except TypeError:
                    add_tokens(uid, int(cost), reason=f"{reason}_refund")
            except Exception:
                pass
        _mark_workspace_image_generation_failed(generation_id, str(e), error_code="queue_error")
        raise HTTPException(status_code=500, detail=f"Image generation failed: {e}")


@router.post("/image/action")
async def workspace_image_action(
    payload: WorkspaceImageActionIn,
    user: Dict[str, Any] = Depends(get_current_workspace_user),
) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    generation_id_text = str(payload.generation_id or "").strip()
    if not generation_id_text:
        raise HTTPException(status_code=400, detail="Missing generation_id")

    action = str(payload.action or "").strip().lower()
    if action not in {"reroll", "variation"}:
        raise HTTPException(status_code=400, detail="Unsupported action")

    if supabase is None:
        raise HTTPException(status_code=500, detail="Supabase is not configured")

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
        source_row = rows[0]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Image generation load failed: {e}")

    if str(source_row.get("provider") or "").strip().lower() != "midjourney":
        raise HTTPException(status_code=400, detail="Actions are currently supported only for Midjourney")

    source_task_id = str(source_row.get("provider_task_id") or "").strip()
    if not source_task_id:
        raise HTTPException(status_code=400, detail="Source Midjourney task id is missing. Apply SQL migration and regenerate the image.")

    image_no = None if payload.image_no is None else int(payload.image_no)
    if action == "variation" and image_no not in {0, 1, 2, 3}:
        raise HTTPException(status_code=400, detail="variation requires image_no 0..3")

    variation_type = str(payload.variation_type or "subtle").strip().lower()
    if action == "variation" and variation_type not in {"subtle", "strong"}:
        raise HTTPException(status_code=400, detail="variation_type must be subtle or strong")

    speed_mode = _workspace_speed_mode(payload.speed_mode or source_row.get("mj_speed_mode") or "fast")
    prompt = str(source_row.get("prompt") or "").strip()
    aspect_ratio = str(source_row.get("aspect_ratio") or "1:1").strip() or "1:1"
    negative_prompt = str(source_row.get("negative_prompt") or "").strip()
    mj_stylize = source_row.get("mj_stylize")
    mj_chaos = source_row.get("mj_chaos")
    mj_raw = _workspace_boolish(source_row.get("mj_raw"))
    mj_seed = str(source_row.get("mj_seed") or "").strip()
    style_ref_urls = _workspace_json_field(source_row.get("style_ref_urls_json"), [])
    omni_ref_url = str(source_row.get("omni_ref_url") or "").strip() or None

    ensure_user_row(uid)
    try:
        bal = float(get_balance(uid) or 0)
    except Exception:
        bal = 0

    cost = int(_workspace_image_cost("midjourney", "text_to_image", "", "2K", speed_mode=speed_mode, action_type=action))
    reason = _workspace_image_charge_reason("midjourney", "text_to_image", action_type=action)
    if cost > 0 and bal < cost:
        raise HTTPException(status_code=402, detail=f"Недостаточно токенов. Нужно: {cost} ток.")

    charged = False
    ref_id = uuid4().hex if cost > 0 and reason else ""
    new_generation_id = _insert_workspace_image_generation(
        {
            "user_id": str(uid),
            "provider": "midjourney",
            "model": str(source_row.get("model") or "midjourney-v7"),
            "mode": "text_to_image",
            "prompt": prompt,
            "status": "queued",
            "resolution": source_row.get("resolution") or "2K",
            "aspect_ratio": aspect_ratio,
            "negative_prompt": negative_prompt,
            "mj_stylize": mj_stylize,
            "mj_chaos": mj_chaos,
            "mj_raw": mj_raw,
            "mj_speed_mode": speed_mode,
            "mj_seed": mj_seed,
            "style_ref_urls_json": style_ref_urls,
            "omni_ref_url": omni_ref_url,
            "parent_generation_id": generation_id_text,
            "action_type": action,
            "selected_image_no": image_no,
            "origin": "workspace_image",
        }
    )

    try:
        if cost > 0 and reason:
            try:
                add_tokens(uid, -cost, reason=reason, ref_id=ref_id, meta={"origin": "workspace_image", "provider": "midjourney", "action": action})
            except TypeError:
                add_tokens(uid, -int(cost), reason=reason)
            charged = True

        await enqueue_job(
            {
                "job_id": uuid4().hex,
                "kind": "workspace_image_run",
                "generation_id": new_generation_id,
                "user_id": uid,
                "provider": "midjourney",
                "model": str(source_row.get("model") or "midjourney-v7"),
                "mode": "text_to_image",
                "prompt": prompt,
                "run_prompt": build_midjourney_v7_prompt(
                    prompt=prompt,
                    aspect_ratio=aspect_ratio,
                    stylize=mj_stylize,
                    chaos=mj_chaos,
                    raw_mode=mj_raw,
                    negative_prompt=negative_prompt,
                    seed=mj_seed,
                    speed_mode=speed_mode,
                    style_ref_urls=style_ref_urls,
                    omni_ref_url=omni_ref_url,
                ),
                "aspect_ratio": aspect_ratio,
                "negative_prompt": negative_prompt,
                "mj_stylize": mj_stylize,
                "mj_chaos": mj_chaos,
                "mj_raw": mj_raw,
                "mj_speed_mode": speed_mode,
                "mj_seed": mj_seed,
                "style_ref_urls": style_ref_urls,
                "omni_ref_url": omni_ref_url,
                "mj_action": action,
                "source_task_id": source_task_id,
                "selected_image_no": image_no,
                "variation_type": variation_type,
                "charge_tokens": int(cost if charged else 0),
                "charge_ref_id": ref_id,
                "refund_reason": f"{reason}_refund" if reason else "workspace_image_refund",
                "origin": "workspace_image",
            },
            queue_name=WORKSPACE_IMAGE_QUEUE_NAME,
        )

        try:
            balance_tokens = int(get_balance(uid) or 0)
        except Exception:
            balance_tokens = None
        return {
            "ok": True,
            "generation_id": new_generation_id,
            "provider": "midjourney",
            "action": action,
            "tokens_required": cost,
            "status": "queued",
            "status_text": "Midjourney задача поставлена в очередь. Результат появится в рабочей зоне автоматически.",
            "balance_tokens": balance_tokens,
        }
    except HTTPException:
        if charged:
            try:
                try:
                    add_tokens(uid, cost, reason=f"{reason}_refund", ref_id=ref_id or uuid4().hex, meta={"origin": "workspace_image", "stage": "route_http_exception"})
                except TypeError:
                    add_tokens(uid, int(cost), reason=f"{reason}_refund")
            except Exception:
                pass
        _mark_workspace_image_generation_failed(new_generation_id, "Queueing failed", error_code="queue_error")
        raise
    except Exception as e:
        if charged:
            try:
                try:
                    add_tokens(uid, cost, reason=f"{reason}_refund", ref_id=ref_id or uuid4().hex, meta={"origin": "workspace_image", "error": str(e)})
                except TypeError:
                    add_tokens(uid, int(cost), reason=f"{reason}_refund")
            except Exception:
                pass
        _mark_workspace_image_generation_failed(new_generation_id, str(e), error_code="queue_error")
        raise HTTPException(status_code=500, detail=f"Image action failed: {e}")



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

    try:
        await enqueue_job(
            {
                "job_id": uuid4().hex,
                "kind": "workspace_music_run",
                "generation_id": generation_id,
                "user_id": uid,
                "payload": payload.model_dump(),
                "charge_tokens": int(cost if charged else 0),
                "charge_ref_id": ref_id,
                "origin": "workspace",
            },
            queue_name=WORKSPACE_MEDIA_QUEUE_NAME,
        )
    except Exception as e:
        if charged:
            try:
                add_tokens(uid, int(cost), reason="workspace_music_refund", ref_id=ref_id or uuid4().hex, meta={"origin": "workspace_music", "error": str(e)[:300], "stage": "enqueue"})
            except Exception:
                pass
        _mark_workspace_music_failed(generation_id, str(e), error_code="queue_error")
        raise HTTPException(status_code=500, detail=f"Music run failed: {e}")

    try:
        balance_tokens = int(get_balance(uid) or 0)
    except Exception:
        balance_tokens = None
    return {"ok": True, "generation_id": generation_id, "status": "queued", "cost_tokens": int(cost if charged else 0), "balance_tokens": balance_tokens}


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
