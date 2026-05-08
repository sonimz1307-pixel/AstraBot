from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from typing import Any, Dict, Optional

from billing_db import ensure_user_row, get_balance, merge_user_balance_records
from db_supabase import supabase, track_user_activity


WORKSPACE_EMAIL_CODE_TTL_MIN = max(3, int(os.getenv("WORKSPACE_EMAIL_CODE_TTL_MIN", "10") or 10))
WORKSPACE_EMAIL_CODE_MAX_ATTEMPTS = max(3, int(os.getenv("WORKSPACE_EMAIL_CODE_MAX_ATTEMPTS", "5") or 5))
WORKSPACE_EMAIL_RESEND_COOLDOWN_SEC = max(15, int(os.getenv("WORKSPACE_EMAIL_RESEND_COOLDOWN_SEC", "60") or 60))
WORKSPACE_EMAIL_PASSWORD_MIN_LEN = max(6, int(os.getenv("WORKSPACE_EMAIL_PASSWORD_MIN_LEN", "6") or 6))
WORKSPACE_PASSWORD_HASH_ITERATIONS = max(120_000, int(os.getenv("WORKSPACE_PASSWORD_HASH_ITERATIONS", "210000") or 210000))
WORKSPACE_EMAIL_CODE_SECRET = (os.getenv("WORKSPACE_EMAIL_CODE_SECRET") or os.getenv("WORKSPACE_AUTH_SECRET") or os.getenv("TELEGRAM_BOT_TOKEN") or "workspace-email-code-secret").strip()

SMTP_HOST = (os.getenv("SMTP_HOST") or "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587") or 587)
SMTP_USER = (os.getenv("SMTP_USER") or "").strip()
SMTP_PASS = (os.getenv("SMTP_PASS") or "").strip()
SMTP_FROM = (os.getenv("SMTP_FROM") or SMTP_USER or "").strip()
SMTP_USE_TLS = str(os.getenv("SMTP_USE_TLS", "true") or "true").strip().lower() not in {"0", "false", "no"}

EMAIL_RE = re.compile(r"^[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}$", re.I)


class WorkspaceAccountError(ValueError):
    pass


class WorkspaceMailerError(RuntimeError):
    pass


class WorkspaceAccountNotFound(WorkspaceAccountError):
    pass


class WorkspaceAuthFailed(WorkspaceAccountError):
    pass


class WorkspaceCodeExpired(WorkspaceAccountError):
    pass


class WorkspaceCodeTooManyAttempts(WorkspaceAccountError):
    pass


def _require_supabase():
    if supabase is None:
        raise RuntimeError("Supabase disabled: SUPABASE_URL / SUPABASE_SERVICE_KEY not set")
    return supabase


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_dt().isoformat()


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def normalize_email(email: str) -> str:
    value = str(email or "").strip().lower()
    if not EMAIL_RE.match(value):
        raise WorkspaceAccountError("Укажи корректный email.")
    return value


def hash_password(password: str) -> str:
    raw = str(password or "")
    if len(raw) < WORKSPACE_EMAIL_PASSWORD_MIN_LEN:
        raise WorkspaceAccountError(f"Пароль должен быть не короче {WORKSPACE_EMAIL_PASSWORD_MIN_LEN} символов.")
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", raw.encode("utf-8"), salt, WORKSPACE_PASSWORD_HASH_ITERATIONS)
    salt_b64 = base64.urlsafe_b64encode(salt).decode("utf-8").rstrip("=")
    digest_b64 = base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")
    return f"pbkdf2_sha256${WORKSPACE_PASSWORD_HASH_ITERATIONS}${salt_b64}${digest_b64}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        scheme, iter_s, salt_b64, digest_b64 = str(stored_hash or "").split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        iterations = int(iter_s)
        salt = base64.urlsafe_b64decode(salt_b64 + "=" * (-len(salt_b64) % 4))
        expected = base64.urlsafe_b64decode(digest_b64 + "=" * (-len(digest_b64) % 4))
    except Exception:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", str(password or "").encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def _hash_code(email: str, code: str) -> str:
    payload = f"{normalize_email(email)}:{str(code or '').strip()}:{WORKSPACE_EMAIL_CODE_SECRET}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _generate_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _send_email_code(email: str, code: str, *, purpose: str) -> None:
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and SMTP_FROM):
        raise WorkspaceMailerError("SMTP не настроен. Добавь SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS и SMTP_FROM.")

    subject = "Код подтверждения AstraBot"
    if purpose == "link_email":
        intro = "Код для подтверждения email"
    elif purpose == "reset_password":
        intro = "Код для сброса пароля AstraBot"
    else:
        intro = "Код для регистрации AstraBot"
    body = (
        f"{intro}: {code}\n\n"
        f"Код действует {WORKSPACE_EMAIL_CODE_TTL_MIN} минут.\n"
        "Если это были не вы, просто проигнорируйте письмо."
    )
    msg = MIMEText(body, _subtype="plain", _charset="utf-8")
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = email

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
            if SMTP_USE_TLS:
                server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_FROM, [email], msg.as_string())
    except Exception as e:
        raise WorkspaceMailerError(f"Не удалось отправить письмо: {e}")


