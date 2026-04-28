from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel, Field

from billing_db import add_tokens
from kling3_kie_flow import Kling3KieError, create_kling3_kie_task, get_kling3_kie_task, normalize_kling3_kie_task, upload_kling3_kie_input_bytes
from kling3_kie_pricing import calculate_kling3_kie_price, normalize_kling3_kie_duration, normalize_kling3_kie_mode

router = APIRouter()


def _model_dump(obj: Any) -> Dict[str, Any]:
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    return dict(obj)


@router.get("/health", summary="Kling 3.0 - New healthcheck")
async def kling3_kie_health() -> Dict[str, Any]:
    return {"ok": True, "service": "kling3_kie", "display_name": "Kling 3.0 - New"}


class Kling3KieShot(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=500)
    duration: int = Field(..., ge=1, le=12)


class Kling3KieElement(BaseModel):
    name: str = Field(..., min_length=1, max_length=48)
    description: str = Field(default="", max_length=160)
    element_input_urls: List[str] = Field(default_factory=list)
    element_input_video_urls: List[str] = Field(default_factory=list)


class Kling3KieCreateBody(BaseModel):
    telegram_user_id: int = Field(..., ge=1)
    prompt: str = Field(default="")
    duration: int = Field(default=5, ge=3, le=15)
    mode: str = Field(default="pro")
    generation_mode: str = Field(default="text_to_video")
    enable_audio: bool = False
    aspect_ratio: str = Field(default="16:9")
    start_image_url: Optional[str] = None
    end_image_url: Optional[str] = None
    multi_shots: List[Kling3KieShot] = Field(default_factory=list)
    kling_elements: List[Kling3KieElement] = Field(default_factory=list)




@router.post("/upload-reference", summary="Upload Kling 3.0 - New reference/element file to Supabase")
async def kling3_kie_upload_reference(
    file: UploadFile = File(...),
    telegram_user_id: int = Form(0),
    slot: str = Form("element"),
) -> Dict[str, Any]:
    """Upload a Telegram WebApp-selected file and return a public URL for KIE."""
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file")

    content_type = (file.content_type or "application/octet-stream").lower()
    filename = (file.filename or "").lower()
    is_video = content_type.startswith("video/") or filename.endswith((".mp4", ".mov"))
    is_image = content_type in {"image/jpeg", "image/jpg", "image/png"} or filename.endswith((".jpg", ".jpeg", ".png"))

    if not (is_image or is_video):
        raise HTTPException(status_code=400, detail="Only JPG/PNG image files and MP4/MOV video files are supported")
    if is_image and len(raw) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image file is too large. Max 10MB")
    if is_video and len(raw) > 50 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Video file is too large. Max 50MB")

    safe_slot = str(slot or "element").strip().lower().replace("/", "_")[:40] or "element"
    prefix = f"kling3-kie/webapp/{int(telegram_user_id or 0)}/{safe_slot}"
    try:
        public_url = upload_kling3_kie_input_bytes(
            raw,
            filename=file.filename,
            content_type=content_type,
            prefix=prefix,
        )
        return {
            "ok": True,
            "public_url": public_url,
            "url": public_url,
            "content_type": content_type,
            "filename": file.filename,
            "size_bytes": len(raw),
            "kind": "video" if is_video else "image",
        }
    except Kling3KieError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Upload failed: {exc}")


@router.post("/create", summary="Create Kling 3.0 - New task with token charge")
async def kling3_kie_create(body: Kling3KieCreateBody) -> Dict[str, Any]:
    request_id = uuid4().hex
    shots = [_model_dump(item) for item in body.multi_shots]
    mode = normalize_kling3_kie_mode(body.mode)
    duration = normalize_kling3_kie_duration(body.duration)
    tokens_required = 0
    try:
        tokens_required = calculate_kling3_kie_price(mode, body.enable_audio, duration, multi_shots=shots)
        add_tokens(
            body.telegram_user_id,
            -tokens_required,
            reason="kling3_kie_create",
            ref_id=request_id,
            meta={"provider": "kie", "model": "Kling 3.0 - New", "mode": mode, "duration": duration, "enable_audio": body.enable_audio},
        )
        task = await create_kling3_kie_task(
            prompt=body.prompt,
            duration=duration,
            mode=mode,
            enable_audio=body.enable_audio,
            aspect_ratio=body.aspect_ratio,
            generation_mode=body.generation_mode,
            start_image_url=body.start_image_url,
            end_image_url=body.end_image_url,
            multi_shots=shots,
            kling_elements=[_model_dump(item) for item in body.kling_elements],
            request_id=request_id,
        )
        normalized = normalize_kling3_kie_task(task if isinstance(task, dict) else {"raw": task})
        return {"ok": True, "request_id": request_id, "tokens_required": tokens_required, "task": task, "normalized": normalized}
    except (ValueError, Kling3KieError) as exc:
        if tokens_required > 0:
            try:
                add_tokens(body.telegram_user_id, tokens_required, reason="kling3_kie_refund", ref_id=request_id, meta={"error": str(exc)})
            except Exception:
                pass
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        if tokens_required > 0:
            try:
                add_tokens(body.telegram_user_id, tokens_required, reason="kling3_kie_refund", ref_id=request_id, meta={"error": str(exc)})
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=f"Internal error: {exc}")


@router.get("/task/{task_id}", summary="Get Kling 3.0 - New task status/result")
async def kling3_kie_task(task_id: str) -> Dict[str, Any]:
    try:
        task = await get_kling3_kie_task(task_id)
        return {"ok": True, "task": task, "normalized": normalize_kling3_kie_task(task if isinstance(task, dict) else {"raw": task})}
    except (ValueError, Kling3KieError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Internal error: {exc}")
