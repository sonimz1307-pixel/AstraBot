from __future__ import annotations

import io
import json
import mimetypes
import os
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, Field

from ai_chat import openai_chat_answer
from app.routers.prompts import categories as prompts_categories
from app.routers.prompts import groups as prompts_groups
from app.routers.prompts import items as prompts_items
from app.routers.tts import ALLOWED_VOICE_IDS, ALLOWED_VOICES
from app.services.eleven_tts import ElevenTTS
from app.services.telegram_webauth import TelegramWebAuthError, validate_telegram_login_data
from app.services.workspace_auth import WORKSPACE_SESSION_TTL_SEC, create_access_token, get_current_workspace_user, get_optional_workspace_user
from billing_db import add_tokens, ensure_user_row, get_balance
from db_supabase import supabase, track_user_activity
from kling3_flow import Kling3Error, create_kling3_task, get_kling3_task
from kling3_pricing import calculate_kling3_price
from songwriter_prompt import SONGWRITER_SYSTEM_PROMPT

router = APIRouter(prefix="/api/workspace", tags=["workspace"])
OPENAI_CHAT_MODEL = (os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini") or "gpt-4o-mini").strip()
PROMPT_BUILDER_MODEL = (os.getenv("PROMPT_BUILDER_MODEL", "gpt-5.4") or "gpt-5.4").strip()
_tts_client: ElevenTTS | None = None


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
    return {
        "telegram_user_id": int(user.get("telegram_user_id") or user.get("id") or 0),
        "username": user.get("username"),
        "first_name": user.get("first_name"),
        "last_name": user.get("last_name"),
        "language_code": user.get("language_code"),
        "photo_url": user.get("photo_url"),
        "is_premium": bool(user.get("is_premium", False)),
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

_WORKSPACE_VIDEOS_BUCKET = (os.getenv("WORKSPACE_VIDEOS_BUCKET", "workspace-videos") or "workspace-videos").strip() or "workspace-videos"


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


class TelegramAuthPayload(BaseModel):
    auth_data: Dict[str, Any]


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


class SongwriterPayload(BaseModel):
    text: str = Field("", description="Current user message")
    history: Optional[List[ChatTurn]] = None
    language: Optional[str] = None
    genre: Optional[str] = None
    mood: Optional[str] = None
    references: Optional[str] = None


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

    tg_user = {
        "id": uid,
        "username": verified.get("username") or existing.get("username"),
        "first_name": verified.get("first_name") or existing.get("first_name"),
        "last_name": verified.get("last_name") or existing.get("last_name"),
        "language_code": existing.get("language_code"),
        "photo_url": verified.get("photo_url"),
        "is_premium": existing.get("is_premium", False),
    }
    ensure_user_row(uid)
    track_user_activity(tg_user)
    access_token = create_access_token(user=tg_user)
    balance = int(get_balance(uid) or 0)
    return {
        "ok": True,
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": WORKSPACE_SESSION_TTL_SEC,
        "user": _workspace_user_payload(tg_user),
        "balance_tokens": balance,
    }


@router.post("/logout")
async def workspace_logout() -> Dict[str, Any]:
    return {"ok": True}


@router.get("/me")
async def workspace_me(user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    ensure_user_row(uid)
    return {"ok": True, "user": _workspace_user_payload(user), "balance_tokens": int(get_balance(uid) or 0)}


@router.get("/balance")
async def workspace_balance(user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    ensure_user_row(uid)
    balance = int(get_balance(uid) or 0)
    return {"ok": True, "balance_tokens": balance}


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
        files = [f for f in form.getlist("files") if isinstance(f, UploadFile)]
    else:
        payload = WorkspaceChatIn.model_validate(await request.json())
        text_value = payload.text.strip()
        mode = _normalize_chat_mode_value(payload.mode)
        history = [{"role": item.role, "content": item.content} for item in (payload.history or []) if item.role in ("user", "assistant")]
        temperature = payload.temperature
        max_tokens = payload.max_tokens
        resolved_model = _resolve_workspace_chat_model(payload.model, mode)

    if not text_value and not files:
        raise HTTPException(status_code=400, detail="Введите текст или прикрепите хотя бы один файл.")

    prepared_files = await _prepare_workspace_chat_attachments(files) if files else {"items": [], "context": "", "image_bytes_list": []}

    user_text = text_value or "Проанализируй приложенные файлы и кратко скажи, что в них находится, затем предложи полезные следующие шаги."
    if prepared_files.get("context"):
        user_text = f"{user_text}\n\n{prepared_files['context']}"

    model_label = resolved_model["label"]
    model_actual = resolved_model["actual"]
    system_prompt = (
        "Ты — AstraBot Prompt Builder. Отвечай как сильный AI prompt engineer и creative strategist. Строй ответ структурно: идея, основной промпт, улучшенная версия, опции под video/image/music. Если запрос расплывчатый — делай лучшую рабочую версию без лишних вопросов."
        if mode == "prompt_builder"
        else "Ты — AstraBot Workspace Assistant. Помогай как product-minded AI co-pilot: сценарии, промпты, creative direction, тексты, планы и упаковка идей в рабочий пайплайн. Пиши по делу, понятно и удобно для дальнейшего запуска в video/image/voice/music студиях."
    )
    system_prompt += f"\n\nТекущая выбранная модель в интерфейсе сайта: {model_label}. Если пользователь спрашивает, какая модель выбрана в интерфейсе, отвечай именно этим значением."

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
        item = _serialize_workspace_generation(rows[0])
        return {"ok": True, "item": item}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"History item load failed: {e}")


@router.get("/tts/voices")
async def workspace_tts_voices(user: Dict[str, Any] = Depends(get_current_workspace_user)) -> List[Dict[str, Any]]:
    return ALLOWED_VOICES


@router.post("/tts/generate")
async def workspace_tts_generate(payload: TTSGenerateIn, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Response:
    if payload.voice_id not in ALLOWED_VOICE_IDS:
        raise HTTPException(status_code=400, detail="voice_id is not allowed")
    tts = _get_tts()
    audio_bytes = await tts.tts(text=payload.text, voice_id=payload.voice_id, model_id=payload.model_id, output_format=payload.output_format)
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


@router.get("/prompts/categories")
async def workspace_prompts_categories(user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    return prompts_categories()


@router.get("/prompts/groups")
async def workspace_prompts_groups(category: str, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    return prompts_groups(category=category)


@router.get("/prompts/items")
async def workspace_prompts_items(group_id: str, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    return prompts_items(group_id=group_id)
