from __future__ import annotations

import os
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.services.workspace_auth import get_current_workspace_user
from app.services.partner_program import (
    PartnerProgramError,
    admin_list_partners,
    admin_list_payouts,
    admin_mark_payout_paid,
    admin_reject_payout,
    bind_referral,
    create_partner_payout,
    get_partner_dashboard,
)
from queue_redis import enqueue_job

router = APIRouter(prefix="/api/partner", tags=["partner-program"])

PARTNER_EVENTS_QUEUE_NAME = (os.getenv("PARTNER_EVENTS_QUEUE_NAME", "partner_events") or "partner_events").strip() or "partner_events"


class ReferralBindPayload(BaseModel):
    ref_code: str = Field(..., min_length=3, max_length=64)
    source: str = Field("site", max_length=40)


class PayoutCreatePayload(BaseModel):
    amount_rub: float = Field(..., gt=0)
    card_number: str = Field(..., min_length=12, max_length=32)
    card_holder_name: str = Field(..., min_length=5, max_length=160)
    comment: Optional[str] = Field(None, max_length=500)


class AdminPayoutActionPayload(BaseModel):
    admin_user_id: Optional[int] = None
    admin_note: Optional[str] = Field(None, max_length=500)


def _uid_from_user(user: Dict[str, Any]) -> int:
    uid = int(user.get("workspace_user_id") or user.get("telegram_user_id") or user.get("id") or 0)
    if uid <= 0:
        raise HTTPException(status_code=401, detail="Не удалось определить пользователя.")
    return uid


def _require_admin(x_admin_token: Optional[str]) -> None:
    expected = (os.getenv("ADMIN_TOKEN") or "").strip()
    if not expected:
        raise HTTPException(status_code=500, detail="ADMIN_TOKEN is not configured")
    if not x_admin_token or x_admin_token.strip() != expected:
        raise HTTPException(status_code=403, detail="forbidden")


async def _notify_payout_created(payout: Optional[Dict[str, Any]]) -> None:
    if not payout:
        return
    try:
        await enqueue_job(
            {
                "job_id": f"partner_payout_notify_{uuid4().hex}",
                "kind": "partner_payout_created",
                "payout": payout,
            },
            queue_name=PARTNER_EVENTS_QUEUE_NAME,
        )
    except Exception:
        # Notification must not break the payout request.
        pass


@router.get("/me")
async def partner_me(user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    try:
        return get_partner_dashboard(_uid_from_user(user))
    except PartnerProgramError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Partner dashboard error: {e}")


@router.post("/referral/bind")
async def partner_bind_referral(payload: ReferralBindPayload, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    try:
        return bind_referral(
            referred_user_id=_uid_from_user(user),
            ref_code=payload.ref_code,
            source=payload.source or "site",
            meta={"origin": "workspace_api"},
        )
    except PartnerProgramError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Referral bind error: {e}")


@router.post("/payouts")
async def partner_create_payout(payload: PayoutCreatePayload, user: Dict[str, Any] = Depends(get_current_workspace_user)) -> Dict[str, Any]:
    try:
        out = create_partner_payout(
            partner_user_id=_uid_from_user(user),
            amount_rub=payload.amount_rub,
            card_number=payload.card_number,
            card_holder_name=payload.card_holder_name,
            comment=payload.comment or "",
        )
        await _notify_payout_created(out.get("payout"))
        return out
    except PartnerProgramError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        message = str(e)
        if "min_payout" in message:
            raise HTTPException(status_code=400, detail="Минимальная сумма вывода — 1000 ₽.")
        if "insufficient_partner_balance" in message:
            raise HTTPException(status_code=400, detail="Недостаточно средств для вывода.")
        if "bad_card" in message:
            raise HTTPException(status_code=400, detail="Проверь номер карты и ФИО.")
        raise HTTPException(status_code=500, detail=f"Payout create error: {e}")


@router.get("/admin/payouts")
async def partner_admin_payouts(
    status: str = Query("pending", max_length=40),
    limit: int = Query(100, ge=1, le=300),
    x_admin_token: Optional[str] = Header(None),
) -> Dict[str, Any]:
    _require_admin(x_admin_token)
    try:
        return admin_list_payouts(status=status, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Admin payouts error: {e}")


@router.post("/admin/payouts/{payout_id}/paid")
async def partner_admin_payout_paid(
    payout_id: str,
    payload: AdminPayoutActionPayload,
    x_admin_token: Optional[str] = Header(None),
) -> Dict[str, Any]:
    _require_admin(x_admin_token)
    try:
        return admin_mark_payout_paid(
            payout_id=payout_id,
            admin_user_id=payload.admin_user_id,
            admin_note=payload.admin_note or "",
        )
    except PartnerProgramError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Mark payout paid error: {e}")


@router.post("/admin/payouts/{payout_id}/reject")
async def partner_admin_payout_reject(
    payout_id: str,
    payload: AdminPayoutActionPayload,
    x_admin_token: Optional[str] = Header(None),
) -> Dict[str, Any]:
    _require_admin(x_admin_token)
    try:
        return admin_reject_payout(
            payout_id=payout_id,
            admin_user_id=payload.admin_user_id,
            admin_note=payload.admin_note or "",
        )
    except PartnerProgramError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Reject payout error: {e}")


@router.get("/admin/partners")
async def partner_admin_partners(
    limit: int = Query(100, ge=1, le=300),
    x_admin_token: Optional[str] = Header(None),
) -> Dict[str, Any]:
    _require_admin(x_admin_token)
    try:
        return admin_list_partners(limit=limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Admin partners error: {e}")
