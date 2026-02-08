# yookassa_flow.py
from __future__ import annotations

import base64
import os
import uuid
from typing import Any, Dict, Optional, Tuple

import httpx


YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "").strip()
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "").strip()
YOOKASSA_RETURN_URL = os.getenv("YOOKASSA_RETURN_URL", "").strip()

YOOKASSA_API_BASE = "https://api.yookassa.ru/v3"


def _basic_auth_header(shop_id: str, secret_key: str) -> str:
    token = base64.b64encode(f"{shop_id}:{secret_key}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _require_creds():
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        raise RuntimeError("YOOKASSA_SHOP_ID / YOOKASSA_SECRET_KEY not set")


async def create_yookassa_payment(
    *,
    amount_rub: int,
    description: str,
    user_id: int,
    tokens: int,
    idempotence_key: Optional[str] = None,
    return_url: Optional[str] = None,
) -> Tuple[str, str]:
    """
    Создаёт платёж в ЮKassa (redirect flow).
    Возвращает: (payment_id, confirmation_url)

    Важно: если в ЮKassa включены чеки (54-ФЗ), необходимо передавать receipt.
    """
    _require_creds()

    rub = int(amount_rub)
    if rub <= 0:
        raise ValueError("amount_rub must be > 0")

    idem = (idempotence_key or str(uuid.uuid4())).strip()

    # ✅ Добавили receipt, чтобы не было 400: "Receipt is missing or illegal"
    body: Dict[str, Any] = {
        "amount": {"value": f"{rub:.2f}", "currency": "RUB"},
        "confirmation": {
            "type": "redirect",
            "return_url": (return_url or YOOKASSA_RETURN_URL or "https://t.me"),
        },
        "capture": True,
        "description": description,
        "receipt": {
            "customer": {
                # Технический email допустим, если пользователь не вводит email.
                "email": f"user{user_id}@neiroastra.ai"
            },
            "items": [
                {
                    "description": f"{int(tokens)} токенов NeiroAstra",
                    "quantity": "1.00",
                    "amount": {"value": f"{rub:.2f}", "currency": "RUB"},
                    "vat_code": 1,  # 1 = без НДС
                    "payment_mode": "full_payment",
                    "payment_subject": "service",
                }
            ],
        },
        "metadata": {
            "telegram_user_id": int(user_id),
            "tokens": int(tokens),
        },
    }

    headers = {
        "Authorization": _basic_auth_header(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
        "Idempotence-Key": idem,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(f"{YOOKASSA_API_BASE}/payments", json=body, headers=headers)
        if r.status_code >= 300:
            # оставляем как было, чтобы ты видел текст ошибки ЮKassa
            raise RuntimeError(f"YooKassa create payment failed: {r.status_code} {r.text[:800]}")
        j = r.json()

    payment_id = (j.get("id") or "").strip()
    conf = j.get("confirmation") or {}
    confirmation_url = (conf.get("confirmation_url") or "").strip()

    if not payment_id or not confirmation_url:
        raise RuntimeError(f"YooKassa response missing id/url: {j}")

    return payment_id, confirmation_url


def parse_webhook(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Нормализуем webhook от ЮKassa.
    Возвращаем dict:
      event, payment_id, status, metadata
    """
    event = (payload.get("event") or "").strip()
    obj = payload.get("object") or {}
    payment_id = (obj.get("id") or "").strip()
    status = (obj.get("status") or "").strip()
    metadata = obj.get("metadata") or {}

    return {
        "event": event,
        "payment_id": payment_id,
        "status": status,
        "metadata": metadata,
        "raw": payload,
    }
