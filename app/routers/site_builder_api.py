from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response

from app.services.site_builder_repo import get_project_for_user, get_version_for_user, list_projects_for_user, list_versions
from app.services.site_builder_storage import download_zip
from app.services.workspace_auth import get_current_workspace_user

router = APIRouter(prefix="/api/site-builder", tags=["site-builder"])


@router.get("/projects")
async def api_list_projects(user=Depends(get_current_workspace_user)):
    uid = int(user["telegram_user_id"])
    return {"ok": True, "items": list_projects_for_user(uid, limit=50)}


@router.get("/projects/{project_id}")
async def api_get_project(project_id: str, user=Depends(get_current_workspace_user)):
    uid = int(user["telegram_user_id"])
    row = get_project_for_user(uid, project_id)
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"ok": True, "item": row, "versions": list_versions(project_id, limit=20)}


@router.get("/projects/{project_id}/versions/{version_number}/download")
async def api_download_version(project_id: str, version_number: int, user=Depends(get_current_workspace_user)):
    uid = int(user["telegram_user_id"])
    version = get_version_for_user(uid, project_id, version_number)
    if not version:
        raise HTTPException(status_code=404, detail="Version not found")
    raw = download_zip(str(version.get("zip_storage_path") or ""))
    headers = {"Content-Disposition": f'attachment; filename="site-v{int(version_number)}.zip"'}
    return Response(content=raw, media_type="application/zip", headers=headers)
