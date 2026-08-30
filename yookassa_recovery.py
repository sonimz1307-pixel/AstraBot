from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation

from typing import Any, Dict, Optional
from uuid import NAMESPACE_URL, uuid5

from billing_db import resolve_billing_user_id
from subscriptions_db import get_subscription_plan
from yookassa_flow import fetch_yookassa_payment
from yookassa_store import (
    claim_yookassa_payment,
    claim_yookassa_subscription_user_lock,
    apply_yookassa_subscription_once,
    credit_tokens_once,
    get_yookassa_payment_intent,
    mark_yookassa_payment_applied,
    record_yookassa_payment_intent,
    release_yookassa_payment,
    release_yookassa_subscription_user_lock,
    update_yookassa_provider_status,
)

PUBLIC_SUBSCRIPTION_PLAN_CODES = {"spark", "pulse", "nexus"}


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



def _require_verified_rub_amount(payment: Dict[str, Any]) -> Decimal:
    """Return YooKassa amount only when the provider response is financially complete.

    A succeeded/paid response with a missing amount/currency must never fall back to
    the locally stored intent. The local intent tells us what we expected to sell;
    the provider response must independently prove what was actually paid.
    """
    amount_obj = payment.get("amount")
    if not isinstance(amount_obj, dict):
        raise RuntimeError("YooKassa amount is missing")

    currency = str(amount_obj.get("currency") or "").strip().upper()
    if currency != "RUB":
        raise RuntimeError(f"YooKassa currency mismatch: {currency or 'missing'}")

    raw_value = amount_obj.get("value")
    if raw_value is None or str(raw_value).strip() == "":
        raise RuntimeError("YooKassa amount value is missing")
    try:
        amount = Decimal(str(raw_value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise RuntimeError("YooKassa amount value is invalid") from exc
    if not amount.is_finite() or amount <= 0:
        raise RuntimeError("YooKassa amount value must be positive")
    try:
        quantized = amount.quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise RuntimeError("YooKassa amount value is invalid") from exc
    if amount != quantized:
        raise RuntimeError("YooKassa amount value has invalid precision")
    return quantized

def _same_user(a: int, b: int) -> bool:
    if int(a or 0) <= 0 or int(b or 0) <= 0:
        return False
    if int(a) == int(b):
        return True
    try:
        return int(resolve_billing_user_id(int(a))) == int(resolve_billing_user_id(int(b)))
    except Exception:
        return False



async def reconcile_yookassa_payment(
    payment_id: str,
    *,
    expected_user_id: Optional[int] = None,
    source: str = "reconcile",
) -> Dict[str, Any]:
    """Verify YooKassa directly and apply a successful payment exactly once.

    The function is safe to call concurrently from the webhook, a browser return,
    Telegram WebApp, and the periodic in-process reconciler.
    """
    pid = str(payment_id or "").strip()
    if not pid:
        raise ValueError("payment_id is required")

    intent = await asyncio.to_thread(get_yookassa_payment_intent, pid)
    if intent and expected_user_id and not _same_user(_safe_int(intent.get("user_id")), int(expected_user_id)):
        raise PermissionError("payment does not belong to this user")
    if intent and str(intent.get("state") or "").lower() == "applied":
        return {
            "ok": True,
            "payment_id": pid,
            "status": "applied",
            "provider_status": str(intent.get("provider_status") or "succeeded"),
            "newly_applied": False,
            "user_id": _safe_int(intent.get("user_id")),
            "tokens": _safe_int(intent.get("tokens")),
            "amount_rub": _safe_float(intent.get("amount_rub")),
            "payment_type": str(intent.get("payment_type") or "topup"),
            "plan_code": str(intent.get("plan_code") or ""),
        }

    verified = await fetch_yookassa_payment(pid)
    verified_id = str(verified.get("id") or "").strip()
    if verified_id != pid:
        raise RuntimeError("YooKassa payment id mismatch")

    provider_status = str(verified.get("status") or "").strip().lower()
    # YooKassa contract uses a JSON boolean. Fail closed on malformed truthy values
    # such as the string "true" rather than interpreting them as payment proof.
    paid = verified.get("paid") is True
    # Financial proof must be complete. Never use the stored sale intent as a
    # substitute for a missing provider amount/currency.
    verified_amount = _require_verified_rub_amount(verified)
    amount_rub = float(verified_amount)

    md = verified.get("metadata") if isinstance(verified.get("metadata"), dict) else {}
    md_uid = _safe_int(md.get("user_id"))
    md_tokens = _safe_int(md.get("tokens"))
    md_type = str(md.get("payment_type") or "").strip().lower()
    md_plan = str(md.get("plan_code") or "").strip().lower()
    md_duration = _safe_int(md.get("duration_days"))

    if not intent:
        if md_uid <= 0 or md_tokens <= 0 or amount_rub <= 0:
            raise RuntimeError("YooKassa payment has no durable intent and missing recovery metadata")
        payment_type = "subscription" if (md_type == "subscription" or md_plan in PUBLIC_SUBSCRIPTION_PLAN_CODES) else "topup"
        intent = await asyncio.to_thread(
            record_yookassa_payment_intent,
            payment_id=pid,
            user_id=md_uid,
            tokens=md_tokens,
            amount_rub=amount_rub,
            payment_type=payment_type,
            plan_code=md_plan,
            duration_days=md_duration,
            provider_status=provider_status or "pending",
            metadata={"recovered_from": source},
        )

    uid = _safe_int(intent.get("user_id"))
    tokens = _safe_int(intent.get("tokens"))
    expected_amount = round(_safe_float(intent.get("amount_rub")), 2)
    payment_type = str(intent.get("payment_type") or "topup").strip().lower() or "topup"
    plan_code = str(intent.get("plan_code") or "").strip().lower()
    duration_days = _safe_int(intent.get("duration_days"))

    if expected_user_id and not _same_user(uid, int(expected_user_id)):
        raise PermissionError("payment does not belong to this user")
    if uid <= 0 or tokens <= 0 or expected_amount <= 0:
        raise RuntimeError("stored YooKassa payment intent is invalid")
    if md_uid > 0 and not _same_user(uid, md_uid):
        raise RuntimeError("YooKassa metadata user mismatch")
    if md_tokens > 0 and md_tokens != tokens:
        raise RuntimeError("YooKassa metadata token mismatch")
    expected_amount_decimal = Decimal(str(expected_amount)).quantize(Decimal("0.01"))
    if verified_amount != expected_amount_decimal:
        raise RuntimeError(
            f"YooKassa amount mismatch: expected {expected_amount_decimal:.2f}, got {verified_amount:.2f}"
        )

    if provider_status == "canceled":
        await asyncio.to_thread(update_yookassa_provider_status, pid, provider_status, state="canceled")
        return {
            "ok": True,
            "payment_id": pid,
            "status": "canceled",
            "provider_status": provider_status,
            "newly_applied": False,
            "user_id": uid,
            "tokens": tokens,
            "amount_rub": expected_amount,
            "payment_type": payment_type,
            "plan_code": plan_code,
        }

    if provider_status != "succeeded" or not paid:
        await asyncio.to_thread(update_yookassa_provider_status, pid, provider_status or "pending", state="pending")
        return {
            "ok": True,
            "payment_id": pid,
            "status": "pending",
            "provider_status": provider_status or "pending",
            "newly_applied": False,
            "user_id": uid,
            "tokens": tokens,
            "amount_rub": expected_amount,
            "payment_type": payment_type,
            "plan_code": plan_code,
        }

    claim = await asyncio.to_thread(
        claim_yookassa_payment,
        pid,
        lock_seconds=300 if payment_type == "subscription" else 180,
    )
    if not bool(claim.get("claimed")):
        latest = (await asyncio.to_thread(get_yookassa_payment_intent, pid)) or intent
        state = str(latest.get("state") or "processing").strip().lower()
        return {
            "ok": True,
            "payment_id": pid,
            "status": state,
            "provider_status": str(latest.get("provider_status") or provider_status),
            "newly_applied": False,
            "user_id": uid,
            "tokens": tokens,
            "amount_rub": expected_amount,
            "payment_type": payment_type,
            "plan_code": plan_code,
        }

    payment_ref_id = str(uuid5(NAMESPACE_URL, f"nabex:yookassa:{pid}"))
    reason = "yookassa_subscription" if payment_type == "subscription" else "yookassa_topup"
    subscription_changed = False
    subscription_lock_acquired = False
    try:
        if payment_type == "subscription":
            if plan_code not in PUBLIC_SUBSCRIPTION_PLAN_CODES:
                raise RuntimeError(f"unknown subscription plan: {plan_code}")

            # Payment-level claim above prevents duplicate work for this payment.
            # This second, durable lease prevents two DIFFERENT subscription payments
            # for the same user from reading/mutating the plan concurrently on separate
            # web instances. It expires automatically if a process dies mid-payment.
            user_lock = await asyncio.to_thread(
                claim_yookassa_subscription_user_lock,
                uid,
                pid,
                lock_seconds=300,
            )
            if not bool(user_lock.get("claimed")):
                await asyncio.to_thread(
                    release_yookassa_payment,
                    pid,
                    "subscription user lock busy; retry safely",
                )
                return {
                    "ok": True,
                    "payment_id": pid,
                    "status": "pending",
                    "provider_status": provider_status,
                    "newly_applied": False,
                    "user_id": uid,
                    "tokens": tokens,
                    "amount_rub": expected_amount,
                    "payment_type": payment_type,
                    "plan_code": plan_code,
                    "retry_reason": "subscription_user_lock_busy",
                }
            subscription_lock_acquired = True

            plan = await asyncio.to_thread(get_subscription_plan, plan_code)
            # The durable intent is the sale-time contract. A later tariff edit must
            # not invalidate an already-paid purchase that is being reconciled.
            duration_days = max(1, duration_days or _safe_int(plan.get("duration_days"), 30))

            subscription_apply = await asyncio.to_thread(
                apply_yookassa_subscription_once,
                uid,
                pid,
                plan_code=plan_code,
                duration_days=duration_days,
                amount_rub=expected_amount,
                tokens=tokens,
                ref_id=payment_ref_id,
            )
            subscription_changed = bool(subscription_apply.get("applied"))

        credit = await asyncio.to_thread(
            credit_tokens_once,
            uid,
            tokens,
            reason=reason,
            ref_id=payment_ref_id,
            meta={
                "payment_id": pid,
                "status": provider_status,
                "amount_rub": expected_amount,
                "provider": "yookassa",
                "payment_type": payment_type,
                "plan_code": plan_code if payment_type == "subscription" else "",
                "reconcile_source": source,
            },
        )
        await asyncio.to_thread(mark_yookassa_payment_applied, pid, provider_status=provider_status)
        return {
            "ok": True,
            "payment_id": pid,
            "status": "applied",
            "provider_status": provider_status,
            "newly_applied": bool(credit.get("credited")) or subscription_changed,
            "tokens_credited_now": bool(credit.get("credited")),
            "subscription_changed_now": subscription_changed,
            "balance_tokens": _safe_int(credit.get("balance_tokens")),
            "user_id": uid,
            "tokens": tokens,
            "amount_rub": expected_amount,
            "payment_type": payment_type,
            "plan_code": plan_code,
        }
    except Exception as exc:
        try:
            await asyncio.to_thread(release_yookassa_payment, pid, str(exc))
        except Exception:
            pass
        raise
    finally:
        if payment_type == "subscription" and subscription_lock_acquired:
            try:
                await asyncio.to_thread(release_yookassa_subscription_user_lock, uid, pid)
            except Exception:
                # The lease has a hard TTL, so a transient release failure cannot
                # deadlock future payments. Do not turn a completed credit into an
                # apparent failure merely because cleanup could not reach Supabase.
                pass
