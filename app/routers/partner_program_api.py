from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
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
    serialize_payout,
)
from queue_redis import enqueue_job

from billing_db import add_tokens, get_balance, ledger_ref_exists
from db_supabase import supabase

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

# ==========================================================
# Nabex Admin Console MVP
# - search users
# - balance and token ledger
# - manual token +/- with ledger
# - generation history and admin fail/refund actions
# - partner payouts remain handled by existing endpoints above
# ==========================================================

_ADMIN_GENERATION_TABLES: Dict[str, str] = {
    "video": "workspace_video_generations",
    "image": "workspace_image_generations",
    "music": "workspace_music_generations",
    "voice": "workspace_voice_generations",
}

_PAYMENT_REASON_RE = re.compile(r"(topup|payment|yookassa|stars|плат|pay)", re.I)


class AdminTokenAdjustPayload(BaseModel):
    delta_tokens: int = Field(..., description="Positive to add, negative to subtract")
    reason: Optional[str] = Field(None, max_length=120)
    comment: Optional[str] = Field(None, max_length=1000)
    ref_id: Optional[str] = Field(None, max_length=120)


class AdminGenerationFailPayload(BaseModel):
    refund_tokens: int = Field(0, ge=0, le=100000)
    admin_note: Optional[str] = Field(None, max_length=1000)


def _admin_sb():
    if supabase is None:
        raise RuntimeError("Supabase disabled: SUPABASE_URL / SUPABASE_SERVICE_KEY not set")
    return supabase


def _admin_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _safe_str(value: Any, limit: int = 500) -> str:
    return str(value or "").strip()[:limit]


def _try_rows(table: str, select: str = "*", limit: int = 50, **filters: Any) -> List[Dict[str, Any]]:
    sb = _admin_sb()
    try:
        q = sb.table(table).select(select)
        for key, value in filters.items():
            q = q.eq(key, value)
        res = q.limit(max(1, min(int(limit or 50), 500))).execute()
        return list(getattr(res, "data", None) or [])
    except Exception:
        return []


def _try_one(table: str, select: str = "*", **filters: Any) -> Optional[Dict[str, Any]]:
    rows = _try_rows(table, select=select, limit=1, **filters)
    return rows[0] if rows else None


def _try_ordered_rows(
    table: str,
    *,
    select: str = "*",
    order: str = "created_at",
    desc: bool = True,
    limit: int = 50,
    **filters: Any,
) -> List[Dict[str, Any]]:
    sb = _admin_sb()
    try:
        q = sb.table(table).select(select)
        for key, value in filters.items():
            q = q.eq(key, value)
        res = q.order(order, desc=desc).limit(max(1, min(int(limit or 50), 500))).execute()
        return list(getattr(res, "data", None) or [])
    except Exception:
        try:
            q = sb.table(table).select(select)
            for key, value in filters.items():
                q = q.eq(key, value)
            res = q.limit(max(1, min(int(limit or 50), 500))).execute()
            rows = list(getattr(res, "data", None) or [])
            rows.sort(key=lambda r: str((r or {}).get(order) or ""), reverse=bool(desc))
            return rows
        except Exception:
            return []


def _admin_log_action(
    *,
    action: str,
    target_user_id: Optional[int] = None,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    before: Optional[Dict[str, Any]] = None,
    after: Optional[Dict[str, Any]] = None,
) -> None:
    # Optional table. If it does not exist, admin API must still work.
    try:
        _admin_sb().table("admin_actions_log").insert(
            {
                "action": _safe_str(action, 120),
                "target_user_id": int(target_user_id) if target_user_id else None,
                "target_type": _safe_str(target_type, 80) or None,
                "target_id": _safe_str(target_id, 160) or None,
                "payload": payload or {},
                "before_json": before or {},
                "after_json": after or {},
                "created_at": _admin_now_iso(),
            }
        ).execute()
    except Exception:
        pass


