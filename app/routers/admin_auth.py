from __future__ import annotations

from typing import Dict

from fastapi import APIRouter, Body, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.services.admin_auth import (
    ADMIN_SESSION_TTL_SEC,
    audit_admin_action,
    clear_admin_session_cookie,
    has_valid_admin_session,
    set_admin_session_cookie,
    verify_admin_token,
)

router = APIRouter(prefix="/api/admin-auth", tags=["admin-auth"])


class AdminLoginPayload(BaseModel):
    token: str = Field(..., min_length=1, max_length=4096)


@router.post("/login")
async def admin_login(payload: AdminLoginPayload, request: Request, response: Response) -> Dict[str, object]:
    if not verify_admin_token(payload.token):
        audit_admin_action(request, "login_failed")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    ttl = set_admin_session_cookie(response)
    audit_admin_action(request, "login_ok")
    return {"ok": True, "expires_in": ttl}


@router.post("/logout")
async def admin_logout(request: Request, response: Response) -> Dict[str, object]:
    clear_admin_session_cookie(response)
    audit_admin_action(request, "logout")
    return {"ok": True}


@router.get("/me")
async def admin_me(request: Request) -> Dict[str, object]:
    if not has_valid_admin_session(request):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="admin session required")
    return {"ok": True, "expires_in": ADMIN_SESSION_TTL_SEC}
