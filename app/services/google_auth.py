from __future__ import annotations

import os
from typing import Any, Dict

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token


GOOGLE_CLIENT_ID = (os.getenv("GOOGLE_CLIENT_ID") or "").strip()


class GoogleAuthError(ValueError):
    pass


def get_google_client_id() -> str:
    if not GOOGLE_CLIENT_ID:
        raise GoogleAuthError("GOOGLE_CLIENT_ID is not configured")
    return GOOGLE_CLIENT_ID


def verify_google_id_token(credential: str) -> Dict[str, Any]:
    raw = str(credential or "").strip()
    if not raw:
        raise GoogleAuthError("Missing Google credential")

    client_id = get_google_client_id()
    try:
        info = id_token.verify_oauth2_token(raw, google_requests.Request(), client_id)
    except Exception as exc:
        raise GoogleAuthError(f"Invalid Google credential: {exc}")

    issuer = str(info.get("iss") or "").strip()
    if issuer not in {"accounts.google.com", "https://accounts.google.com"}:
        raise GoogleAuthError("Invalid Google token issuer")

    sub = str(info.get("sub") or "").strip()
    if not sub:
        raise GoogleAuthError("Google token has no subject")

    email = str(info.get("email") or "").strip().lower()
    email_verified = bool(info.get("email_verified", False))
    if not email or not email_verified:
        raise GoogleAuthError("Google email is not verified")

    return {
        "sub": sub,
        "email": email,
        "email_verified": email_verified,
        "name": str(info.get("name") or "").strip() or None,
        "first_name": str(info.get("given_name") or "").strip() or None,
        "last_name": str(info.get("family_name") or "").strip() or None,
        "photo_url": str(info.get("picture") or "").strip() or None,
        "locale": str(info.get("locale") or "").strip() or None,
    }