def _select_one(table: str, **filters: Any) -> Optional[Dict[str, Any]]:
    sb = _require_supabase()
    query = sb.table(table).select("*")
    for key, value in filters.items():
        query = query.eq(key, value)
    res = query.limit(1).execute()
    return (getattr(res, "data", None) or [None])[0]


def get_workspace_account_by_id(account_id: int) -> Optional[Dict[str, Any]]:
    return _select_one("workspace_accounts", id=int(account_id))


def get_workspace_account_by_email(email: str) -> Optional[Dict[str, Any]]:
    return _select_one("workspace_accounts", email=normalize_email(email))


def get_workspace_account_by_telegram(telegram_user_id: int) -> Optional[Dict[str, Any]]:
    return _select_one("workspace_accounts", telegram_user_id=int(telegram_user_id))


def _sync_linked_telegram_balance(row: Dict[str, Any]) -> None:
    """Best-effort переносит старый TG-баланс на workspace account после привязки."""
    try:
        account_id = int(row.get("id") or 0)
        linked_tg = row.get("telegram_user_id")
        if linked_tg in (None, ""):
            return
        tg_id = int(linked_tg)
        if account_id > 0 and tg_id > 0 and account_id != tg_id:
            merge_user_balance_records(source_user_id=tg_id, target_user_id=account_id)
    except Exception as exc:
        # Не блокируем вход/привязку аккаунта из-за вспомогательной миграции баланса.
        try:
            print(f"[workspace_account] linked Telegram balance sync skipped: {exc}")
        except Exception:
            pass


def _account_payload_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    account_id = int(row.get("id") or 0)
    linked_tg = row.get("telegram_user_id")
    linked_tg_int = int(linked_tg) if linked_tg not in (None, "") else None
    return {
        "id": account_id,
        "workspace_user_id": account_id,
        "telegram_user_id": account_id,
        "linked_telegram_user_id": linked_tg_int,
        "username": row.get("username"),
        "first_name": row.get("first_name"),
        "last_name": row.get("last_name"),
        "language_code": row.get("language_code"),
        "photo_url": row.get("photo_url"),
        "is_premium": bool(row.get("is_premium", False)),
        "email": row.get("email"),
        "email_verified": bool(row.get("email_verified", False)),
    }


def account_to_workspace_user_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    _sync_linked_telegram_balance(row)
    payload = _account_payload_from_row(row)
    payload["auth_methods"] = []
    if payload.get("linked_telegram_user_id"):
        payload["auth_methods"].append("telegram")
    if payload.get("email"):
        payload["auth_methods"].append("email")
    payload["balance_tokens"] = int(get_balance(int(payload["workspace_user_id"])) or 0)
    return payload


def ensure_workspace_account_from_claims(claims: Dict[str, Any]) -> Dict[str, Any]:
    account_id = int(claims.get("workspace_user_id") or claims.get("telegram_user_id") or claims.get("sub") or 0)
    if account_id <= 0:
        raise WorkspaceAccountError("Invalid workspace account id in token")
    row = get_workspace_account_by_id(account_id)
    if row:
        _sync_linked_telegram_balance(row)
        ensure_user_row(account_id)
        return row

    linked_tg = claims.get("linked_telegram_user_id")
    if linked_tg in (None, "") and not claims.get("workspace_user_id"):
        linked_tg = claims.get("telegram_user_id")
    if linked_tg in (None, ""):
        raise WorkspaceAccountNotFound("Workspace account not found")

    sb = _require_supabase()
    row_payload = {
        "id": account_id,
        "telegram_user_id": int(linked_tg),
        "username": claims.get("username"),
        "first_name": claims.get("first_name"),
        "last_name": claims.get("last_name"),
        "language_code": claims.get("language_code"),
        "photo_url": claims.get("photo_url"),
        "is_premium": bool(claims.get("is_premium", False)),
        "updated_at": _now_iso(),
        "last_login_at": _now_iso(),
    }
    res = sb.table("workspace_accounts").upsert(row_payload, on_conflict="id").execute()
    row = (getattr(res, "data", None) or [None])[0] or get_workspace_account_by_id(account_id)
    if row:
        _sync_linked_telegram_balance(row)
    ensure_user_row(account_id)
    return row


