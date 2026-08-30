from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from billing_db import resolve_billing_user_id, supabase

YOOKASSA_INTENTS_TABLE = "yookassa_payment_intents"
YOOKASSA_CLAIM_RPC = "nabex_claim_yookassa_payment"
YOOKASSA_CREDIT_RPC = "nabex_credit_tokens_once"
YOOKASSA_SUBSCRIPTION_LOCK_CLAIM_RPC = "nabex_claim_yookassa_subscription_user"
YOOKASSA_SUBSCRIPTION_LOCK_RELEASE_RPC = "nabex_release_yookassa_subscription_user"
YOOKASSA_SUBSCRIPTION_APPLY_RPC = "nabex_apply_yookassa_subscription_once"


def _require_client():
    if supabase is None:
        raise RuntimeError("Supabase disabled: cannot persist YooKassa payment recovery state")
    return supabase


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rows(value: Any) -> List[Dict[str, Any]]:
    data = getattr(value, "data", value)
    if isinstance(data, dict):
        return [dict(data)]
    if isinstance(data, list):
        return [dict(row) for row in data if isinstance(row, dict)]
    return []


def _rpc_payload(value: Any) -> Dict[str, Any]:
    data = getattr(value, "data", value)
    if isinstance(data, dict):
        return dict(data)
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return dict(data[0])
    return {}


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return int(default)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def record_yookassa_payment_intent(
    *,
    payment_id: str,
    user_id: int,
    tokens: int,
    amount_rub: float,
    payment_type: str = "topup",
    plan_code: str = "",
    duration_days: int = 0,
    provider_status: str = "pending",
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Persist a created YooKassa payment before its confirmation URL is returned.

    This is the durable recovery anchor used when an HTTP notification is delayed or
    lost. The row intentionally contains no receipt email/customer data.
    """
    sb = _require_client()
    pid = str(payment_id or "").strip()
    uid = _safe_int(user_id)
    token_count = _safe_int(tokens)
    amount = round(_safe_float(amount_rub), 2)
    if not pid or uid <= 0 or token_count <= 0 or amount <= 0:
        raise ValueError("invalid YooKassa payment intent")

    ptype = str(payment_type or "topup").strip().lower() or "topup"
    row = {
        "payment_id": pid,
        "user_id": uid,
        "tokens": token_count,
        "amount_rub": amount,
        "payment_type": ptype[:32],
        "plan_code": str(plan_code or "").strip().lower()[:32] or None,
        "duration_days": max(0, _safe_int(duration_days)),
        "state": "pending",
        "provider_status": str(provider_status or "pending").strip().lower()[:32] or "pending",
        "metadata": dict(metadata or {}),
        "updated_at": _now_iso(),
    }
    # Insert first. If a webhook won the tiny race and already created this row,
    # never overwrite its state (especially an already-applied payment).
    try:
        result = sb.table(YOOKASSA_INTENTS_TABLE).insert(row).execute()
        rows = _rows(result)
        return rows[0] if rows else row
    except Exception as insert_exc:
        try:
            existing = get_yookassa_payment_intent(pid)
        except Exception:
            existing = None
        if not existing:
            raise insert_exc
        if _safe_int(existing.get("user_id")) != uid:
            raise RuntimeError("existing YooKassa intent user mismatch") from insert_exc
        if _safe_int(existing.get("tokens")) != token_count:
            raise RuntimeError("existing YooKassa intent token mismatch") from insert_exc
        if abs(_safe_float(existing.get("amount_rub")) - amount) > 0.009:
            raise RuntimeError("existing YooKassa intent amount mismatch") from insert_exc
        return existing


def get_yookassa_payment_intent(payment_id: str) -> Optional[Dict[str, Any]]:
    sb = _require_client()
    pid = str(payment_id or "").strip()
    if not pid:
        return None
    result = sb.table(YOOKASSA_INTENTS_TABLE).select("*").eq("payment_id", pid).limit(1).execute()
    rows = _rows(result)
    return rows[0] if rows else None


def update_yookassa_provider_status(payment_id: str, provider_status: str, *, state: Optional[str] = None) -> None:
    sb = _require_client()
    pid = str(payment_id or "").strip()
    if not pid:
        return
    payload: Dict[str, Any] = {
        "provider_status": str(provider_status or "").strip().lower()[:32] or None,
        "updated_at": _now_iso(),
    }
    state_norm = str(state or "").strip().lower()[:32]
    if state_norm:
        payload["state"] = state_norm
    query = sb.table(YOOKASSA_INTENTS_TABLE).update(payload).eq("payment_id", pid)
    # Never let a stale provider read downgrade a committed/actively processing row.
    if state_norm == "pending":
        query = query.eq("state", "pending")
    elif state_norm == "canceled":
        query = query.neq("state", "applied")
    query.execute()


def claim_yookassa_payment(payment_id: str, *, lock_seconds: int = 180) -> Dict[str, Any]:
    sb = _require_client()
    pid = str(payment_id or "").strip()
    if not pid:
        raise ValueError("payment_id is required")
    response = sb.rpc(
        YOOKASSA_CLAIM_RPC,
        {"p_payment_id": pid, "p_lock_seconds": max(30, min(int(lock_seconds or 180), 1800))},
    ).execute()
    payload = _rpc_payload(response)
    if not payload:
        raise RuntimeError(f"{YOOKASSA_CLAIM_RPC} returned empty response")
    return payload



def claim_yookassa_subscription_user_lock(
    user_id: int,
    payment_id: str,
    *,
    lock_seconds: int = 300,
) -> Dict[str, Any]:
    """Acquire a durable cross-process lease for subscription mutation.

    Payment-level claims serialize one payment id. This lease additionally serializes
    subscription payments for the same canonical billing user across all web
    instances/workers. The lease expires automatically after a crash.
    """
    sb = _require_client()
    uid = int(resolve_billing_user_id(int(user_id)))
    pid = str(payment_id or "").strip()
    if uid <= 0 or not pid:
        raise ValueError("user_id and payment_id are required")
    response = sb.rpc(
        YOOKASSA_SUBSCRIPTION_LOCK_CLAIM_RPC,
        {
            "p_user_id": uid,
            "p_payment_id": pid,
            "p_lock_seconds": max(30, min(int(lock_seconds or 300), 1800)),
        },
    ).execute()
    payload = _rpc_payload(response)
    if not payload:
        raise RuntimeError(f"{YOOKASSA_SUBSCRIPTION_LOCK_CLAIM_RPC} returned empty response")
    return payload


def release_yookassa_subscription_user_lock(user_id: int, payment_id: str) -> None:
    """Release a subscription lease only when this payment still owns it."""
    sb = _require_client()
    uid = int(resolve_billing_user_id(int(user_id)))
    pid = str(payment_id or "").strip()
    if uid <= 0 or not pid:
        return
    last_exc: Optional[Exception] = None
    for attempt in range(1, 4):
        try:
            sb.rpc(
                YOOKASSA_SUBSCRIPTION_LOCK_RELEASE_RPC,
                {"p_user_id": uid, "p_payment_id": pid},
            ).execute()
            return
        except Exception as exc:
            last_exc = exc
            if attempt < 3:
                time.sleep(0.2 * attempt)
    raise RuntimeError(f"failed to release YooKassa subscription user lock: {last_exc}") from last_exc


def apply_yookassa_subscription_once(
    user_id: int,
    payment_id: str,
    *,
    plan_code: str,
    duration_days: int,
    amount_rub: float,
    tokens: int,
    ref_id: str,
) -> Dict[str, Any]:
    """Atomically apply a YooKassa subscription mutation and its durable marker.

    PostgreSQL performs the subscription write and inserts the payment marker in the
    same transaction. A retry can therefore never observe "subscription changed but
    idempotency marker missing" for payments processed by V6+.
    """
    sb = _require_client()
    uid = int(resolve_billing_user_id(int(user_id)))
    pid = str(payment_id or "").strip()
    plan = str(plan_code or "").strip().lower()
    days = max(1, min(_safe_int(duration_days, 30), 3660))
    token_count = _safe_int(tokens)
    amount = round(_safe_float(amount_rub), 2)
    try:
        ref_uuid = str(uuid.UUID(str(ref_id or "").strip()))
    except Exception as exc:
        raise ValueError("payment ref_id must be UUID") from exc
    if uid <= 0 or not pid or not plan or token_count <= 0 or amount <= 0:
        raise ValueError("invalid YooKassa subscription application")

    response = sb.rpc(
        YOOKASSA_SUBSCRIPTION_APPLY_RPC,
        {
            "p_user_id": uid,
            "p_payment_id": pid,
            "p_plan_code": plan,
            "p_duration_days": days,
            "p_amount_rub": f"{amount:.2f}",
            "p_tokens": token_count,
            "p_ref_id": ref_uuid,
        },
    ).execute()
    payload = _rpc_payload(response)
    if not payload or not bool(payload.get("ok")):
        raise RuntimeError(f"{YOOKASSA_SUBSCRIPTION_APPLY_RPC} returned invalid response: {payload!r}")
    return payload

def release_yookassa_payment(payment_id: str, error: str) -> None:
    sb = _require_client()
    pid = str(payment_id or "").strip()
    if not pid:
        return
    sb.table(YOOKASSA_INTENTS_TABLE).update(
        {
            "state": "pending",
            "processing_started_at": None,
            "last_error": str(error or "")[:1000],
            "updated_at": _now_iso(),
        }
    ).eq("payment_id", pid).eq("state", "processing").execute()


def mark_yookassa_payment_applied(payment_id: str, *, provider_status: str = "succeeded") -> None:
    sb = _require_client()
    pid = str(payment_id or "").strip()
    if not pid:
        return
    now = _now_iso()
    sb.table(YOOKASSA_INTENTS_TABLE).update(
        {
            "state": "applied",
            "provider_status": str(provider_status or "succeeded").strip().lower()[:32],
            "processing_started_at": None,
            "applied_at": now,
            "last_error": None,
            "updated_at": now,
        }
    ).eq("payment_id", pid).eq("state", "processing").execute()


def list_recoverable_yookassa_payments(*, limit: int = 25) -> List[str]:
    sb = _require_client()
    lim = max(1, min(int(limit or 25), 100))
    result = (
        sb.table(YOOKASSA_INTENTS_TABLE)
        .select("payment_id,state,processing_started_at,created_at,updated_at")
        .in_("state", ["pending", "processing"])
        .order("updated_at")
        .limit(lim)
        .execute()
    )
    return [str(row.get("payment_id") or "").strip() for row in _rows(result) if str(row.get("payment_id") or "").strip()]


def credit_tokens_once(
    user_id: int,
    tokens: int,
    *,
    reason: str,
    ref_id: str,
    meta: Optional[Dict[str, Any]] = None,
    attempts: int = 3,
) -> Dict[str, Any]:
    """Atomically credit balance and ledger exactly once for the stable payment ref."""
    sb = _require_client()
    amount = _safe_int(tokens)
    if amount <= 0:
        raise ValueError("tokens must be positive")
    ref_text = str(ref_id or "").strip()
    try:
        ref_uuid = str(uuid.UUID(ref_text))
    except Exception as exc:
        raise ValueError("payment ref_id must be UUID") from exc

    uid = int(resolve_billing_user_id(int(user_id)))
    max_attempts = max(1, min(int(attempts or 3), 5))
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = sb.rpc(
                YOOKASSA_CREDIT_RPC,
                {
                    "p_telegram_user_id": uid,
                    "p_amount": amount,
                    "p_reason": str(reason or "yookassa_topup"),
                    "p_ref_id": ref_uuid,
                    "p_meta": dict(meta or {}),
                    "p_ledger_id": str(uuid4()),
                },
            ).execute()
            payload = _rpc_payload(response)
            if not payload or not bool(payload.get("ok")):
                raise RuntimeError(f"{YOOKASSA_CREDIT_RPC} returned invalid response: {payload!r}")
            return payload
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts:
                time.sleep(min(2.0, 0.4 * attempt))
    raise RuntimeError(f"atomic YooKassa credit failed after {max_attempts} attempts: {last_exc}") from last_exc
