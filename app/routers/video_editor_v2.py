from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.services.workspace_auth import get_current_workspace_user
from app.services.video_editor_v2_service import (
    VIDEO_EDITOR_QUEUE_NAME,
    create_project,
    create_render_job,
    delete_project,
    get_project,
    get_render_job,
    list_generation_library,
    list_projects,
    list_upload_library,
    normalize_project_json,
    process_render_job,
    update_project,
)
from app.services.video_editor_service import create_workspace_upload_record
from queue_redis import enqueue_job

router = APIRouter(prefix="/api/video-editor-v2", tags=["video-editor-v2"])
page_router = APIRouter(tags=["video-editor-v2-page"])
BASE_DIR = Path(__file__).resolve().parents[2] / "web_workspace_frontend" / "video_editor_v2"


class ProjectPayload(BaseModel):
    title: str = Field(default="Новый видеопроект", max_length=120)
    project_json: Dict[str, Any] = Field(default_factory=dict)


class RenderPayload(BaseModel):
    project_id: str


@page_router.get("/workspace/video-editor-v2")
async def video_editor_v2_page() -> Response:
    return FileResponse(BASE_DIR / "index.html")


@page_router.get("/workspace/video-editor-v2/app.js")
async def video_editor_v2_js() -> Response:
    return FileResponse(BASE_DIR / "app.js", media_type="application/javascript")


@page_router.get("/workspace/video-editor-v2/styles.css")
async def video_editor_v2_css() -> Response:
    return FileResponse(BASE_DIR / "styles.css", media_type="text/css")


@router.get("/projects")
async def api_list_projects(user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    return {"ok": True, "items": list_projects(uid)}


@router.post("/projects")
async def api_create_project(payload: ProjectPayload, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    try:
        row = create_project(uid, payload.title, payload.project_json)
        return {"ok": True, "item": row}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/projects/{project_id}")
async def api_get_project(project_id: str, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    row = get_project(uid, project_id)
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"ok": True, "item": row}


@router.put("/projects/{project_id}")
async def api_update_project(project_id: str, payload: ProjectPayload, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    try:
        row = update_project(uid, project_id, payload.title, payload.project_json)
        return {"ok": True, "item": row}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/projects/{project_id}")
async def api_delete_project(project_id: str, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    delete_project(uid, project_id)
    return {"ok": True}


@router.post("/upload/video")
async def api_upload_video(file: UploadFile = File(...), user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    raw = await file.read()
    try:
        row = create_workspace_upload_record(user_id=uid, filename=file.filename or "video.mp4", content_type=file.content_type or "video/mp4", raw_bytes=raw)
        if row.get("file_type") != "video":
            raise HTTPException(status_code=400, detail="Нужен видеофайл")
        return {"ok": True, "item": row}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/upload/audio")
async def api_upload_audio(file: UploadFile = File(...), user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    raw = await file.read()
    try:
        row = create_workspace_upload_record(user_id=uid, filename=file.filename or "audio.mp3", content_type=file.content_type or "audio/mpeg", raw_bytes=raw)
        if row.get("file_type") != "audio":
            raise HTTPException(status_code=400, detail="Нужен аудиофайл")
        return {"ok": True, "item": row}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/library/videos")
async def api_library_videos(user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    return {"ok": True, "items": list_generation_library(uid)}


@router.get("/library/uploads")
async def api_library_uploads(file_type: Optional[str] = None, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    return {"ok": True, "items": list_upload_library(uid, file_type=file_type)}


@router.post("/render")
async def api_render(payload: RenderPayload, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    try:
        row = create_render_job(uid, payload.project_id)
        try:
            await enqueue_job({"job_id": row["id"], "kind": "video_editor_v2_render"}, queue_name=VIDEO_EDITOR_QUEUE_NAME)
            mode = "queue"
        except Exception:
            asyncio.create_task(asyncio.to_thread(process_render_job, row["id"]))
            mode = "background"
        return {"ok": True, "item": row, "mode": mode}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/render/{job_id}")
async def api_render_status(job_id: str, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    uid = int(user["telegram_user_id"])
    row = get_render_job(uid, job_id)
    if not row:
        raise HTTPException(status_code=404, detail="Render job not found")
    return {"ok": True, "item": row}
