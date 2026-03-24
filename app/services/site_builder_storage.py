from __future__ import annotations

import os
import re
from typing import Optional

from db_supabase import supabase

SITE_BUILDS_BUCKET = (os.getenv("SITE_BUILDS_BUCKET", "site-builds") or "site-builds").strip() or "site-builds"


def _require_client() -> None:
    if supabase is None:
        raise RuntimeError("Supabase client is not configured")


def safe_slug(value: str, *, default: str = "site") -> str:
    raw = (value or "").strip().lower()
    raw = re.sub(r"[^a-z0-9а-яё_-]+", "-", raw, flags=re.IGNORECASE)
    raw = re.sub(r"-+", "-", raw).strip("-_")
    return raw or default


def build_zip_storage_path(*, user_id: int, project_id: str, version_number: int, title: str) -> str:
    slug = safe_slug(title, default="site")
    return f"{int(user_id)}/sites/{project_id}/v{int(version_number)}/{slug}-v{int(version_number)}.zip"


def upload_zip(*, path: str, raw_bytes: bytes) -> str:
    _require_client()
    if not raw_bytes:
        raise ValueError("raw_bytes is empty")
    try:
        supabase.storage.from_(SITE_BUILDS_BUCKET).upload(
            path,
            raw_bytes,
            {"content-type": "application/zip", "upsert": "true"},
        )
    except Exception as exc:
        raise RuntimeError(f"Supabase upload failed: {exc}")
    return path


def download_zip(path: str) -> bytes:
    _require_client()
    try:
        data = supabase.storage.from_(SITE_BUILDS_BUCKET).download(path)
        return bytes(data)
    except Exception as exc:
        raise RuntimeError(f"Supabase download failed: {exc}")


def create_signed_zip_url(path: str, expires_in: int = 3600) -> Optional[str]:
    _require_client()
    try:
        res = supabase.storage.from_(SITE_BUILDS_BUCKET).create_signed_url(path, int(expires_in))
        if isinstance(res, dict):
            return str(res.get("signedURL") or res.get("signedUrl") or "") or None
        if isinstance(res, str):
            return res
    except Exception:
        return None
    return None
