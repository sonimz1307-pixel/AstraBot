from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from db_supabase import supabase

SITE_PROJECTS_TABLE = "bot_site_projects"
SITE_VERSIONS_TABLE = "bot_site_versions"
SITE_JOBS_TABLE = "bot_site_jobs"

STATUS_DRAFT = "draft"
STATUS_WAITING_EXTRA_TEXTS = "waiting_extra_texts"
STATUS_PREVIEW_READY = "preview_ready"
STATUS_PAYMENT_PENDING = "payment_pending"
STATUS_QUEUED = "queued"
STATUS_GENERATING = "generating"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

JOB_CREATE = "create"
JOB_REVISION = "revision"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_client() -> None:
    if supabase is None:
        raise RuntimeError("Supabase client is not configured")


def _select_one(table: str, **where: Any) -> Optional[Dict[str, Any]]:
    _require_client()
    query = supabase.table(table).select("*")
    for key, value in where.items():
        query = query.eq(key, value)
    resp = query.limit(1).execute()
    rows = getattr(resp, "data", None) or []
    return rows[0] if rows and isinstance(rows[0], dict) else None


def _select_many(table: str, *, order_by: str = "created_at", desc: bool = True, limit: int = 100, **where: Any) -> List[Dict[str, Any]]:
    _require_client()
    query = supabase.table(table).select("*")
    for key, value in where.items():
        query = query.eq(key, value)
    resp = query.order(order_by, desc=desc).limit(limit).execute()
    return list(getattr(resp, "data", None) or [])


def create_project(*, telegram_user_id: int, title: str, brief_raw: str) -> Dict[str, Any]:
    _require_client()
    project_id = str(uuid4())
    row = {
        "id": project_id,
        "telegram_user_id": int(telegram_user_id),
        "title": str(title or "Новый сайт").strip() or "Новый сайт",
        "status": STATUS_DRAFT,
        "brief_raw": str(brief_raw or "").strip(),
        "extra_texts_raw": None,
        "current_version": 0,
        "free_revision_included": True,
        "free_revision_used": False,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    supabase.table(SITE_PROJECTS_TABLE).insert(row).execute()
    return row


def update_project(project_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    _require_client()
    if not patch:
        return get_project(project_id)
    payload = dict(patch)
    payload["updated_at"] = _now_iso()
    supabase.table(SITE_PROJECTS_TABLE).update(payload).eq("id", str(project_id)).execute()
    return get_project(project_id)


def get_project(project_id: str) -> Optional[Dict[str, Any]]:
    return _select_one(SITE_PROJECTS_TABLE, id=str(project_id))


def get_project_for_user(telegram_user_id: int, project_id: str) -> Optional[Dict[str, Any]]:
    return _select_one(SITE_PROJECTS_TABLE, id=str(project_id), telegram_user_id=int(telegram_user_id))


def list_projects_for_user(telegram_user_id: int, limit: int = 20) -> List[Dict[str, Any]]:
    return _select_many(SITE_PROJECTS_TABLE, telegram_user_id=int(telegram_user_id), order_by="updated_at", desc=True, limit=limit)


def create_job(
    *,
    project_id: str,
    telegram_user_id: int,
    job_type: str,
    tokens_cost: int,
    is_free_revision: bool,
    request_raw: Optional[str] = None,
) -> Dict[str, Any]:
    _require_client()
    job_id = str(uuid4())
    row = {
        "id": job_id,
        "project_id": str(project_id),
        "telegram_user_id": int(telegram_user_id),
        "job_type": str(job_type),
        "status": STATUS_QUEUED,
        "tokens_cost": int(tokens_cost),
        "is_free_revision": bool(is_free_revision),
        "request_raw": request_raw or None,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    supabase.table(SITE_JOBS_TABLE).insert(row).execute()
    return row


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    return _select_one(SITE_JOBS_TABLE, id=str(job_id))


def update_job(job_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    _require_client()
    payload = dict(patch or {})
    payload["updated_at"] = _now_iso()
    supabase.table(SITE_JOBS_TABLE).update(payload).eq("id", str(job_id)).execute()
    return get_job(job_id)


def list_jobs_for_project(project_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    return _select_many(SITE_JOBS_TABLE, project_id=str(project_id), order_by="created_at", desc=True, limit=limit)


def get_next_version_number(project_id: str) -> int:
    project = get_project(project_id)
    current = int((project or {}).get("current_version") or 0)
    return current + 1


def create_version(
    *,
    project_id: str,
    version_number: int,
    source_type: str,
    request_raw: Optional[str],
    blueprint_json: Optional[Dict[str, Any]],
    html_content: str,
    css_content: str,
    js_content: str,
    readme_content: str,
    zip_storage_path: str,
) -> Dict[str, Any]:
    _require_client()
    version_id = str(uuid4())
    row = {
        "id": version_id,
        "project_id": str(project_id),
        "version_number": int(version_number),
        "source_type": str(source_type),
        "request_raw": request_raw,
        "blueprint_json": blueprint_json or {},
        "html_content": html_content,
        "css_content": css_content,
        "js_content": js_content,
        "readme_content": readme_content,
        "zip_storage_path": zip_storage_path,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    supabase.table(SITE_VERSIONS_TABLE).insert(row).execute()
    return row


def list_versions(project_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    return _select_many(SITE_VERSIONS_TABLE, project_id=str(project_id), order_by="version_number", desc=True, limit=limit)


def get_version(project_id: str, version_number: int) -> Optional[Dict[str, Any]]:
    return _select_one(SITE_VERSIONS_TABLE, project_id=str(project_id), version_number=int(version_number))


def get_latest_version(project_id: str) -> Optional[Dict[str, Any]]:
    rows = _select_many(SITE_VERSIONS_TABLE, project_id=str(project_id), order_by="version_number", desc=True, limit=1)
    return rows[0] if rows else None


def get_version_for_user(telegram_user_id: int, project_id: str, version_number: int) -> Optional[Dict[str, Any]]:
    project = get_project_for_user(telegram_user_id, project_id)
    if not project:
        return None
    return get_version(project_id, version_number)


def mark_free_revision_used(project_id: str) -> Optional[Dict[str, Any]]:
    return update_project(project_id, {"free_revision_used": True})
