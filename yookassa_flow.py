# yookassa_flow.py
from __future__ import annotations

import asyncio
import base64
import os
import uuid
from typing import Any, Dict, Optional, Tuple

import httpx

from yookassa_store import record_yookassa_payment_intent
from yookassa_accounts import checkout_shop, get_shop, validate_provider_shop

YOOKASSA_FLOW_VERSION = "2026-10-06_dual_shop"

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "").strip()
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "").strip()
YOOKASSA_RETURN_URL = os.getenv("YOOKASSA_RETURN_URL", "").strip()

YOOKASSA_API_BASE = "https://api.yookassa.ru/v3"


def _basic_auth_header(shop_id: str, secret_key: str) -> str:
    token = base64.b64encode(f"{shop_id}:{secret_key}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


async def fetch_yookassa_payment(payment_id: str, *, account: str = "legacy") -> Dict[str, Any]:
    """Fetch current payment object directly from YooKassa API. Used by webhook handler before any financial action."""
    shop = get_shop(account)
    shop.require_credentials()
    pid = str(payment_id or "").strip()
    if not pid:
        raise ValueError("payment_id is required")

    headers = {"Authorization": _basic_auth_header(shop.shop_id, shop.secret_key)}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"{YOOKASSA_API_BASE}/payments/{pid}", headers=headers)
        try:
            j = r.json()
        except Exception:
            j = {}
        if r.status_code >= 300:
            raise RuntimeError(f"YooKassa fetch payment failed: {r.status_code} {str(j)[:300]}")
        if not isinstance(j, dict):
            raise RuntimeError("YooKassa fetch payment: bad JSON")
        validate_provider_shop(j, shop)
        return j


async def create_yookassa_payment(
    *,
    amount_rub: int,
    description: str,
    user_id: int,
    tokens: int,
    customer_email: Optional[str] = None,
    idempotence_key: Optional[str] = None,
    return_url: Optional[str] = None,
    payment_metadata: Optional[Dict[str, Any]] = None,
    receipt_item_description: Optional[str] = None,
    payment_account: Optional[str] = None,
    authenticated_user: bool = False,
) -> Tuple[str, str]:
    """
    Создаёт платёж в ЮKassa (redirect).
    Возвращает: (payment_id, confirmation_url)

    КЛЮЧЕВОЕ:
    - Передаём receipt + receipt.customer.email (берём из Supabase),
      чтобы сервис «Чеки от ЮKassa» мог сформировать и отправить чек.
    """
    shop = checkout_shop(payment_account, user_id, authenticated=authenticated_user)

    rub = int(amount_rub)
    if rub <= 0:
        raise ValueError("amount_rub must be > 0")

    idem = (idempotence_key or str(uuid.uuid4())).strip()
    tax_code = shop.tax_system_code()

    email = (customer_email or "").strip().lower()
    if not email:
        raise ValueError("customer_email is required for receipts")

    metadata: Dict[str, Any] = {
        "user_id": int(user_id),  # важно: main.py ждёт metadata.user_id / tokens
        "tokens": int(tokens),
    }
    if isinstance(payment_metadata, dict):
        for key, value in payment_metadata.items():
            key_text = str(key or "").strip()
            if not key_text or key_text in {"user_id", "tokens"}:
                continue
            metadata[key_text] = value

    # Server-controlled routing cannot be overridden by caller metadata.
    metadata.setdefault("payment_type", "topup")
    metadata.update(shop.metadata())

    brand = "Nabex" if shop.account == "new" else "NeiroAstra"
    receipt_description = (receipt_item_description or f"{int(tokens)} токенов {brand}").strip()

    body: Dict[str, Any] = {
        "amount": {"value": f"{rub:.2f}", "currency": "RUB"},
        "confirmation": {
            "type": "redirect",
            "return_url": (return_url or YOOKASSA_RETURN_URL or "https://t.me"),
        },
        "capture": True,
        "description": description[:128],
        "metadata": metadata,
        "receipt": {
            "tax_system_code": tax_code,
            "customer": {"email": email},
            "items": [
                {
                    "description": receipt_description[:128],
                    "quantity": 1.0,
                    "amount": {"value": f"{rub:.2f}", "currency": "RUB"},
                    "vat_code": shop.vat_code(),
                    "payment_mode": "full_payment",
                    "payment_subject": "service",
                }
            ],
        },
    }

    headers = {
        "Authorization": _basic_auth_header(shop.shop_id, shop.secret_key),
        "Idempotence-Key": idem,
        "Content-Type": "application/json",
    }

    print("YOOKASSA_FLOW_VERSION =", YOOKASSA_FLOW_VERSION)
    print("YOOKASSA REQUEST BODY (safe) =", {
        "amount": body.get("amount"),
        "capture": body.get("capture"),
        "description_len": len(str(body.get("description") or "")),
        "has_confirmation": bool(body.get("confirmation")),
        "has_receipt": bool(body.get("receipt")),
        "has_metadata": bool(body.get("metadata")),
    })

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(f"{YOOKASSA_API_BASE}/payments", json=body, headers=headers)

        if r.status_code >= 300:
            raise RuntimeError(f"YooKassa create payment failed: HTTP {r.status_code}")

        j = r.json()

    validate_provider_shop(j, shop)

    payment_id = (j.get("id") or "").strip()
    conf = j.get("confirmation") or {}
    confirmation_url = (conf.get("confirmation_url") or "").strip()

    if not payment_id or not confirmation_url:
        raise RuntimeError(f"YooKassa response missing id/url: {j}")

    payment_type = str(metadata.get("payment_type") or "topup").strip().lower() or "topup"
    plan_code = str(metadata.get("plan_code") or "").strip().lower()
    try:
        duration_days = int(float(metadata.get("duration_days") or 0))
    except Exception:
        duration_days = 0

    # Durable recovery anchor. We intentionally persist this before exposing the
    # confirmation URL to the client. If the DB is temporarily unavailable, the
    # caller gets an error instead of a payment link that our system cannot later
    # reconcile safely.
    try:
        await asyncio.to_thread(
            record_yookassa_payment_intent,
            payment_id=payment_id,
            user_id=int(user_id),
            tokens=int(tokens),
            amount_rub=rub,
            payment_type=payment_type,
            plan_code=plan_code,
            duration_days=duration_days,
            provider_status=str(j.get("status") or "pending"),
            metadata={"flow_version": YOOKASSA_FLOW_VERSION, **shop.metadata()},
        )
    except Exception as exc:
        raise RuntimeError(f"YooKassa payment created but recovery state was not saved: {exc}") from exc

    return payment_id, confirmation_url
