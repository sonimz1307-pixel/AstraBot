from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Any, Dict


class TelegramWebAuthError(ValueError):
    pass


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_LOGIN_MAX_AGE_SEC = int(os.getenv("TELEGRAM_LOGIN_MAX_AGE_SEC", "86400") or 86400)


ALLOWED_FIELDS = {
    "id",
    "first_name",
    "last_name",
    "username",
    "photo_url",
    "auth_date",
    "hash",
}


def validate_telegram_login_data(auth_data: Dict[str, Any], *, bot_token: str | None = None, max_age_seconds: int | None = None) -> Dict[str, Any]:
    token = (bot_token or TELEGRAM_BOT_TOKEN or "").strip()
    if not token:
        raise TelegramWebAuthError("TELEGRAM_BOT_TOKEN is not configured")

    raw = {k: v for k, v in (auth_data or {}).items() if k in ALLOWED_FIELDS and v not in (None, "")}
    if not raw:
        raise TelegramWebAuthError("Missing Telegram auth payload")

    their_hash = str(raw.pop("hash", "")).strip()
    if not their_hash:
        raise TelegramWebAuthError("Telegram auth payload has no hash")

    try:
        auth_date = int(raw.get("auth_date") or 0)
    except Exception:
        raise TelegramWebAuthError("Invalid auth_date")

    max_age = TELEGRAM_LOGIN_MAX_AGE_SEC if max_age_seconds is None else int(max_age_seconds)
    now_ts = int(time.time())
    if auth_date <= 0 or abs(now_ts - auth_date) > max_age:
        raise TelegramWebAuthError("Telegram auth data is expired")

    rows = [f"{k}={raw[k]}" for k in sorted(raw.keys())]
    data_check_string = "\n".join(rows)
    secret_key = hashlib.sha256(token.encode("utf-8")).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected_hash, their_hash):
        raise TelegramWebAuthError("Telegram auth hash mismatch")

    try:
        user_id = int(raw.get("id") or 0)
    except Exception:
        user_id = 0
    if user_id <= 0:
        raise TelegramWebAuthError("Telegram auth payload has invalid id")

    return {
        "id": user_id,
        "first_name": str(raw.get("first_name") or ""),
        "last_name": str(raw.get("last_name") or ""),
        "username": str(raw.get("username") or "") or None,
        "photo_url": str(raw.get("photo_url") or "") or None,
        "auth_date": auth_date,
    }
