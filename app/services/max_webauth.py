from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict, List, Tuple
from urllib.parse import parse_qsl, quote, unquote, urlparse


class MaxWebAuthError(ValueError):
    pass


MAX_BOT_TOKEN = (os.getenv("MAX_BOT_TOKEN") or os.getenv("MAX_ACCESS_TOKEN") or "").strip()
MAX_BOT_USERNAME = (os.getenv("MAX_BOT_USERNAME") or "").strip().lstrip("@")


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)) or str(default))
    except Exception:
        value = int(default)
    if minimum is not None:
        value = max(int(minimum), value)
    return value


MAX_WEBAPP_AUTH_MAX_AGE_SEC = _env_int("MAX_WEBAPP_AUTH_MAX_AGE_SEC", 3600, minimum=0)


def get_max_bot_token() -> str:
    token = (os.getenv("MAX_BOT_TOKEN") or os.getenv("MAX_ACCESS_TOKEN") or MAX_BOT_TOKEN or "").strip()
    if not token:
        raise MaxWebAuthError("MAX_BOT_TOKEN is not configured")
    return token


def get_max_bot_username() -> str:
    username = (os.getenv("MAX_BOT_USERNAME") or MAX_BOT_USERNAME or "").strip().lstrip("@")
    if not username:
        raise MaxWebAuthError("MAX_BOT_USERNAME is not configured")
    return username


def build_max_startapp_url(start_param: str, *, bot_username: str | None = None) -> str:
    username = (bot_username or get_max_bot_username()).strip().lstrip("@")
    state = str(start_param or "").strip()
    if not username:
        raise MaxWebAuthError("MAX_BOT_USERNAME is not configured")
    if not state:
        raise MaxWebAuthError("MAX auth state is empty")
    return f"https://max.ru/{username}?startapp={quote(state, safe='')}"


def _extract_webapp_data(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        raise MaxWebAuthError("Missing MAX initData")

    # MAX can pass either window.WebApp.initData directly or a full URL/hash
    # like https://site/#WebAppData=<encoded>&WebAppPlatform=...
    if "#" in value:
        fragment = urlparse(value).fragment or value.split("#", 1)[1]
        outer = dict(parse_qsl(fragment, keep_blank_values=True))
        if outer.get("WebAppData"):
            return outer["WebAppData"]

    if "WebAppData=" in value:
        outer = dict(parse_qsl(value.lstrip("#"), keep_blank_values=True))
        if outer.get("WebAppData"):
            return outer["WebAppData"]

    return value


def _parse_unique_pairs(app_data: str) -> List[Tuple[str, str]]:
    pairs = parse_qsl(app_data, keep_blank_values=True, strict_parsing=False)
    if not pairs:
        raise MaxWebAuthError("MAX initData is empty")

    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise MaxWebAuthError(f"MAX initData contains duplicate key: {key}")
        seen.add(key)
    return pairs


def validate_max_init_data(
    init_data: str,
    *,
    bot_token: str | None = None,
    max_age_seconds: int | None = None,
) -> Dict[str, Any]:
    token = (bot_token or get_max_bot_token()).strip()
    app_data = _extract_webapp_data(init_data)
    pairs = _parse_unique_pairs(app_data)

    hash_values = [value for key, value in pairs if key == "hash"]
    if len(hash_values) != 1 or not str(hash_values[0] or "").strip():
        raise MaxWebAuthError("MAX initData has no valid hash")
    their_hash = str(hash_values[0]).strip()

    data_pairs = [(key, value) for key, value in pairs if key != "hash"]
    data_pairs.sort(key=lambda item: item[0])
    launch_params = "\n".join(f"{key}={value}" for key, value in data_pairs)

    secret_key = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, launch_params.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, their_hash):
        raise MaxWebAuthError("MAX initData hash mismatch")

    data = {key: value for key, value in data_pairs}

    try:
        auth_date = int(data.get("auth_date") or 0)
    except Exception:
        raise MaxWebAuthError("Invalid MAX auth_date")

    max_age = MAX_WEBAPP_AUTH_MAX_AGE_SEC if max_age_seconds is None else int(max_age_seconds)
    if max_age > 0:
        now_ts = int(time.time())
        if auth_date <= 0 or abs(now_ts - auth_date) > max_age:
            raise MaxWebAuthError("MAX initData is expired")

    try:
        user_raw = data.get("user") or "{}"
        user = json.loads(user_raw)
    except Exception as exc:
        raise MaxWebAuthError(f"Invalid MAX user payload: {exc}")

    try:
        user_id = int(user.get("id") or 0)
    except Exception:
        user_id = 0
    if user_id <= 0:
        raise MaxWebAuthError("MAX user id is missing")

    return {
        "id": user_id,
        "first_name": str(user.get("first_name") or ""),
        "last_name": str(user.get("last_name") or ""),
        "username": str(user.get("username") or "") or None,
        "language_code": str(user.get("language_code") or "") or None,
        "photo_url": str(user.get("photo_url") or "") or None,
        "auth_date": auth_date,
        "query_id": str(data.get("query_id") or "") or None,
        "start_param": str(data.get("start_param") or "") or None,
        "chat": json.loads(data.get("chat") or "null") if data.get("chat") else None,
        "raw": data,
    }
