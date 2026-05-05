"""Supabase Storage helpers for AI chat attachments.

Goal: keep Redis chat jobs small. Raw files live in Supabase Storage;
Redis receives only storage refs plus extracted text/context.
"""

from __future__ import annotations

import mimetypes
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote
from uuid import uuid4

import httpx

SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
SUPABASE_SERVICE_KEY = (os.getenv("SUPABASE_SERVICE_KEY") or "").strip()
CHAT_ATTACHMENTS_BUCKET = (
    os.getenv("CHAT_ATTACHMENTS_BUCKET")
    or os.getenv("WORKSPACE_CHAT_ATTACHMENTS_BUCKET")
    or os.getenv("WORKSPACE_VIDEOS_BUCKET")
    or "workspace-videos"
).strip() or "workspace-videos"
CHAT_ATTACHMENTS_PREFIX = (
    os.getenv("CHAT_ATTACHMENTS_PREFIX")
    or os.getenv("WORKSPACE_CHAT_ATTACHMENTS_PREFIX")
    or "chat-attachments"
).strip().strip("/") or "chat-attachments"
CHAT_ATTACHMENT_SIGNED_URL_TTL_SEC = int(os.getenv("CHAT_ATTACHMENT_SIGNED_URL_TTL_SEC", "3600") or "3600")


def is_chat_storage_configured() -> bool:
    return bool(SUPABASE_URL and SUPABASE_SERVICE_KEY and CHAT_ATTACHMENTS_BUCKET)


def _headers(content_type: Optional[str] = None) -> Dict[str, str]:
    headers = {
        "authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "apikey": SUPABASE_SERVICE_KEY,
    }
    if content_type:
        headers["content-type"] = content_type
    return headers


def safe_filename(filename: str) -> str:
    name = Path(filename or "file").name or "file"
    stem = Path(name).stem or "file"
    ext = Path(name).suffix.lower()[:16]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")[:80] or "file"
    ext = re.sub(r"[^A-Za-z0-9.]+", "", ext)
    return f"{stem}{ext}" if ext else stem


def _guess_ext(filename: str, content_type: str) -> str:
    ext = Path(filename or "").suffix.lower().lstrip(".")
    if ext:
        return ext[:16]
    guessed = mimetypes.guess_extension(content_type or "") or ""
    return guessed.lower().lstrip(".")[:16] or "bin"


def build_chat_attachment_path(*, user_id: Any, origin: str, filename: str, job_id: Optional[str] = None) -> str:
    uid = str(user_id or "anonymous").strip() or "anonymous"
    uid = re.sub(r"[^A-Za-z0-9_-]+", "_", uid)[:64] or "anonymous"
    origin_safe = re.sub(r"[^A-Za-z0-9_-]+", "_", str(origin or "chat").strip())[:32] or "chat"
    now = datetime.now(timezone.utc)
    clean_name = safe_filename(filename)
    ext = _guess_ext(clean_name, mimetypes.guess_type(clean_name)[0] or "application/octet-stream")
    stem = Path(clean_name).stem[:64] or "file"
    unique = str(job_id or uuid4().hex)[:48]
    return f"{CHAT_ATTACHMENTS_PREFIX}/{origin_safe}/{uid}/{now:%Y/%m/%d}/{unique}_{stem}.{ext}"


def public_object_url(bucket: str, path: str) -> str:
    return f"{SUPABASE_URL}/storage/v1/object/public/{quote(bucket.strip('/'))}/{quote(path.lstrip('/'), safe='/')}"


async def create_signed_url(bucket: str, path: str, *, expires_in: int = CHAT_ATTACHMENT_SIGNED_URL_TTL_SEC) -> str:
    if not is_chat_storage_configured():
        return ""
    bucket = (bucket or CHAT_ATTACHMENTS_BUCKET).strip()
    path = str(path or "").strip().lstrip("/")
    if not bucket or not path:
        return ""
    url = f"{SUPABASE_URL}/storage/v1/object/sign/{quote(bucket)}/{quote(path, safe='/')}"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                url,
                headers=_headers("application/json"),
                json={"expiresIn": max(60, int(expires_in or CHAT_ATTACHMENT_SIGNED_URL_TTL_SEC))},
            )
        if response.status_code >= 300:
            return ""
        data = response.json()
        signed = str(data.get("signedURL") or data.get("signedUrl") or data.get("signed_url") or "")
        if signed.startswith("http://") or signed.startswith("https://"):
            return signed
        if signed:
            return f"{SUPABASE_URL}/storage/v1{signed if signed.startswith('/') else '/' + signed}"
    except Exception:
        return ""
    return ""


async def upload_chat_attachment_bytes(
    raw: bytes,
    *,
    filename: str,
    content_type: str = "application/octet-stream",
    user_id: Any = None,
    origin: str = "workspace",
    job_id: Optional[str] = None,
    bucket: Optional[str] = None,
) -> Dict[str, Any]:
    if not raw:
        raise RuntimeError("Empty chat attachment")
    if not is_chat_storage_configured():
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY are not configured for chat attachment storage")

    bucket_name = (bucket or CHAT_ATTACHMENTS_BUCKET).strip() or CHAT_ATTACHMENTS_BUCKET
    clean_name = safe_filename(filename)
    content_type = (content_type or mimetypes.guess_type(clean_name)[0] or "application/octet-stream").strip()
    path = build_chat_attachment_path(user_id=user_id, origin=origin, filename=clean_name, job_id=job_id)
    put_url = f"{SUPABASE_URL}/storage/v1/object/{quote(bucket_name)}/{quote(path, safe='/')}"
    headers = _headers(content_type)
    headers["x-upsert"] = "true"

    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.put(put_url, headers=headers, content=raw)
    if response.status_code >= 300:
        raise RuntimeError(f"Supabase chat attachment upload failed: {response.status_code} {response.text[:300]}")

    signed_url = await create_signed_url(bucket_name, path)
    return {
        "storage_bucket": bucket_name,
        "storage_path": path,
        "storage_url": signed_url or public_object_url(bucket_name, path),
    }


async def download_chat_attachment_bytes(bucket: str, path: str) -> bytes:
    if not is_chat_storage_configured():
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY are not configured for chat attachment storage")
    bucket_name = (bucket or CHAT_ATTACHMENTS_BUCKET).strip() or CHAT_ATTACHMENTS_BUCKET
    object_path = str(path or "").strip().lstrip("/")
    if not object_path:
        raise RuntimeError("Empty chat attachment storage_path")
    url = f"{SUPABASE_URL}/storage/v1/object/{quote(bucket_name)}/{quote(object_path, safe='/')}"
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.get(url, headers=_headers())
    if response.status_code >= 300:
        raise RuntimeError(f"Supabase chat attachment download failed: {response.status_code} {response.text[:300]}")
    return response.content
