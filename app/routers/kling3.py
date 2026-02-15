from fastapi import APIRouter, HTTPException
from typing import Any, Dict

# Kling 3.0 (PiAPI) router.
# Keep main.py thin: main should only include this router.
#
# NOTE: endpoints are intentionally minimal for Step-by-step integration.

router = APIRouter()


@router.get("/health", summary="Kling 3.0 healthcheck")
async def kling3_health() -> Dict[str, Any]:
    return {"ok": True, "service": "kling3"}


# дальнейшие эндпоинты добавим в следующих шагах (create_task / get_task / webhook)
