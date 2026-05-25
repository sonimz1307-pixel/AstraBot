import os
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query

from app.services.provider_balances import get_provider_balances

router = APIRouter()


def _require_admin(x_admin_token: Optional[str]) -> None:
    expected = os.getenv("ADMIN_TOKEN")
    if not expected:
        raise HTTPException(status_code=500, detail="ADMIN_TOKEN is not configured")
    if not x_admin_token or x_admin_token != expected:
        raise HTTPException(status_code=403, detail="forbidden")


@router.get("/provider-balances")
async def provider_balances(
    force: bool = Query(default=False),
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    _require_admin(x_admin_token)
    return await get_provider_balances(force=force)