def _workspace_account_public(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    return {
        "id": _safe_int(row.get("id")),
        "email": row.get("email"),
        "email_verified": bool(row.get("email_verified", False)),
        "telegram_user_id": row.get("telegram_user_id"),
        "username": row.get("username"),
        "first_name": row.get("first_name"),
        "last_name": row.get("last_name"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "last_login_at": row.get("last_login_at"),
    }


def _bot_user_public(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    return {
        "telegram_user_id": row.get("telegram_user_id"),
        "username": row.get("username"),
        "first_name": row.get("first_name"),
        "last_name": row.get("last_name"),
        "language_code": row.get("language_code"),
        "is_premium": row.get("is_premium"),
        "last_seen_at": row.get("last_seen_at"),
        "updated_at": row.get("updated_at"),
    }


def _resolve_admin_user_ids(user_id: int) -> Dict[str, Any]:
    uid = int(user_id)
    account = _try_one(
        "workspace_accounts",
        "id,email,email_verified,telegram_user_id,username,first_name,last_name,created_at,updated_at,last_login_at",
        id=uid,
    )
    if not account:
        account = _try_one(
            "workspace_accounts",
            "id,email,email_verified,telegram_user_id,username,first_name,last_name,created_at,updated_at,last_login_at",
            telegram_user_id=uid,
        )
    effective_id = int((account or {}).get("id") or uid)
    linked_tg = (account or {}).get("telegram_user_id")
    ids = [effective_id]
    if linked_tg not in (None, ""):
        try:
            tg_int = int(linked_tg)
            if tg_int not in ids:
                ids.append(tg_int)
        except Exception:
            pass
    if uid not in ids:
        ids.append(uid)
    return {"effective_user_id": effective_id, "linked_ids": ids, "account": account}


def _balance_row(user_id: int) -> Dict[str, Any]:
    row = _try_one("bot_user_balance", "telegram_user_id,balance_tokens,updated_at", telegram_user_id=int(user_id)) or {}
    if not row:
        try:
            return {"telegram_user_id": int(user_id), "balance_tokens": int(get_balance(int(user_id)) or 0), "updated_at": None}
        except Exception:
            return {"telegram_user_id": int(user_id), "balance_tokens": 0, "updated_at": None}
    return row


def _ledger_rows_for_ids(user_ids: List[int], *, limit: int) -> List[Dict[str, Any]]:
    all_rows: List[Dict[str, Any]] = []
    for uid in user_ids:
        rows = _try_ordered_rows(
            "bot_balance_ledger",
            select="id,telegram_user_id,delta_tokens,reason,ref_id,meta,created_at",
            order="created_at",
            desc=True,
            limit=limit,
            telegram_user_id=int(uid),
        )
        for row in rows:
            row["ledger_user_id"] = int(uid)
            all_rows.append(row)
    all_rows.sort(key=lambda r: str((r or {}).get("created_at") or ""), reverse=True)
    return all_rows[: max(1, min(int(limit or 50), 200))]


def _serialize_ledger(rows: List[Dict[str, Any]], *, current_balance: int) -> List[Dict[str, Any]]:
    # Estimate visible running balance for the primary/effective balance only.
    running_after = int(current_balance or 0)
    out: List[Dict[str, Any]] = []
    for row in rows:
        delta = _safe_int(row.get("delta_tokens"))
        meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
        item = {
            "id": row.get("id"),
            "ledger_user_id": row.get("ledger_user_id") or row.get("telegram_user_id"),
            "telegram_user_id": row.get("telegram_user_id"),
            "delta_tokens": delta,
            "reason": row.get("reason"),
            "ref_id": row.get("ref_id"),
            "meta": meta,
            "created_at": row.get("created_at"),
            "balance_after_estimated": running_after,
            "balance_before_estimated": running_after - delta,
        }
        running_after -= delta
        out.append(item)
    return out


def _payment_like_rows(ledger_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for row in ledger_items:
        reason = str(row.get("reason") or "")
        meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
        if int(row.get("delta_tokens") or 0) > 0 and (_PAYMENT_REASON_RE.search(reason) or meta.get("payment_id") or meta.get("charge_id")):
            items.append(row)
    return items[:50]


def _generation_rows_for_user(user_id: int, *, limit: int = 20) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for kind, table in _ADMIN_GENERATION_TABLES.items():
        rows = _try_ordered_rows(table, select="*", order="created_at", desc=True, limit=limit, user_id=str(user_id))
        # Some schemas store user_id as bigint; PostgREST usually casts, but fallback just in case.
        if not rows:
            rows = _try_ordered_rows(table, select="*", order="created_at", desc=True, limit=limit, user_id=int(user_id))
        for row in rows:
            meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
            output_url = (
                row.get("provider_video_url")
                or row.get("video_url")
                or row.get("audio_url")
                or row.get("download_url")
                or row.get("public_url")
                or row.get("output_url")
                or row.get("image_url")
            )
            if not output_url and isinstance(row.get("image_urls_json"), list) and row.get("image_urls_json"):
                output_url = row.get("image_urls_json")[0]
            items.append(
                {
                    "kind": kind,
                    "table": table,
                    "id": row.get("id"),
                    "user_id": row.get("user_id"),
                    "provider": row.get("provider"),
                    "model": row.get("model"),
                    "mode": row.get("mode"),
                    "status": row.get("status"),
                    "task_id": row.get("task_id") or row.get("provider_task_id"),
                    "prompt": row.get("prompt") or row.get("text") or row.get("title") or row.get("idea_text"),
                    "cost_tokens": row.get("tokens_cost") or row.get("cost_tokens") or meta.get("tokens_cost") or meta.get("cost_tokens"),
                    "output_url": output_url,
                    "error_code": row.get("error_code"),
                    "error_message": row.get("error_message"),
                    "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at"),
                    "completed_at": row.get("completed_at"),
                }
            )
    items.sort(key=lambda r: str((r or {}).get("created_at") or ""), reverse=True)
    return items[: max(1, min(int(limit or 20), 100))]


def _partner_info_for_user(user_id: int) -> Dict[str, Any]:
    profile = _try_one("partner_profiles", "user_id,ref_code,status,created_at,updated_at", user_id=int(user_id))
    balance = _try_one(
        "partner_balances",
        "partner_user_id,earned_total_rub,available_balance_rub,pending_payout_balance_rub,paid_total_rub,updated_at",
        partner_user_id=int(user_id),
    )
    referred_by = _try_one("partner_referrals", "id,partner_user_id,ref_code,source,first_paid_at,created_at", referred_user_id=int(user_id))
    return {"profile": profile, "balance": balance, "referred_by": referred_by}


def _user_search_candidate(*, user_id: int, source: str, account: Optional[Dict[str, Any]] = None, bot_user: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    resolved = _resolve_admin_user_ids(int(user_id))
    account = account or resolved.get("account")
    effective_id = int(resolved.get("effective_user_id") or user_id)
    linked_tg = (account or {}).get("telegram_user_id")
    if not bot_user and linked_tg not in (None, ""):
        bot_user = _try_one("bot_users", "telegram_user_id,username,first_name,last_name,language_code,is_premium,last_seen_at,updated_at", telegram_user_id=int(linked_tg))
    if not bot_user:
        bot_user = _try_one("bot_users", "telegram_user_id,username,first_name,last_name,language_code,is_premium,last_seen_at,updated_at", telegram_user_id=int(user_id))
    bal = _balance_row(effective_id)
    partner = _partner_info_for_user(effective_id)
    return {
        "source": source,
        "user_id": effective_id,
        "input_user_id": int(user_id),
        "linked_ids": resolved.get("linked_ids") or [effective_id],
        "workspace_account": _workspace_account_public(account),
        "bot_user": _bot_user_public(bot_user),
        "balance_tokens": _safe_int(bal.get("balance_tokens")),
        "partner_ref_code": (partner.get("profile") or {}).get("ref_code"),
        "is_partner": bool(partner.get("profile")),
    }


def _dedupe_candidates(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out = []
    for item in items:
        key = int(item.get("user_id") or 0)
        if key <= 0 or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out[:50]


@router.get("/admin/users/search")
async def partner_admin_user_search(
    q: str = Query(..., min_length=1, max_length=120),
    limit: int = Query(20, ge=1, le=50),
    x_admin_token: Optional[str] = Header(None),
) -> Dict[str, Any]:
    _require_admin(x_admin_token)
    query = str(q or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="Введите поисковый запрос.")
    candidates: List[Dict[str, Any]] = []
    numeric = int(query) if query.isdigit() else None

    if numeric:
        account = _try_one("workspace_accounts", "id,email,email_verified,telegram_user_id,username,first_name,last_name,created_at,updated_at,last_login_at", id=numeric)
        if account:
            candidates.append(_user_search_candidate(user_id=int(account["id"]), source="workspace_accounts.id", account=account))
        account_by_tg = _try_one("workspace_accounts", "id,email,email_verified,telegram_user_id,username,first_name,last_name,created_at,updated_at,last_login_at", telegram_user_id=numeric)
        if account_by_tg:
            candidates.append(_user_search_candidate(user_id=int(account_by_tg["id"]), source="workspace_accounts.telegram_user_id", account=account_by_tg))
        bot_user = _try_one("bot_users", "telegram_user_id,username,first_name,last_name,language_code,is_premium,last_seen_at,updated_at", telegram_user_id=numeric)
        if bot_user:
            candidates.append(_user_search_candidate(user_id=numeric, source="bot_users.telegram_user_id", bot_user=bot_user))
        bal = _try_one("bot_user_balance", "telegram_user_id,balance_tokens,updated_at", telegram_user_id=numeric)
        if bal:
            candidates.append(_user_search_candidate(user_id=numeric, source="bot_user_balance.telegram_user_id"))

    # email / username search in workspace accounts.
    if "@" in query:
        for row in _try_rows("workspace_accounts", "id,email,email_verified,telegram_user_id,username,first_name,last_name,created_at,updated_at,last_login_at", limit=limit, email=query.lower()):
            candidates.append(_user_search_candidate(user_id=int(row["id"]), source="workspace_accounts.email", account=row))
        try:
            res = _admin_sb().table("workspace_accounts").select("id,email,email_verified,telegram_user_id,username,first_name,last_name,created_at,updated_at,last_login_at").ilike("email", f"%{query.lower()}%").limit(limit).execute()
            for row in (getattr(res, "data", None) or []):
                candidates.append(_user_search_candidate(user_id=int(row["id"]), source="workspace_accounts.email_like", account=row))
        except Exception:
            pass
    else:
        like = f"%{query.lstrip('@')}%"
        try:
            res = _admin_sb().table("workspace_accounts").select("id,email,email_verified,telegram_user_id,username,first_name,last_name,created_at,updated_at,last_login_at").ilike("username", like).limit(limit).execute()
            for row in (getattr(res, "data", None) or []):
                candidates.append(_user_search_candidate(user_id=int(row["id"]), source="workspace_accounts.username", account=row))
        except Exception:
            pass
        try:
            res = _admin_sb().table("bot_users").select("telegram_user_id,username,first_name,last_name,language_code,is_premium,last_seen_at,updated_at").ilike("username", like).limit(limit).execute()
            for row in (getattr(res, "data", None) or []):
                candidates.append(_user_search_candidate(user_id=int(row["telegram_user_id"]), source="bot_users.username", bot_user=row))
        except Exception:
            pass

    # Partner ref-code search.
    try:
        res = _admin_sb().table("partner_profiles").select("user_id,ref_code,status,created_at,updated_at").ilike("ref_code", f"%{query.upper()}%").limit(limit).execute()
        for row in (getattr(res, "data", None) or []):
            candidates.append(_user_search_candidate(user_id=int(row["user_id"]), source="partner_profiles.ref_code"))
    except Exception:
        pass

    return {"ok": True, "items": _dedupe_candidates(candidates)}


@router.get("/admin/users/{user_id}/overview")
async def partner_admin_user_overview(
    user_id: int,
    ledger_limit: int = Query(80, ge=1, le=200),
    generation_limit: int = Query(40, ge=1, le=100),
    x_admin_token: Optional[str] = Header(None),
) -> Dict[str, Any]:
    _require_admin(x_admin_token)
    resolved = _resolve_admin_user_ids(int(user_id))
    effective_id = int(resolved.get("effective_user_id") or user_id)
    linked_ids = [int(x) for x in (resolved.get("linked_ids") or [effective_id])]
    account = resolved.get("account")
    linked_tg = (account or {}).get("telegram_user_id")
    bot_user = None
    if linked_tg not in (None, ""):
        bot_user = _try_one("bot_users", "telegram_user_id,username,first_name,last_name,language_code,is_premium,last_seen_at,updated_at", telegram_user_id=int(linked_tg))
    if not bot_user:
        bot_user = _try_one("bot_users", "telegram_user_id,username,first_name,last_name,language_code,is_premium,last_seen_at,updated_at", telegram_user_id=int(user_id))
    balance = _balance_row(effective_id)
    old_balances = [_balance_row(uid) for uid in linked_ids if uid != effective_id]
    ledger_rows = _ledger_rows_for_ids(linked_ids, limit=ledger_limit)
    ledger_items = _serialize_ledger(ledger_rows, current_balance=_safe_int(balance.get("balance_tokens")))
    generations: List[Dict[str, Any]] = []
    seen_generations = set()
    for gen_uid in linked_ids:
        for gen_item in _generation_rows_for_user(int(gen_uid), limit=generation_limit):
            gen_key = (str(gen_item.get("table") or ""), str(gen_item.get("id") or ""))
            if gen_key in seen_generations:
                continue
            seen_generations.add(gen_key)
            generations.append(gen_item)
    generations.sort(key=lambda r: str((r or {}).get("created_at") or ""), reverse=True)
    generations = generations[: max(1, min(int(generation_limit or 40), 100))]
    partner = _partner_info_for_user(effective_id)
    payouts = _try_ordered_rows("partner_payouts", select="*", order="created_at", desc=True, limit=30, partner_user_id=effective_id)
    commissions = _try_ordered_rows("partner_commissions", select="*", order="created_at", desc=True, limit=30, partner_user_id=effective_id)
    return {
        "ok": True,
        "user": {
            "user_id": effective_id,
            "input_user_id": int(user_id),
            "linked_ids": linked_ids,
            "workspace_account": _workspace_account_public(account),
            "bot_user": _bot_user_public(bot_user),
        },
        "balance": {
            "primary": balance,
            "old_or_linked_balances": old_balances,
        },
        "ledger": ledger_items,
        "payments": _payment_like_rows(ledger_items),
        "generations": generations,
        "partner": partner,
        "partner_payouts": [serialize_payout(row, include_sensitive=True) for row in payouts],
        "partner_commissions": commissions,
    }


@router.post("/admin/users/{user_id}/tokens/adjust")
async def partner_admin_adjust_tokens(
    user_id: int,
    payload: AdminTokenAdjustPayload,
    x_admin_token: Optional[str] = Header(None),
) -> Dict[str, Any]:
    _require_admin(x_admin_token)
    resolved = _resolve_admin_user_ids(int(user_id))
    effective_id = int(resolved.get("effective_user_id") or user_id)
    delta = int(payload.delta_tokens)
    if delta == 0:
        raise HTTPException(status_code=400, detail="delta_tokens cannot be 0")
    before_balance = _safe_int(_balance_row(effective_id).get("balance_tokens"))
    reason_text = _safe_str(payload.reason or ("admin_manual_add" if delta > 0 else "admin_manual_subtract"), 80)
    if not reason_text.startswith("admin_"):
        reason_text = f"admin_{reason_text}"[:80]
    meta = {
        "origin": "nabex_admin_panel",
        "comment": _safe_str(payload.comment, 1000),
        "input_user_id": int(user_id),
        "effective_user_id": effective_id,
    }
    try:
        ledger_id = add_tokens(
            effective_id,
            delta,
            reason=reason_text,
            ref_id=_safe_str(payload.ref_id, 120) or None,
            meta=meta,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Не удалось изменить баланс: {e}")
    after_balance = _safe_int(_balance_row(effective_id).get("balance_tokens"))
    _admin_log_action(
        action="tokens_adjust",
        target_user_id=effective_id,
        target_type="user",
        target_id=str(effective_id),
        payload={"delta_tokens": delta, "reason": reason_text, "comment": payload.comment, "ref_id": payload.ref_id},
        before={"balance_tokens": before_balance},
        after={"balance_tokens": after_balance, "ledger_id": ledger_id},
    )
    return {
        "ok": True,
        "user_id": effective_id,
        "delta_tokens": delta,
        "before_balance": before_balance,
        "after_balance": after_balance,
        "ledger_id": ledger_id,
    }


@router.post("/admin/generations/{kind}/{generation_id}/mark-failed")
async def partner_admin_generation_mark_failed(
    kind: str,
    generation_id: str,
    payload: AdminGenerationFailPayload,
    x_admin_token: Optional[str] = Header(None),
) -> Dict[str, Any]:
    _require_admin(x_admin_token)
    kind_key = str(kind or "").strip().lower()
    table = _ADMIN_GENERATION_TABLES.get(kind_key)
    if not table:
        raise HTTPException(status_code=400, detail="Unknown generation kind")
    gen_id = _safe_str(generation_id, 120)
    if not gen_id:
        raise HTTPException(status_code=400, detail="generation_id is required")
    before = _try_one(table, "*", id=gen_id)
    if not before:
        raise HTTPException(status_code=404, detail="Generation not found")
    user_id = _safe_int(before.get("user_id"))
    note = _safe_str(payload.admin_note or "Сброшено админом", 1000)
    patch_full = {
        "status": "failed",
        "error_code": "admin_mark_failed",
        "error_message": note,
        "updated_at": _admin_now_iso(),
        "completed_at": _admin_now_iso(),
    }
    try:
        _admin_sb().table(table).update(patch_full).eq("id", gen_id).execute()
    except Exception:
        try:
            _admin_sb().table(table).update({"status": "failed", "error_message": note, "updated_at": _admin_now_iso()}).eq("id", gen_id).execute()
        except Exception:
            _admin_sb().table(table).update({"status": "failed"}).eq("id", gen_id).execute()
    after = _try_one(table, "*", id=gen_id) or {}
    refund_ledger_id = None
    if int(payload.refund_tokens or 0) > 0:
        try:
            if ledger_ref_exists(reason="admin_generation_refund", ref_id=gen_id):
                refund_ledger_id = "already_refunded"
            else:
                refund_ledger_id = add_tokens(
                    user_id,
                    int(payload.refund_tokens),
                    reason="admin_generation_refund",
                    ref_id=gen_id,
                    meta={"origin": "nabex_admin_panel", "generation_kind": kind_key, "comment": note},
                )
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Статус сброшен, но возврат токенов не прошёл: {e}")
    _admin_log_action(
        action="generation_mark_failed",
        target_user_id=user_id,
        target_type=f"generation:{kind_key}",
        target_id=gen_id,
        payload={"refund_tokens": int(payload.refund_tokens or 0), "admin_note": note},
        before=before,
        after=after,
    )
    return {"ok": True, "kind": kind_key, "generation_id": gen_id, "user_id": user_id, "refund_ledger_id": refund_ledger_id}

