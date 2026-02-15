from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Any, Dict
from uuid import uuid4

from kling3_flow import create_kling3_task, get_kling3_task, Kling3Error
from kling3_pricing import calculate_kling3_price

# Billing (Supabase ledger)
from billing_db import add_tokens

router = APIRouter()


@router.get("/health", summary="Kling 3.0 healthcheck")
async def kling3_health() -> Dict[str, Any]:
    return {"ok": True, "service": "kling3"}


class Kling3CreateBody(BaseModel):
    telegram_user_id: int = Field(..., ge=1)

    prompt: str = Field(..., min_length=1)
    duration: int = Field(..., ge=3, le=15)
    resolution: str = Field(..., pattern="^(720|1080)$")
    enable_audio: bool = False
    aspect_ratio: str = Field(default="16:9", pattern="^(16:9|9:16|1:1)$")


@router.post("/create", summary="Create Kling 3.0 task (with token charge)")
async def kling3_create(body: Kling3CreateBody) -> Dict[str, Any]:
    request_id = str(uuid4())
    tokens_required = 0

    try:
        # 1) Calculate tokens (server-side source of truth)
        tokens_required = calculate_kling3_price(
            resolution=body.resolution,
            enable_audio=body.enable_audio,
            duration=body.duration,
        )

        # 2) Charge BEFORE sending to provider (avoid provider spend on insufficient balance)
        add_tokens(
            body.telegram_user_id,
            -tokens_required,
            reason="kling3_create",
            ref_id=request_id,
            meta={
                "duration": body.duration,
                "resolution": body.resolution,
                "enable_audio": body.enable_audio,
                "aspect_ratio": body.aspect_ratio,
            },
        )

        # 3) Create provider task
        task = await create_kling3_task(
            prompt=body.prompt,
            duration=body.duration,
            resolution=body.resolution,
            enable_audio=body.enable_audio,
            aspect_ratio=body.aspect_ratio,
        )

        # Try to extract provider task_id (PiAPI usually returns data.task_id)
        provider_task_id = None
        if isinstance(task, dict):
            provider_task_id = (task.get("data") or {}).get("task_id") or task.get("task_id")

        return {
            "ok": True,
            "request_id": request_id,
            "tokens_required": tokens_required,
            "provider_task_id": provider_task_id,
            "task": task,
        }

    except (ValueError, Kling3Error) as e:
        # Refund if we already charged
        if tokens_required > 0:
            try:
                add_tokens(
                    body.telegram_user_id,
                    tokens_required,
                    reason="kling3_refund",
                    ref_id=request_id,
                    meta={"error": str(e)},
                )
            except Exception:
                pass
        raise HTTPException(status_code=400, detail=str(e))

    except Exception as e:
        # Refund on unexpected errors too
        if tokens_required > 0:
            try:
                add_tokens(
                    body.telegram_user_id,
                    tokens_required,
                    reason="kling3_refund",
                    ref_id=request_id,
                    meta={"error": str(e)},
                )
            except Exception:
                pass
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
