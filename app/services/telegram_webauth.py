from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict
from urllib.parse import parse_qsl


class TelegramWebAuthError(ValueError):
    pass


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_INITDATA_MAX_AGE_SEC = int(os.getenv("TELEGRAM_INITDATA_MAX_AGE_SEC", "86400") or 86400)


def _build_data_check_string(params: Dict[str, str]) -> str:
    rows = [f"{k}={v}" for k, v in sorted(params.items(), key=lambda item: item[0])]
    return "\n".join(rows)


def validate_telegram_init_data(
    init_data: str,
    *,
    bot_token: str | None = None,
    max_age_seconds: int | None = None,
) -> Dict[str, Any]:
    raw = (init_data or "").strip()
    if not raw:
        raise TelegramWebAuthError("Missing Telegram initData")

    token = (bot_token or TELEGRAM_BOT_TOKEN or "").strip()
    if not token:
        raise TelegramWebAuthError("TELEGRAM_BOT_TOKEN is not configured")

    pairs = dict(parse_qsl(raw, keep_blank_values=True))
    their_hash = (pairs.pop("hash", "") or "").strip()
    if not their_hash:
        raise TelegramWebAuthError("Telegram initData has no hash")

    auth_date_raw = (pairs.get("auth_date") or "").strip()
    if not auth_date_raw:
        raise TelegramWebAuthError("Telegram initData has no auth_date")

    try:
        auth_date = int(auth_date_raw)
    except Exception as e:
        raise TelegramWebAuthError(f"Invalid auth_date: {e}")

    max_age = TELEGRAM_INITDATA_MAX_AGE_SEC if max_age_seconds is None else int(max_age_seconds)
    now_ts = int(time.time())
    if auth_date <= 0 or abs(now_ts - auth_date) > max_age:
        raise TelegramWebAuthError("Telegram initData is expired")

    data_check_string = _build_data_check_string(pairs)
    secret_key = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected_hash, their_hash):
        raise TelegramWebAuthError("Telegram initData hash mismatch")

    user_raw = (pairs.get("user") or "").strip()
    if not user_raw:
        raise TelegramWebAuthError("Telegram initData has no user payload")

    try:
        user = json.loads(user_raw)
    except Exception as e:
        raise TelegramWebAuthError(f"Invalid Telegram user JSON: {e}")

    if not isinstance(user, dict) or not user.get("id"):
        raise TelegramWebAuthError("Telegram user payload is invalid")

    return {
        "ok": True,
        "auth_date": auth_date,
        "user": user,
        "query_id": pairs.get("query_id"),
        "chat_type": pairs.get("chat_type"),
        "chat_instance": pairs.get("chat_instance"),
        "start_param": pairs.get("start_param"),
        "raw": raw,
        "data_check_string": data_check_string,
    }