def get_or_create_workspace_account_for_telegram(verified: Dict[str, Any], existing_bot_user: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    tg_id = int(verified["id"])
    row = get_workspace_account_by_telegram(tg_id) or get_workspace_account_by_id(tg_id)

    base_payload = {
        "telegram_user_id": tg_id,
        "username": verified.get("username") or (existing_bot_user or {}).get("username"),
        "first_name": verified.get("first_name") or (existing_bot_user or {}).get("first_name"),
        "last_name": verified.get("last_name") or (existing_bot_user or {}).get("last_name"),
        "language_code": (existing_bot_user or {}).get("language_code"),
        "photo_url": verified.get("photo_url"),
        "is_premium": bool((existing_bot_user or {}).get("is_premium", False)),
        "updated_at": _now_iso(),
        "last_login_at": _now_iso(),
    }

    sb = _require_supabase()
    if row:
        res = sb.table("workspace_accounts").update(base_payload).eq("id", int(row["id"])).execute()
        row = (getattr(res, "data", None) or [None])[0] or get_workspace_account_by_id(int(row["id"]))
    else:
        insert_payload = {"id": tg_id, **base_payload}
        res = sb.table("workspace_accounts").insert(insert_payload).execute()
        row = (getattr(res, "data", None) or [None])[0] or get_workspace_account_by_id(tg_id)

    tg_user = {
        "id": tg_id,
        "username": base_payload.get("username"),
        "first_name": base_payload.get("first_name"),
        "last_name": base_payload.get("last_name"),
        "language_code": base_payload.get("language_code"),
        "photo_url": base_payload.get("photo_url"),
        "is_premium": base_payload.get("is_premium"),
    }
    track_user_activity(tg_user)
    if row:
        _sync_linked_telegram_balance(row)
    ensure_user_row(int(row["id"]))
    return row


def _latest_active_email_code(*, email: str, purpose: str, account_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    sb = _require_supabase()
    query = (
        sb.table("workspace_email_codes")
        .select("*")
        .eq("email", normalize_email(email))
        .eq("purpose", purpose)
        .is_("used_at", "null")
        .order("created_at", desc=True)
    )
    if account_id is None:
        query = query.is_("account_id", "null")
    else:
        query = query.eq("account_id", int(account_id))
    res = query.limit(1).execute()
    return (getattr(res, "data", None) or [None])[0]


def _check_resend_cooldown(row: Optional[Dict[str, Any]]) -> None:
    if not row:
        return
    created_at = _parse_dt(row.get("created_at"))
    if not created_at:
        return
    elapsed = (_now_dt() - created_at).total_seconds()
    if elapsed < WORKSPACE_EMAIL_RESEND_COOLDOWN_SEC:
        remain = max(1, int(WORKSPACE_EMAIL_RESEND_COOLDOWN_SEC - elapsed + 0.999))
        raise WorkspaceAccountError(f"Повторно отправить код можно через {remain} сек.")


def _create_email_code_record(*, account_id: Optional[int], email: str, password_hash: str, purpose: str) -> None:
    sb = _require_supabase()
    code = _generate_code()
    code_hash = _hash_code(email, code)
    expires_at = (_now_dt() + timedelta(minutes=WORKSPACE_EMAIL_CODE_TTL_MIN)).isoformat()
    payload = {
        "account_id": int(account_id) if account_id is not None else None,
        "email": normalize_email(email),
        "password_hash": password_hash,
        "code_hash": code_hash,
        "purpose": purpose,
        "expires_at": expires_at,
        "attempts": 0,
        "used_at": None,
        "created_at": _now_iso(),
    }
    sb.table("workspace_email_codes").insert(payload).execute()
    _send_email_code(email, code, purpose=purpose)


def start_email_registration(email: str, password: str) -> None:
    normalized = normalize_email(email)
    if get_workspace_account_by_email(normalized):
        raise WorkspaceAccountError("Этот email уже используется.")
    password_hash = hash_password(password)
    latest = _latest_active_email_code(email=normalized, purpose="register", account_id=None)
    _check_resend_cooldown(latest)
    _create_email_code_record(account_id=None, email=normalized, password_hash=password_hash, purpose="register")


def _validate_email_code(*, email: str, code: str, purpose: str, account_id: Optional[int]) -> Dict[str, Any]:
    sb = _require_supabase()
    row = _latest_active_email_code(email=email, purpose=purpose, account_id=account_id)
    if not row:
        raise WorkspaceAccountError("Код не найден. Сначала отправь код заново.")

    attempts = int(row.get("attempts") or 0)
    if attempts >= WORKSPACE_EMAIL_CODE_MAX_ATTEMPTS:
        raise WorkspaceCodeTooManyAttempts("Лимит попыток исчерпан. Запроси новый код.")

    expires_at = _parse_dt(row.get("expires_at"))
    if not expires_at or expires_at <= _now_dt():
        raise WorkspaceCodeExpired("Код истёк. Запроси новый код.")

    if not hmac.compare_digest(str(row.get("code_hash") or ""), _hash_code(email, code)):
        sb.table("workspace_email_codes").update({"attempts": attempts + 1}).eq("id", row["id"]).execute()
        raise WorkspaceAccountError("Неверный код подтверждения.")

    sb.table("workspace_email_codes").update({"used_at": _now_iso()}).eq("id", row["id"]).execute()
    return row


def confirm_email_registration(email: str, code: str) -> Dict[str, Any]:
    normalized = normalize_email(email)
    if get_workspace_account_by_email(normalized):
        raise WorkspaceAccountError("Этот email уже используется.")

    row = _validate_email_code(email=normalized, code=code, purpose="register", account_id=None)
    sb = _require_supabase()
    res = sb.table("workspace_accounts").insert(
        {
            "email": normalized,
            "email_verified": True,
            "password_hash": row.get("password_hash"),
            "updated_at": _now_iso(),
            "last_login_at": _now_iso(),
        }
    ).execute()
    account = (getattr(res, "data", None) or [None])[0]
    if not account:
        account = get_workspace_account_by_email(normalized)
    if not account:
        raise WorkspaceAccountError("Не удалось создать аккаунт")
    ensure_user_row(int(account["id"]))
    return account




def start_password_reset(email: str) -> None:
    normalized = normalize_email(email)
    account = get_workspace_account_by_email(normalized)
    if not account:
        raise WorkspaceAccountError("Аккаунт с таким email не найден.")
    if not bool(account.get("email_verified", False)):
        raise WorkspaceAccountError("Email ещё не подтверждён.")
    latest = _latest_active_email_code(email=normalized, purpose="reset_password", account_id=int(account["id"]))
    _check_resend_cooldown(latest)
    _create_email_code_record(account_id=int(account["id"]), email=normalized, password_hash="reset_pending", purpose="reset_password")


def confirm_password_reset(email: str, code: str, new_password: str) -> Dict[str, Any]:
    normalized = normalize_email(email)
    account = get_workspace_account_by_email(normalized)
    if not account:
        raise WorkspaceAccountError("Аккаунт с таким email не найден.")
    row = _validate_email_code(email=normalized, code=code, purpose="reset_password", account_id=int(account["id"]))
    _ = row
    password_hash = hash_password(new_password)
    sb = _require_supabase()
    res = sb.table("workspace_accounts").update(
        {
            "password_hash": password_hash,
            "updated_at": _now_iso(),
            "last_login_at": _now_iso(),
        }
    ).eq("id", int(account["id"])).execute()
    out = (getattr(res, "data", None) or [None])[0] or get_workspace_account_by_id(int(account["id"]))
    if not out:
        raise WorkspaceAccountError("Не удалось обновить пароль.")
    ensure_user_row(int(out["id"]))
    return out


def change_password(*, account_id: int, current_password: str, new_password: str) -> Dict[str, Any]:
    account = get_workspace_account_by_id(account_id)
    if not account:
        raise WorkspaceAccountNotFound("Аккаунт не найден")
    if not account.get("email") or not bool(account.get("email_verified", False)):
        raise WorkspaceAccountError("Сначала привяжи и подтверди email.")
    stored_hash = str(account.get("password_hash") or "")
    if not stored_hash or not verify_password(current_password, stored_hash):
        raise WorkspaceAuthFailed("Текущий пароль введён неверно.")
    if str(current_password or "") == str(new_password or ""):
        raise WorkspaceAccountError("Новый пароль должен отличаться от текущего.")
    password_hash = hash_password(new_password)
    sb = _require_supabase()
    res = sb.table("workspace_accounts").update(
        {
            "password_hash": password_hash,
            "updated_at": _now_iso(),
        }
    ).eq("id", int(account_id)).execute()
    out = (getattr(res, "data", None) or [None])[0] or get_workspace_account_by_id(account_id)
    if not out:
        raise WorkspaceAccountError("Не удалось обновить пароль.")
    return out

def start_link_email(*, account_id: int, email: str, password: str) -> None:
    account = get_workspace_account_by_id(account_id)
    if not account:
        raise WorkspaceAccountNotFound("Аккаунт не найден")
    normalized = normalize_email(email)
    existing = get_workspace_account_by_email(normalized)
    if existing and int(existing.get("id") or 0) != int(account_id):
        raise WorkspaceAccountError("Этот email уже используется в другом аккаунте.")
    password_hash = hash_password(password)
    latest = _latest_active_email_code(email=normalized, purpose="link_email", account_id=account_id)
    _check_resend_cooldown(latest)
    _create_email_code_record(account_id=account_id, email=normalized, password_hash=password_hash, purpose="link_email")


def confirm_link_email(*, account_id: int, email: str, code: str) -> Dict[str, Any]:
    account = get_workspace_account_by_id(account_id)
    if not account:
        raise WorkspaceAccountNotFound("Аккаунт не найден")
    normalized = normalize_email(email)
    existing = get_workspace_account_by_email(normalized)
    if existing and int(existing.get("id") or 0) != int(account_id):
        raise WorkspaceAccountError("Этот email уже используется в другом аккаунте.")

    row = _validate_email_code(email=normalized, code=code, purpose="link_email", account_id=account_id)
    sb = _require_supabase()
    res = sb.table("workspace_accounts").update(
        {
            "email": normalized,
            "email_verified": True,
            "password_hash": row.get("password_hash"),
            "updated_at": _now_iso(),
        }
    ).eq("id", int(account_id)).execute()
    out = (getattr(res, "data", None) or [None])[0] or get_workspace_account_by_id(account_id)
    return out


def login_with_email(email: str, password: str) -> Dict[str, Any]:
    account = get_workspace_account_by_email(email)
    if not account:
        raise WorkspaceAuthFailed("Аккаунт с таким email не найден.")
    if not bool(account.get("email_verified", False)):
        raise WorkspaceAuthFailed("Email ещё не подтверждён.")
    stored_hash = str(account.get("password_hash") or "")
    if not stored_hash or not verify_password(password, stored_hash):
        raise WorkspaceAuthFailed("Неверный email или пароль.")

    sb = _require_supabase()
    sb.table("workspace_accounts").update({"last_login_at": _now_iso()}).eq("id", int(account["id"])).execute()
    fresh = get_workspace_account_by_id(int(account["id"])) or account
    _sync_linked_telegram_balance(fresh)
    ensure_user_row(int(fresh["id"]))
    return fresh


def link_telegram_to_account(*, account_id: int, verified: Dict[str, Any]) -> Dict[str, Any]:
    account = get_workspace_account_by_id(account_id)
    if not account:
        raise WorkspaceAccountNotFound("Аккаунт не найден")
    tg_id = int(verified["id"])
    existing_tg = get_workspace_account_by_telegram(tg_id)
    if existing_tg and int(existing_tg.get("id") or 0) != int(account_id):
        raise WorkspaceAccountError("Этот Telegram уже привязан к другому аккаунту.")

    payload = {
        "telegram_user_id": tg_id,
        "username": verified.get("username") or account.get("username"),
        "first_name": verified.get("first_name") or account.get("first_name"),
        "last_name": verified.get("last_name") or account.get("last_name"),
        "photo_url": verified.get("photo_url") or account.get("photo_url"),
        "updated_at": _now_iso(),
        "last_login_at": _now_iso(),
    }
    sb = _require_supabase()
    res = sb.table("workspace_accounts").update(payload).eq("id", int(account_id)).execute()
    out = (getattr(res, "data", None) or [None])[0] or get_workspace_account_by_id(account_id)
    if out:
        _sync_linked_telegram_balance(out)

    track_user_activity(
        {
            "id": tg_id,
            "username": payload.get("username"),
            "first_name": payload.get("first_name"),
            "last_name": payload.get("last_name"),
            "photo_url": payload.get("photo_url"),
            "is_premium": account.get("is_premium"),
            "language_code": account.get("language_code"),
        }
    )
    ensure_user_row(int(account_id))
    return out
