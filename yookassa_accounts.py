"""Independent YooKassa shops and staged checkout rollout.

Checkout switches NEVER change the shop used to reconcile an existing payment.
Rows created before this migration always belong to the legacy shop.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def account_name(value: Any) -> str:
    name = str(value or "legacy").strip().lower()
    if name not in {"legacy", "new"}:
        raise ValueError("Неизвестный способ оплаты.")
    return name


def intent_account(intent: Optional[Dict[str, Any]]) -> str:
    metadata = (intent or {}).get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    return account_name(metadata.get("yookassa_account"))


@dataclass(frozen=True)
class YooKassaShop:
    account: str
    shop_id: str
    secret_key: str = field(repr=False)

    @property
    def prefix(self) -> str:
        return "YOOKASSA_NEW_" if self.account == "new" else "YOOKASSA_"

    @property
    def configured(self) -> bool:
        return bool(self.shop_id and self.secret_key)

    def require_credentials(self) -> None:
        if not self.configured:
            raise RuntimeError("Выбранный способ оплаты не настроен.")
        if self.secret_key.startswith("test_"):
            raise RuntimeError("Тестовые ключи запрещены для рабочего баланса.")
        if self.account == "new" and self.shop_id == _env("YOOKASSA_SHOP_ID"):
            raise RuntimeError("Для новой кассы требуется отдельный ShopID.")

    def tax_system_code(self) -> int:
        # Legacy fallback preserves the existing integration. New shop must be explicit.
        value = int(_env(self.prefix + "TAX_SYSTEM_CODE") or ("6" if self.account == "legacy" else "0"))
        if value not in range(1, 7):
            raise RuntimeError("Укажите систему налогообложения выбранной кассы.")
        return value

    def vat_code(self) -> int:
        value = int(_env(self.prefix + "VAT_CODE") or ("1" if self.account == "legacy" else "0"))
        if value not in range(1, 13):
            raise RuntimeError("Укажите ставку НДС выбранной кассы.")
        return value

    def metadata(self) -> Dict[str, str]:
        return {"yookassa_account": self.account, "yookassa_shop_id": self.shop_id, "product_id": "nabex"}


def get_shop(account: str = "legacy") -> YooKassaShop:
    name = account_name(account)
    prefix = "YOOKASSA_NEW_" if name == "new" else "YOOKASSA_"
    return YooKassaShop(name, _env(prefix + "SHOP_ID"), _env(prefix + "SECRET_KEY"))


def any_shop_configured() -> bool:
    return get_shop("legacy").configured or get_shop("new").configured


def _new_checkout_allowed(user_id: int, authenticated: bool) -> bool:
    if not authenticated or int(user_id or 0) <= 0 or not get_shop("new").configured:
        return False
    mode = _env("YOOKASSA_NEW_MODE", "off").lower()
    if mode == "all":
        return True
    if mode != "testers":
        return False
    allowed = {part.strip() for part in _env("YOOKASSA_NEW_TEST_USER_IDS").split(",") if part.strip()}
    if str(int(user_id)) in allowed:
        return True
    if allowed:
        try:
            from billing_db import resolve_billing_user_id
            return str(int(resolve_billing_user_id(int(user_id)))) in allowed
        except Exception:
            return False
    return False


def payment_methods(user_id: int, *, authenticated: bool = False) -> Dict[str, Any]:
    methods = []
    legacy_on = _env("YOOKASSA_LEGACY_CHECKOUT_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
    if legacy_on and get_shop("legacy").configured:
        methods.append({"id": "legacy", "label": "Карта / СБП"})
    if _new_checkout_allowed(user_id, authenticated):
        methods.append({"id": "new", "label": "Карта / СБП — способ 2" if methods else "Карта / СБП"})
    default = account_name(_env("YOOKASSA_DEFAULT_ACCOUNT", "legacy"))
    if default not in {method["id"] for method in methods}:
        default = methods[0]["id"] if methods else ""
    methods.sort(key=lambda method: method["id"] != default)
    return {"ok": True, "methods": methods, "default": default, "user_id": int(user_id) if authenticated else None}


def checkout_shop(requested: Optional[str], user_id: int, *, authenticated: bool = False) -> YooKassaShop:
    available = payment_methods(user_id, authenticated=authenticated)
    selected = account_name(requested) if requested else available["default"]
    if selected not in {item["id"] for item in available["methods"]}:
        raise ValueError("Этот способ оплаты сейчас недоступен. Выберите другой.")
    shop = get_shop(selected)
    shop.require_credentials()
    return shop


def validate_provider_shop(payment: Dict[str, Any], shop: YooKassaShop) -> None:
    # A test payment must never credit a real balance, regardless of checkout flags.
    if payment.get("test") is not False:
        raise RuntimeError("YooKassa live payment proof is missing")
    recipient = payment.get("recipient")
    recipient_id = str(recipient.get("account_id") or "") if isinstance(recipient, dict) else ""
    if (shop.account == "new" or recipient_id) and recipient_id != shop.shop_id:
        raise RuntimeError("YooKassa recipient shop mismatch")
    metadata = payment.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    for key, expected in shop.metadata().items():
        if (shop.account == "new" or key in metadata) and str(metadata.get(key) or "") != expected:
            raise RuntimeError("YooKassa payment shop metadata mismatch")
