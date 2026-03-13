from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


WORKSPACE_AUTH_SECRET = (
    os.getenv("WORKSPACE_AUTH_SECRET")
    or os.getenv("TELEGRAM_BOT_TOKEN")
    or ""
).strip()
WORKSPACE_SESSION_TTL_SEC = int(os.getenv("WORKSPACE_SESSION_TTL_SEC", "43200") or 43200)

_http_bearer = HTTPBearer(auto_error=False)


class WorkspaceAuthError(ValueError):
    pass


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _secret() -> bytes:
    if not WORKSPACE_AUTH_SECRET:
        raise WorkspaceAuthError("WORKSPACE_AUTH_SECRET or TELEGRAM_BOT_TOKEN must be configured")
    return WORKSPACE_AUTH_SECRET.encode("utf-8")


def create_access_token(*, user: Dict[str, Any], ttl_seconds: Optional[int] = None) -> str:
    now_ts = int(time.time())
    ttl = WORKSPACE_SESSION_TTL_SEC if ttl_seconds is None else max(300, int(ttl_seconds))

    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": str(user["id"]),
        "telegram_user_id": int(user["id"]),
        "username": user.get("username"),
        "first_name": user.get("first_name"),
        "last_name": user.get("last_name"),
        "language_code": user.get("language_code"),
        "is_premium": bool(user.get("is_premium", False)),
        "iat": now_ts,
        "exp": now_ts + ttl,
        "aud": "astrabot-workspace",
    }

    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}".encode("utf-8")
    sig = hmac.new(_secret(), signing_input, hashlib.sha256).digest()
    return f"{header_b64}.{payload_b64}.{_b64url_encode(sig)}"


def decode_access_token(token: str) -> Dict[str, Any]:
    raw = (token or "").strip()
    if not raw:
        raise WorkspaceAuthError("Missing access token")

    try:
        header_b64, payload_b64, sig_b64 = raw.split(".", 2)
    except ValueError:
        raise WorkspaceAuthError("Malformed access token")

    signing_input = f"{header_b64}.{payload_b64}".encode("utf-8")
    expected_sig = hmac.new(_secret(), signing_input, hashlib.sha256).digest()
    actual_sig = _b64url_decode(sig_b64)
    if not hmac.compare_digest(expected_sig, actual_sig):
        raise WorkspaceAuthError("Invalid access token signature")

    try:
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except Exception as e:
        raise WorkspaceAuthError(f"Invalid access token payload: {e}")

    if payload.get("aud") != "astrabot-workspace":
        raise WorkspaceAuthError("Invalid access token audience")

    now_ts = int(time.time())
    try:
        exp = int(payload.get("exp") or 0)
    except Exception:
        exp = 0
    if exp <= now_ts:
        raise WorkspaceAuthError("Access token expired")

    uid = int(payload.get("telegram_user_id") or 0)
    if uid <= 0:
        raise WorkspaceAuthError("Access token has invalid telegram_user_id")

    return payload


async def get_optional_workspace_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_http_bearer),
) -> Optional[Dict[str, Any]]:
    if credentials is None or not credentials.credentials:
        return None
    try:
        return decode_access_token(credentials.credentials)
    except WorkspaceAuthError:
        return None


async def get_current_workspace_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_http_bearer),
) -> Dict[str, Any]:
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    try:
        return decode_access_token(credentials.credentials)
    except WorkspaceAuthError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))


def workspace_user_from_request(request: Request) -> Optional[Dict[str, Any]]:
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    token = auth[7:].strip()
    if not token:
        return None
    try:
        return decode_access_token(token)
    except WorkspaceAuthError:
        return None
