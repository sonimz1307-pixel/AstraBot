from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Any, Dict, Optional

from kling3_flow import create_kling3_task, get_kling3_task, Kling3Error
from kling3_pricing import calculate_kling3_price

router = APIRouter()


@router.get("/health", summary="Kling 3.0 healthcheck")
async def kling3_health() -> Dict[str, Any]:
    return {"ok": True, "service": "kling3"}


class Kling3CreateBody(BaseModel):
    prompt: str = Field(..., min_length=1)
    duration: int = Field(..., ge=3, le=15)
    resolution: str = Field(..., pattern="^(720|1080)$")
    enable_audio: bool = False
    aspect_ratio: str = Field(default="16:9", pattern="^(16:9|9:16|1:1)$")


@router.post("/create", summary="Create Kling 3.0 task")
async def kling3_create(body: Kling3CreateBody) -> Dict[str, Any]:
    try:
        tokens_required = calculate_kling3_price(
            resolution=body.resolution,
            enable_audio=body.enable_audio,
            duration=body.duration,
        )

        task = await create_kling3_task(
            prompt=body.prompt,
            duration=body.duration,
            resolution=body.resolution,
            enable_audio=body.enable_audio,
            aspect_ratio=body.aspect_ratio,
        )

        return {"ok": True, "tokens_required": tokens_required, "task": task}

    except (ValueError, Kling3Error) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal error: {e}")


@router.get("/task/{task_id}", summary="Get Kling 3.0 task status/result")
async def kling3_task(task_id: str) -> Dict[str, Any]:
    try:
        task = await get_kling3_task(task_id)
        return {"ok": True, "task": task}
    except (ValueError, Kling3Error) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal error: {e}")
