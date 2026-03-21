from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from app.services.site_builder_billing import (
    SITE_CREATE_PRICE,
    SITE_REVISION_PRICE,
    charge_site_create,
    charge_site_revision,
    get_site_balance,
)
from app.services.site_builder_repo import (
    JOB_CREATE,
    JOB_REVISION,
    STATUS_PAYMENT_PENDING,
    STATUS_PREVIEW_READY,
    STATUS_QUEUED,
    create_job,
    create_project,
    get_project_for_user,
    get_version_for_user,
    list_jobs_for_project,
    list_projects_for_user,
    list_versions,
    update_project,
)
from app.services.site_builder_storage import download_zip
from app.services.workspace_auth import get_current_workspace_user
from queue_redis import enqueue_job

router = APIRouter(prefix="/api/site-builder", tags=["site-builder"])
SITE_QUEUE_NAME = (os.getenv("SITE_QUEUE_NAME", "site") or "site").strip() or "site"


class SiteProjectCreateIn(BaseModel):
    title: str = Field(default="Новый сайт", max_length=160)
    brief_raw: str = Field(min_length=10, max_length=50000)
    extra_texts_raw: str | None = Field(default=None, max_length=50000)


class SiteRevisionIn(BaseModel):
    request_raw: str = Field(min_length=4, max_length=20000)


class EmptyBody(BaseModel):
    pass


@router.get("/meta")
async def api_site_builder_meta(user=Depends(get_current_workspace_user)):
    uid = int(user["telegram_user_id"])
    return {
        "ok": True,
        "prices": {"create": SITE_CREATE_PRICE, "revision": SITE_REVISION_PRICE},
        "balance_tokens": get_site_balance(uid),
    }


@router.get("/projects")
async def api_list_projects(user=Depends(get_current_workspace_user)):
    uid = int(user["telegram_user_id"])
    return {"ok": True, "items": list_projects_for_user(uid, limit=50)}


@router.post("/projects")
async def api_create_project(payload: SiteProjectCreateIn, user=Depends(get_current_workspace_user)):
    uid = int(user["telegram_user_id"])
    title = str(payload.title or "Новый сайт").strip() or "Новый сайт"
    brief_raw = str(payload.brief_raw or "").strip()
    extra_texts_raw = str(payload.extra_texts_raw or "").strip() or None
    if len(brief_raw) < 10:
        raise HTTPException(status_code=400, detail="Brief is too short")

    item = create_project(telegram_user_id=uid, title=title, brief_raw=brief_raw)
    patch = {"status": STATUS_PREVIEW_READY}
    if extra_texts_raw:
        patch["extra_texts_raw"] = extra_texts_raw
    item = update_project(str(item["id"]), patch) or item
    return {"ok": True, "item": item}


@router.get("/projects/{project_id}")
async def api_get_project(project_id: str, user=Depends(get_current_workspace_user)):
    uid = int(user["telegram_user_id"])
    row = get_project_for_user(uid, project_id)
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    return {
        "ok": True,
        "item": row,
        "versions": list_versions(project_id, limit=20),
        "jobs": list_jobs_for_project(project_id, limit=20),
    }


@router.post("/projects/{project_id}/build")
async def api_run_build(project_id: str, _payload: EmptyBody | None = None, user=Depends(get_current_workspace_user)):
    uid = int(user["telegram_user_id"])
    project = get_project_for_user(uid, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if int(project.get("current_version") or 0) > 0:
        raise HTTPException(status_code=400, detail="Project already has a version. Use revision instead.")
    balance = get_site_balance(uid)
    if balance < SITE_CREATE_PRICE:
        raise HTTPException(status_code=400, detail=f"Недостаточно токенов. Нужно {SITE_CREATE_PRICE}, баланс: {balance}.")

    charge_site_create(user_id=uid, project_id=project_id, meta={"title": project.get("title")})
    job = create_job(
        project_id=project_id,
        telegram_user_id=uid,
        job_type=JOB_CREATE,
        tokens_cost=SITE_CREATE_PRICE,
        is_free_revision=False,
        request_raw=None,
    )
    update_project(project_id, {"status": STATUS_PAYMENT_PENDING, "last_job_id": job["id"]})
    await enqueue_job({"job_id": job["id"], "kind": "site_build", "telegram_user_id": uid}, queue_name=SITE_QUEUE_NAME)
    return {
        "ok": True,
        "job": job,
        "item": get_project_for_user(uid, project_id),
        "balance_tokens": get_site_balance(uid),
    }


@router.post("/projects/{project_id}/revisions")
async def api_run_revision(project_id: str, payload: SiteRevisionIn, user=Depends(get_current_workspace_user)):
    uid = int(user["telegram_user_id"])
    project = get_project_for_user(uid, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if int(project.get("current_version") or 0) <= 0:
        raise HTTPException(status_code=400, detail="У проекта пока нет готовой версии. Сначала запусти создание сайта.")

    revision_text = str(payload.request_raw or "").strip()
    if len(revision_text) < 4:
        raise HTTPException(status_code=400, detail="Revision request is too short")

    is_free_revision = not bool(project.get("free_revision_used"))
    tokens_cost = 0 if is_free_revision else SITE_REVISION_PRICE
    if not is_free_revision:
        balance = get_site_balance(uid)
        if balance < SITE_REVISION_PRICE:
            raise HTTPException(status_code=400, detail=f"Недостаточно токенов для правки. Нужно {SITE_REVISION_PRICE}, баланс: {balance}.")

    job = create_job(
        project_id=project_id,
        telegram_user_id=uid,
        job_type=JOB_REVISION,
        tokens_cost=tokens_cost,
        is_free_revision=is_free_revision,
        request_raw=revision_text,
    )
    if not is_free_revision:
        charge_site_revision(user_id=uid, job_id=job["id"], project_id=project_id, meta={"title": project.get("title")})
    update_project(project_id, {"status": STATUS_QUEUED, "last_job_id": job["id"]})
    await enqueue_job({"job_id": job["id"], "kind": "site_revision", "telegram_user_id": uid}, queue_name=SITE_QUEUE_NAME)
    return {
        "ok": True,
        "job": job,
        "item": get_project_for_user(uid, project_id),
        "balance_tokens": get_site_balance(uid),
    }


@router.get("/projects/{project_id}/versions/{version_number}/download")
async def api_download_version(project_id: str, version_number: int, user=Depends(get_current_workspace_user)):
    uid = int(user["telegram_user_id"])
    version = get_version_for_user(uid, project_id, version_number)
    if not version:
        raise HTTPException(status_code=404, detail="Version not found")
    raw = download_zip(str(version.get("zip_storage_path") or ""))
    headers = {"Content-Disposition": f'attachment; filename="site-v{int(version_number)}.zip"'}
    return Response(content=raw, media_type="application/zip", headers=headers)
