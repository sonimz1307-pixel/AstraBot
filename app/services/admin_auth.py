from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any, Dict, Optional, Set

from fastapi import HTTPException, Request, Response, status

LOG = logging.getLogger("uvicorn.error")

ADMIN_SESSION_COOKIE_NAME = (os.getenv("ADMIN_SESSION_COOKIE_NAME") or "nabex_admin_session").strip() or "nabex_admin_session"
ADMIN_SESSION_TTL_SEC = max(300, int(os.getenv("ADMIN_SESSION_TTL_SEC", "3600") or 3600))
ADMIN_SESSION_COOKIE_SECURE = os.getenv("ADMIN_SESSION_COOKIE_SECURE", "1").strip().lower() not in ("0", "false", "no", "off")
ADMIN_SESSION_COOKIE_SAMESITE = (os.getenv("ADMIN_SESSION_COOKIE_SAMESITE") or "lax").strip().lower() or "lax"
if ADMIN_SESSION_COOKIE_SAMESITE not in ("lax", "strict", "none"):
    ADMIN_SESSION_COOKIE_SAMESITE = "lax"
ADMIN_SESSION_COOKIE_DOMAIN = (os.getenv("ADMIN_SESSION_COOKIE_DOMAIN") or "").strip() or None


def _allowed_ips() -> Set[str]:
    raw = os.getenv("ADMIN_ALLOWED_IPS", "") or ""
    return {x.strip() for x in raw.split(",") if x.strip()}


def _client_ip(request: Optional[Request]) -> str:
    if request is None:
        return ""
    for header in ("CF-Connecting-IP", "X-Real-IP"):
        val = (request.headers.get(header) or "").strip()
        if val:
            return val
    forwarded = (request.headers.get("X-Forwarded-For") or "").strip()
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return getattr(getattr(request, "client", None), "host", "") or ""


def _check_admin_ip(request: Optional[Request]) -> None:
    allowed = _allowed_ips()
    if not allowed:
        return
    ip = _client_ip(request)
    if ip not in allowed:
        LOG.warning("admin_auth_denied_ip ip=%s path=%s", ip, getattr(getattr(request, "url", None), "path", ""))
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin ip is not allowed")


def _admin_token() -> str:
    return (os.getenv("ADMIN_TOKEN") or "").strip()


def verify_admin_token(token: Optional[str]) -> bool:
    expected = _admin_token()
    given = (token or "").strip()
    if not expected:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="ADMIN_TOKEN is not configured")
    return bool(given) and hmac.compare_digest(expected, given)


def _session_secret() -> bytes:
    secret = (
        os.getenv("ADMIN_SESSION_SECRET")
        or os.getenv("WORKSPACE_AUTH_SECRET")
        or os.getenv("TELEGRAM_BOT_TOKEN")
        or os.getenv("ADMIN_TOKEN")
        or ""
    ).strip()
    if not secret:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="ADMIN_SESSION_SECRET is not configured")
    return secret.encode("utf-8")


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + ("=" * (-len(data) % 4)))


def create_admin_session_token(*, ttl_seconds: Optional[int] = None) -> str:
    now_ts = int(time.time())
    ttl = ADMIN_SESSION_TTL_SEC if ttl_seconds is None else max(300, int(ttl_seconds))
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "aud": "nabex-admin",
        "iat": now_ts,
        "exp": now_ts + ttl,
        "scope": "admin",
        # Helps invalidate all old sessions after ADMIN_TOKEN rotation without storing the raw token in the cookie.
        "tok": hashlib.sha256(_admin_token().encode("utf-8")).hexdigest()[:16],
    }
    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}".encode("utf-8")
    sig = hmac.new(_session_secret(), signing_input, hashlib.sha256).digest()
    return f"{header_b64}.{payload_b64}.{_b64url_encode(sig)}"


def decode_admin_session_token(token: str) -> Dict[str, Any]:
    raw = (token or "").strip()
    if not raw:
        raise ValueError("missing admin session")
    try:
        header_b64, payload_b64, sig_b64 = raw.split(".", 2)
    except ValueError:
        raise ValueError("malformed admin session")
    signing_input = f"{header_b64}.{payload_b64}".encode("utf-8")
    expected_sig = hmac.new(_session_secret(), signing_input, hashlib.sha256).digest()
    try:
        actual_sig = _b64url_decode(sig_b64)
    except Exception:
        raise ValueError("bad admin session signature")
    if not hmac.compare_digest(expected_sig, actual_sig):
        raise ValueError("bad admin session signature")
    try:
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except Exception as e:
        raise ValueError(f"bad admin session payload: {e}")
    if payload.get("aud") != "nabex-admin" or payload.get("scope") != "admin":
        raise ValueError("bad admin session audience")
    if int(payload.get("exp") or 0) <= int(time.time()):
        raise ValueError("admin session expired")
    expected_hash = hashlib.sha256(_admin_token().encode("utf-8")).hexdigest()[:16]
    if payload.get("tok") != expected_hash:
        raise ValueError("admin token rotated")
    return payload


def set_admin_session_cookie(response: Response) -> int:
    token = create_admin_session_token()
    response.set_cookie(
        key=ADMIN_SESSION_COOKIE_NAME,
        value=token,
        max_age=ADMIN_SESSION_TTL_SEC,
        expires=ADMIN_SESSION_TTL_SEC,
        path="/",
        domain=ADMIN_SESSION_COOKIE_DOMAIN,
        secure=ADMIN_SESSION_COOKIE_SECURE,
        httponly=True,
        samesite=ADMIN_SESSION_COOKIE_SAMESITE,
    )
    return ADMIN_SESSION_TTL_SEC


def clear_admin_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=ADMIN_SESSION_COOKIE_NAME,
        path="/",
        domain=ADMIN_SESSION_COOKIE_DOMAIN,
        secure=ADMIN_SESSION_COOKIE_SECURE,
        httponly=True,
        samesite=ADMIN_SESSION_COOKIE_SAMESITE,
    )


def has_valid_admin_session(request: Optional[Request]) -> bool:
    if request is None:
        return False
    raw = (request.cookies.get(ADMIN_SESSION_COOKIE_NAME) or "").strip()
    if not raw:
        return False
    try:
        decode_admin_session_token(raw)
        return True
    except Exception:
        return False


def require_admin_request(request: Optional[Request] = None, x_admin_token: Optional[str] = None) -> None:
    _check_admin_ip(request)
    if has_valid_admin_session(request):
        audit_admin_action(request, "request_cookie")
        return
    # Backward-compatible fallback for scripts/old admin pages. New frontend should use /api/admin-auth/login.
    if verify_admin_token(x_admin_token):
        audit_admin_action(request, "request_header_fallback")
        return
    audit_admin_action(request, "request_forbidden")
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")


def audit_admin_action(request: Optional[Request], action: str, extra: Optional[Dict[str, Any]] = None) -> None:
    try:
        safe_extra = extra or {}
        LOG.warning(
            "admin_audit action=%s ip=%s method=%s path=%s extra=%s",
            action,
            _client_ip(request),
            getattr(request, "method", ""),
            getattr(getattr(request, "url", None), "path", ""),
            json.dumps(safe_extra, ensure_ascii=False, default=str)[:1000],
        )
    except Exception:
        pass
