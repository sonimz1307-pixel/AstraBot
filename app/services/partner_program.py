from __future__ import annotations

import os
import random
import re
import string
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional
from uuid import UUID

from db_supabase import supabase

PARTNER_SITE_URL = (os.getenv("PARTNER_SITE_URL") or os.getenv("PUBLIC_SITE_URL") or "https://nabex.ru").strip().rstrip("/")
PARTNER_BOT_USERNAME = (
    os.getenv("PARTNER_BOT_USERNAME")
    or os.getenv("TELEGRAM_BOT_USERNAME")
    or os.getenv("BOT_USERNAME")
    or ""
).strip().lstrip("@")
PARTNER_REF_PREFIX = (os.getenv("PARTNER_REF_PREFIX") or "NAB").strip().upper()[:8] or "NAB"
PARTNER_MIN_PAYOUT_RUB = int(os.getenv("PARTNER_MIN_PAYOUT_RUB", "1000") or "1000")

_REF_RE = re.compile(r"^[A-Z0-9_\-]{3,32}$")
_BASE36_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


class PartnerProgramError(ValueError):
    pass


def _require_supabase():
    if supabase is None:
        raise RuntimeError("Supabase disabled: SUPABASE_URL / SUPABASE_SERVICE_KEY not set")
    return supabase


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _money(value: Any) -> float:
    try:
        d = Decimal(str(value or "0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return float(d)
    except Exception:
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _base36(n: int) -> str:
    n = abs(int(n))
    if n == 0:
        return "0"
    out = []
    while n:
        n, rem = divmod(n, 36)
        out.append(_BASE36_ALPHABET[rem])
    return "".join(reversed(out))


def normalize_ref_code(value: Any) -> str:
    code = str(value or "").strip().upper()
    code = code.replace("ref_", "").replace("REF_", "")
    code = re.sub(r"[^A-Z0-9_\-]", "", code)
    if not code or not _REF_RE.match(code):
        raise PartnerProgramError("Некорректный ref_code.")
    return code


def mask_card(card_number: Any) -> str:
    digits = re.sub(r"\D", "", str(card_number or ""))
    if len(digits) < 8:
        return ""
    return f"{digits[:4]} **** **** {digits[-4:]}"


def _safe_uuid(value: Any) -> str:
    text = str(value or "").strip()
    try:
        return str(UUID(text))
    except Exception:
        raise PartnerProgramError("Некорректный payout_id.")


def _select_one(table: str, **filters: Any) -> Optional[Dict[str, Any]]:
    sb = _require_supabase()
    q = sb.table(table).select("*")
    for key, value in filters.items():
        q = q.eq(key, value)
    res = q.limit(1).execute()
    rows = getattr(res, "data", None) or []
    return rows[0] if rows else None


def _make_ref_code(user_id: int) -> str:
    suffix = "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(3))
    return f"{PARTNER_REF_PREFIX}{_base36(user_id)}{suffix}"[:32]


def ensure_partner_profile(user_id: int) -> Dict[str, Any]:
    sb = _require_supabase()
    uid = int(user_id)
    if uid <= 0:
        raise PartnerProgramError("Некорректный user_id партнёра.")

    row = _select_one("partner_profiles", user_id=uid)
    if row:
        _ensure_partner_balance(uid)
        return row

    for _ in range(12):
        code = _make_ref_code(uid)
        try:
            res = sb.table("partner_profiles").insert(
                {
                    "user_id": uid,
                    "ref_code": code,
                    "status": "active",
                    "created_at": _now_iso(),
                    "updated_at": _now_iso(),
                }
            ).execute()
            row = (getattr(res, "data", None) or [None])[0] or _select_one("partner_profiles", user_id=uid)
            if row:
                _ensure_partner_balance(uid)
                return row
        except Exception:
            existing = _select_one("partner_profiles", user_id=uid)
            if existing:
                _ensure_partner_balance(uid)
                return existing
            continue

    raise PartnerProgramError("Не удалось создать ref_code. Попробуй ещё раз.")


def _ensure_partner_balance(partner_user_id: int) -> Dict[str, Any]:
    sb = _require_supabase()
    uid = int(partner_user_id)
    row = _select_one("partner_balances", partner_user_id=uid)
    if row:
        return row
    try:
        res = sb.table("partner_balances").insert(
            {
                "partner_user_id": uid,
                "earned_total_rub": 0,
                "available_balance_rub": 0,
                "pending_payout_balance_rub": 0,
                "paid_total_rub": 0,
                "updated_at": _now_iso(),
            }
        ).execute()
        return (getattr(res, "data", None) or [None])[0] or _select_one("partner_balances", partner_user_id=uid) or {}
    except Exception:
        return _select_one("partner_balances", partner_user_id=uid) or {}


def _public_profile(row: Dict[str, Any]) -> Dict[str, Any]:
    code = str(row.get("ref_code") or "").strip()
    site_link = f"{PARTNER_SITE_URL}/?ref={code}" if code else ""
    bot_link = f"https://t.me/{PARTNER_BOT_USERNAME}?start=ref_{code}" if code and PARTNER_BOT_USERNAME else ""
    return {
        "user_id": _int(row.get("user_id")),
        "ref_code": code,
        "status": row.get("status") or "active",
        "site_link": site_link,
        "bot_link": bot_link,
        "universal_link": site_link,
        "created_at": row.get("created_at"),
    }


def bind_referral(*, referred_user_id: int, ref_code: Any, source: str = "unknown", meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    sb = _require_supabase()
    referred = int(referred_user_id)
    code = normalize_ref_code(ref_code)
    source_text = str(source or "unknown").strip().lower()[:40] or "unknown"
    if referred <= 0:
        raise PartnerProgramError("Некорректный user_id реферала.")

    partner = _select_one("partner_profiles", ref_code=code)
    if not partner:
        return {"ok": True, "bound": False, "reason": "partner_not_found"}
    if str(partner.get("status") or "active") != "active":
        return {"ok": True, "bound": False, "reason": "partner_not_active"}

    partner_id = int(partner.get("user_id") or 0)
    if partner_id <= 0 or partner_id == referred:
        return {"ok": True, "bound": False, "reason": "self_referral"}

    existing = _select_one("partner_referrals", referred_user_id=referred)
    if existing:
        return {
            "ok": True,
            "bound": int(existing.get("partner_user_id") or 0) == partner_id,
            "already_exists": True,
            "partner_user_id": int(existing.get("partner_user_id") or 0),
        }

    payload = {
        "partner_user_id": partner_id,
        "referred_user_id": referred,
        "ref_code": code,
        "source": source_text,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "meta": meta or {},
    }
    try:
        res = sb.table("partner_referrals").insert(payload).execute()
        row = (getattr(res, "data", None) or [None])[0] or _select_one("partner_referrals", referred_user_id=referred)
        return {"ok": True, "bound": True, "partner_user_id": partner_id, "referral": row}
    except Exception:
        existing = _select_one("partner_referrals", referred_user_id=referred)
        if existing:
            return {"ok": True, "bound": True, "already_exists": True, "partner_user_id": int(existing.get("partner_user_id") or 0)}
        raise


def apply_topup_event(
    *,
    referred_user_id: int,
    source_payment_id: str,
    payment_amount_rub: float,
    purchased_tokens: Optional[int] = None,
    payment_provider: str = "unknown",
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    sb = _require_supabase()
    if int(referred_user_id) <= 0:
        return {"ok": False, "reason": "bad_referred_user_id"}
    if not str(source_payment_id or "").strip():
        return {"ok": False, "reason": "bad_source_payment_id"}
    if float(payment_amount_rub or 0) <= 0:
        return {"ok": False, "reason": "bad_payment_amount"}

    res = sb.rpc(
        "partner_apply_topup_commission",
        {
            "p_referred_user_id": int(referred_user_id),
            "p_source_payment_id": str(source_payment_id).strip(),
            "p_payment_amount_rub": _money(payment_amount_rub),
            "p_purchased_tokens": int(purchased_tokens) if purchased_tokens is not None else None,
            "p_payment_provider": str(payment_provider or "unknown")[:40],
            "p_meta": meta or {},
        },
    ).execute()
    data = getattr(res, "data", None)
    if isinstance(data, dict):
        return data
    return {"ok": True, "data": data}


def _count(table: str, **filters: Any) -> int:
    sb = _require_supabase()
    q = sb.table(table).select("id", count="exact")
    for key, value in filters.items():
        q = q.eq(key, value)
    res = q.execute()
    return int(getattr(res, "count", None) or 0)


def get_partner_dashboard(user_id: int) -> Dict[str, Any]:
    sb = _require_supabase()
    uid = int(user_id)
    profile = ensure_partner_profile(uid)
    balance = _ensure_partner_balance(uid)

    total_referrals = _count("partner_referrals", partner_user_id=uid)
    paid_res = (
        sb.table("partner_referrals")
        .select("id", count="exact")
        .eq("partner_user_id", uid)
        .not_.is_("first_paid_at", "null")
        .execute()
    )
    paid_referrals = int(getattr(paid_res, "count", None) or 0)

    referrals_rows = (
        sb.table("partner_referrals")
        .select("id,referred_user_id,source,first_paid_at,created_at")
        .eq("partner_user_id", uid)
        .order("created_at", desc=True)
        .limit(30)
        .execute()
    )
    commission_rows = (
        sb.table("partner_commissions")
        .select("id,referred_user_id,source_payment_id,payment_provider,payment_amount_rub,purchased_tokens,commission_percent,commission_amount_rub,period_days,status,created_at")
        .eq("partner_user_id", uid)
        .order("created_at", desc=True)
        .limit(30)
        .execute()
    )
    payout_rows = (
        sb.table("partner_payouts")
        .select("id,amount_rub,card_last4,card_holder_name,comment,status,admin_note,created_at,updated_at,paid_at,rejected_at")
        .eq("partner_user_id", uid)
        .order("created_at", desc=True)
        .limit(30)
        .execute()
    )

    referrals = []
    for row in getattr(referrals_rows, "data", None) or []:
        referrals.append(
            {
                "id": row.get("id"),
                "label": f"Реферал #{str(row.get('referred_user_id') or '')[-6:]}",
                "source": row.get("source") or "unknown",
                "created_at": row.get("created_at"),
                "first_paid_at": row.get("first_paid_at"),
                "paid": bool(row.get("first_paid_at")),
            }
        )

    commissions = []
    for row in getattr(commission_rows, "data", None) or []:
        commissions.append(
            {
                "id": row.get("id"),
                "referral_label": f"Реферал #{str(row.get('referred_user_id') or '')[-6:]}",
                "payment_provider": row.get("payment_provider") or "unknown",
                "payment_amount_rub": _money(row.get("payment_amount_rub")),
                "purchased_tokens": row.get("purchased_tokens"),
                "commission_percent": _money(row.get("commission_percent")),
                "commission_amount_rub": _money(row.get("commission_amount_rub")),
                "period_days": _int(row.get("period_days")),
                "status": row.get("status") or "approved",
                "created_at": row.get("created_at"),
            }
        )

    payouts = []
    for row in getattr(payout_rows, "data", None) or []:
        payouts.append(
            {
                "id": row.get("id"),
                "amount_rub": _money(row.get("amount_rub")),
                "card_last4": row.get("card_last4"),
                "card_holder_name": row.get("card_holder_name"),
                "comment": row.get("comment"),
                "status": row.get("status"),
                "admin_note": row.get("admin_note"),
                "created_at": row.get("created_at"),
                "paid_at": row.get("paid_at"),
                "rejected_at": row.get("rejected_at"),
            }
        )

    return {
        "ok": True,
        "profile": _public_profile(profile),
        "stats": {
            "total_referrals": total_referrals,
            "paid_referrals": paid_referrals,
            "earned_total_rub": _money(balance.get("earned_total_rub")),
            "available_balance_rub": _money(balance.get("available_balance_rub")),
            "pending_payout_balance_rub": _money(balance.get("pending_payout_balance_rub")),
            "paid_total_rub": _money(balance.get("paid_total_rub")),
            "min_payout_rub": PARTNER_MIN_PAYOUT_RUB,
        },
        "referrals": referrals,
        "commissions": commissions,
        "payouts": payouts,
    }


def create_partner_payout(*, partner_user_id: int, amount_rub: float, card_number: str, card_holder_name: str, comment: str = "") -> Dict[str, Any]:
    sb = _require_supabase()
    amount = _money(amount_rub)
    if amount < PARTNER_MIN_PAYOUT_RUB:
        raise PartnerProgramError(f"Минимальная сумма вывода — {PARTNER_MIN_PAYOUT_RUB} ₽.")
    res = sb.rpc(
        "partner_create_payout",
        {
            "p_partner_user_id": int(partner_user_id),
            "p_amount_rub": amount,
            "p_card_number": str(card_number or ""),
            "p_card_holder_name": str(card_holder_name or ""),
            "p_comment": str(comment or ""),
        },
    ).execute()
    payout_id = getattr(res, "data", None)
    row = _select_one("partner_payouts", id=str(payout_id)) if payout_id else None
    return {"ok": True, "payout_id": str(payout_id), "payout": serialize_payout(row) if row else None}


def _clean_card_number(card_number: Any) -> str:
    return re.sub(r"\D", "", str(card_number or ""))


def serialize_payout(row: Optional[Dict[str, Any]], *, include_sensitive: bool = False) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    card_number = _clean_card_number(row.get("card_number"))
    item = {
        "id": row.get("id"),
        "partner_user_id": _int(row.get("partner_user_id")),
        "amount_rub": _money(row.get("amount_rub")),
        "card_mask": mask_card(card_number) or (f"**** {row.get('card_last4')}" if row.get("card_last4") else ""),
        "card_last4": row.get("card_last4"),
        "card_holder_name": row.get("card_holder_name"),
        "comment": row.get("comment"),
        "status": row.get("status"),
        "admin_note": row.get("admin_note"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "paid_at": row.get("paid_at"),
        "rejected_at": row.get("rejected_at"),
    }
    if include_sensitive:
        # Only admin endpoints should call serialize_payout(..., include_sensitive=True).
        # User dashboard and payout-created notifications keep only the masked card.
        item["card_number"] = card_number
    return item


def admin_list_payouts(status: str = "pending", limit: int = 100) -> Dict[str, Any]:
    sb = _require_supabase()
    status_text = str(status or "pending").strip().lower()
    lim = max(1, min(int(limit or 100), 300))
    q = sb.table("partner_payouts").select("*").order("created_at", desc=True).limit(lim)
    if status_text and status_text != "all":
        q = q.eq("status", status_text)
    res = q.execute()
    return {"ok": True, "items": [serialize_payout(row, include_sensitive=True) for row in (getattr(res, "data", None) or [])]}


def admin_mark_payout_paid(*, payout_id: str, admin_user_id: Optional[int] = None, admin_note: str = "") -> Dict[str, Any]:
    sb = _require_supabase()
    res = sb.rpc(
        "partner_mark_payout_paid",
        {
            "p_payout_id": _safe_uuid(payout_id),
            "p_admin_user_id": int(admin_user_id) if admin_user_id else None,
            "p_admin_note": str(admin_note or ""),
        },
    ).execute()
    return getattr(res, "data", None) or {"ok": True}


def admin_reject_payout(*, payout_id: str, admin_user_id: Optional[int] = None, admin_note: str = "") -> Dict[str, Any]:
    sb = _require_supabase()
    res = sb.rpc(
        "partner_reject_payout",
        {
            "p_payout_id": _safe_uuid(payout_id),
            "p_admin_user_id": int(admin_user_id) if admin_user_id else None,
            "p_admin_note": str(admin_note or ""),
        },
    ).execute()
    return getattr(res, "data", None) or {"ok": True}


def admin_list_partners(limit: int = 100) -> Dict[str, Any]:
    sb = _require_supabase()
    lim = max(1, min(int(limit or 100), 300))
    rows = (
        sb.table("partner_profiles")
        .select("user_id,ref_code,status,created_at")
        .order("created_at", desc=True)
        .limit(lim)
        .execute()
    )
    items: List[Dict[str, Any]] = []
    for profile in getattr(rows, "data", None) or []:
        uid = _int(profile.get("user_id"))
        balance = _ensure_partner_balance(uid)
        items.append(
            {
                "profile": _public_profile(profile),
                "stats": {
                    "total_referrals": _count("partner_referrals", partner_user_id=uid),
                    "earned_total_rub": _money(balance.get("earned_total_rub")),
                    "available_balance_rub": _money(balance.get("available_balance_rub")),
                    "pending_payout_balance_rub": _money(balance.get("pending_payout_balance_rub")),
                    "paid_total_rub": _money(balance.get("paid_total_rub")),
                },
            }
        )
    return {"ok": True, "items": items}
