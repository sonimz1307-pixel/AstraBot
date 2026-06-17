import os
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request

from app.services.provider_balances import get_provider_balances
from app.services.admin_auth import require_admin_request

router = APIRouter()


def _require_admin(request: Request, x_admin_token: Optional[str]) -> None:
    require_admin_request(request, x_admin_token)


@router.get("/provider-balances")
async def provider_balances(
    request: Request,
    force: bool = Query(default=False),
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    _require_admin(request, x_admin_token)
    return await get_provider_balances(force=force)
