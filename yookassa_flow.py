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

# Для чеков: система налогообложения (1..6). У тебя ПАТЕНТ => 6.
# Можно задать в Render: YOOKASSA_TAX_SYSTEM_CODE=6
def _tax_system_code() -> int:
    raw = (os.getenv("YOOKASSA_TAX_SYSTEM_CODE") or "").strip()
    if not raw:
        return 6  # патент по умолчанию
    try:
        code = int(raw)
    except ValueError as e:
        raise RuntimeError("YOOKASSA_TAX_SYSTEM_CODE must be integer 1..6") from e
    if code < 1 or code > 6:
        raise RuntimeError("YOOKASSA_TAX_SYSTEM_CODE must be in range 1..6")
    return code


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
    if rub <= 0:
        raise ValueError("amount_rub must be > 0")

    idem = idempotence_key or str(uuid.uuid4())
    email = f"user{user_id}@example.com"
    tax_code = _tax_system_code()  # 6 = патент

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
            "tax_system_code": tax_code,
            "customer": {"email": email},
            "items": [
                {
                    "description": f"{tokens} токенов NeiroAstra",
                    "quantity": 1.0,
                    "amount": {
                        "value": f"{rub:.2f}",
                        "currency": "RUB",
                    },
                    "vat_code": 1,
                    "payment_mode": "full_prepayment",
                    "payment_subject": "service",
                }
            ],
        },
        "metadata": {
            "telegram_user_id": user_id,
            "tokens": tokens,
        },
    }

    headers = {
        "Authorization": _basic_auth_header(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
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
