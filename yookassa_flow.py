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
    token = base64.b64encode(f"{shop_id}:{secret_key}".encode()).decode()
    return f"Basic {token}"


def _require_creds():
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        raise RuntimeError("YOOKASSA creds missing")


async def create_yookassa_payment(
    *,
    amount_rub: int,
    description: str,
    user_id: int,
    tokens: int,
    idempotence_key: Optional[str] = None,
) -> Tuple[str, str]:

    _require_creds()

    rub = int(amount_rub)
    idem = idempotence_key or str(uuid.uuid4())
    email = f"user{user_id}@example.com"

    payload: Dict[str, Any] = {
        "amount": {
            "value": f"{rub:.2f}",
            "currency": "RUB",
        },
        "confirmation": {
            "type": "redirect",
            "return_url": YOOKASSA_RETURN_URL or "https://t.me",
        },
        "capture": True,
        "description": description,
        "receipt": {
            "customer": {
                "email": email
            },
            "items": [
                {
                    "description": f"{tokens} токенов NeiroAstra",
                    "quantity": 1.0,
                    "amount": {
                        "value": f"{rub:.2f}",
                        "currency": "RUB"
                    },
                    "vat_code": 1,
                    "payment_mode": "full_prepayment",
                    "payment_subject": "service"
                }
            ]
        },
        "metadata": {
            "telegram_user_id": user_id,
            "tokens": tokens
        }
    }

    headers = {
        "Authorization": _basic_auth_header(
            YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY
        ),
        "Idempotence-Key": idem,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            f"{YOOKASSA_API_BASE}/payments",
            json=payload,
            headers=headers,
        )

        if r.status_code >= 300:
            raise RuntimeError(
                f"YooKassa create payment failed: {r.status_code} {r.text}"
            )

        data = r.json()

    return data["id"], data["confirmation"]["confirmation_url"]
