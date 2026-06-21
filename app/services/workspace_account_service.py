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
from subscriptions_db import merge_user_subscription_records


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


def get_workspace_account_by_google_sub(google_sub: str) -> Optional[Dict[str, Any]]:
    value = str(google_sub or "").strip()
    if not value:
        return None
    return _select_one("workspace_accounts", google_sub=value)


def get_workspace_account_by_max_user_id(max_user_id: int) -> Optional[Dict[str, Any]]:
    try:
        value = int(max_user_id or 0)
    except Exception:
        value = 0
    if value <= 0:
        return None
    return _select_one("workspace_accounts", max_user_id=value)


def _sync_linked_telegram_balance(row: Dict[str, Any]) -> None:
    """Best-effort переносит старые TG-баланс и тариф на workspace account после привязки."""
    try:
        account_id = int(row.get("id") or 0)
        linked_tg = row.get("telegram_user_id")
        if linked_tg in (None, ""):
            return
        tg_id = int(linked_tg)
        if account_id <= 0 or tg_id <= 0 or account_id == tg_id:
            return
    except Exception:
        return

    try:
        merge_user_balance_records(source_user_id=tg_id, target_user_id=account_id)
    except Exception as exc:
        # Не блокируем вход/привязку аккаунта из-за вспомогательной миграции баланса.
        try:
            print(f"[workspace_account] linked Telegram balance sync skipped: {exc}")
        except Exception:
            pass

    try:
        merge_user_subscription_records(source_user_id=tg_id, target_user_id=account_id)
    except Exception as exc:
        # Не блокируем вход/привязку аккаунта из-за вспомогательной миграции тарифа.
        try:
            print(f"[workspace_account] linked Telegram subscription sync skipped: {exc}")
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
        "has_password": bool(row.get("password_hash")),
        "google_sub": row.get("google_sub"),
        "google_email": row.get("google_email"),
        "google_email_verified": bool(row.get("google_email_verified", False)),
        "google_name": row.get("google_name"),
        "google_picture": row.get("google_picture"),
        "max_user_id": row.get("max_user_id"),
        "max_username": row.get("max_username"),
        "max_first_name": row.get("max_first_name"),
        "max_last_name": row.get("max_last_name"),
        "max_photo_url": row.get("max_photo_url"),
    }


def account_to_workspace_user_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    _sync_linked_telegram_balance(row)
    payload = _account_payload_from_row(row)
    payload["auth_methods"] = []
    if payload.get("linked_telegram_user_id"):
        payload["auth_methods"].append("telegram")
    if payload.get("email"):
        payload["auth_methods"].append("email")
    if payload.get("google_sub"):
        payload["auth_methods"].append("google")
    if payload.get("max_user_id"):
        payload["auth_methods"].append("max")
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


def get_or_create_workspace_account_for_google(verified: Dict[str, Any], *, current_account_id: Optional[int] = None) -> Dict[str, Any]:
    google_sub = str(verified.get("sub") or "").strip()
    if not google_sub:
        raise WorkspaceAccountError("Google account id is missing")

    email = normalize_email(str(verified.get("email") or ""))
    sb = _require_supabase()

    existing_by_google = get_workspace_account_by_google_sub(google_sub)
    existing_by_email = get_workspace_account_by_email(email)

    if current_account_id is not None:
        account = get_workspace_account_by_id(int(current_account_id))
        if not account:
            raise WorkspaceAccountNotFound("Аккаунт не найден")
        current_id = int(current_account_id)
        if existing_by_google and int(existing_by_google.get("id") or 0) != current_id:
            # Пользователь мог сначала создать отдельный Google-аккаунт,
            # а потом войти через Telegram и нажать «Привязать Google».
            # Если текущий аккаунт уже Telegram-аккаунт, объединяем в него
            # временную Google-строку вместо ошибки.
            linked_tg = account.get("telegram_user_id")
            if linked_tg not in (None, ""):
                merged = _merge_current_account_into_existing_telegram_account(
                    current_account=existing_by_google,
                    target_account=account,
                    verified={
                        "id": int(linked_tg),
                        "username": account.get("username"),
                        "first_name": account.get("first_name"),
                        "last_name": account.get("last_name"),
                        "photo_url": account.get("photo_url"),
                    },
                )
                return merged
            raise WorkspaceAccountError("Этот Google уже привязан к другому аккаунту.")
        if existing_by_email and int(existing_by_email.get("id") or 0) != current_id:
            raise WorkspaceAccountError("Email этого Google уже используется в другом аккаунте.")
        row = account
    else:
        # google_sub — главный стабильный идентификатор Google. Если он уже найден,
        # не блокируем вход из-за старой временной строки с таким же email.
        row = existing_by_google or existing_by_email

    payload = {
        "google_sub": google_sub,
        "google_email": email,
        "google_email_verified": bool(verified.get("email_verified", True)),
        "google_name": verified.get("name"),
        "google_picture": verified.get("photo_url"),
        "updated_at": _now_iso(),
        "last_login_at": _now_iso(),
    }

    # Если аккаунт создаётся/заходит через Google, считаем email подтверждённым Google.
    # Пароль не задаём и существующий пароль не трогаем.
    if not row or not row.get("email"):
        payload["email"] = email
        payload["email_verified"] = True
    elif str(row.get("email") or "").strip().lower() == email and not bool(row.get("email_verified", False)):
        payload["email_verified"] = True

    if verified.get("first_name") and not (row or {}).get("first_name"):
        payload["first_name"] = verified.get("first_name")
    if verified.get("last_name") and not (row or {}).get("last_name"):
        payload["last_name"] = verified.get("last_name")
    if verified.get("photo_url") and not (row or {}).get("photo_url"):
        payload["photo_url"] = verified.get("photo_url")
    if verified.get("locale") and not (row or {}).get("language_code"):
        payload["language_code"] = verified.get("locale")

    if row:
        current_google = str(row.get("google_sub") or "").strip()
        if current_google and current_google != google_sub:
            raise WorkspaceAccountError("К этому аккаунту уже привязан другой Google.")
        res = sb.table("workspace_accounts").update(payload).eq("id", int(row["id"])).execute()
        out = (getattr(res, "data", None) or [None])[0] or get_workspace_account_by_id(int(row["id"]))
    else:
        res = sb.table("workspace_accounts").insert(payload).execute()
        out = (getattr(res, "data", None) or [None])[0] or get_workspace_account_by_google_sub(google_sub) or get_workspace_account_by_email(email)

    if not out:
        raise WorkspaceAccountError("Не удалось создать или обновить Google-аккаунт")

    _sync_linked_telegram_balance(out)
    ensure_user_row(int(out["id"]))
    return out



def get_or_create_workspace_account_for_max(verified: Dict[str, Any], *, current_account_id: Optional[int] = None) -> Dict[str, Any]:
    try:
        max_user_id = int(verified.get("id") or 0)
    except Exception:
        max_user_id = 0
    if max_user_id <= 0:
        raise WorkspaceAccountError("MAX account id is missing")

    sb = _require_supabase()
    existing_by_max = get_workspace_account_by_max_user_id(max_user_id)

    if current_account_id is not None:
        row = get_workspace_account_by_id(int(current_account_id))
        if not row:
            raise WorkspaceAccountNotFound("Аккаунт не найден")
        if existing_by_max and int(existing_by_max.get("id") or 0) != int(current_account_id):
            return _merge_existing_max_account_into_current_account(
                max_account=existing_by_max,
                target_account=row,
                verified=verified,
            )
    else:
        row = existing_by_max

    payload = {
        "max_user_id": max_user_id,
        "max_username": verified.get("username"),
        "max_first_name": verified.get("first_name"),
        "max_last_name": verified.get("last_name"),
        "max_photo_url": verified.get("photo_url"),
        "max_linked_at": _now_iso(),
        "updated_at": _now_iso(),
        "last_login_at": _now_iso(),
    }

    # MAX can create a standalone site account. Fill common profile fields only
    # when they are empty, so email/Google/Telegram profile data is not overwritten.
    if not row or not row.get("username"):
        payload["username"] = verified.get("username")
    if not row or not row.get("first_name"):
        payload["first_name"] = verified.get("first_name")
    if not row or not row.get("last_name"):
        payload["last_name"] = verified.get("last_name")
    if not row or not row.get("photo_url"):
        payload["photo_url"] = verified.get("photo_url")
    if not row or not row.get("language_code"):
        payload["language_code"] = verified.get("language_code")

    if row:
        current_max = row.get("max_user_id")
        if current_max not in (None, "") and int(current_max) != max_user_id:
            raise WorkspaceAccountError("К этому аккаунту уже привязан другой MAX.")
        res = sb.table("workspace_accounts").update(payload).eq("id", int(row["id"])).execute()
        out = (getattr(res, "data", None) or [None])[0] or get_workspace_account_by_id(int(row["id"]))
    else:
        res = sb.table("workspace_accounts").insert(payload).execute()
        out = (getattr(res, "data", None) or [None])[0] or get_workspace_account_by_max_user_id(max_user_id)

    if not out:
        raise WorkspaceAccountError("Не удалось создать или обновить MAX-аккаунт")

    ensure_user_row(int(out["id"]))
    return out




def _is_safe_standalone_max_account(row: Dict[str, Any]) -> bool:
    """Return True only for a temporary account that was created by MAX login alone."""
    if not row:
        return False
    if row.get("telegram_user_id") not in (None, ""):
        return False
    if row.get("email") not in (None, ""):
        return False
    if row.get("google_sub") not in (None, ""):
        return False
    if row.get("password_hash") not in (None, ""):
        return False
    return row.get("max_user_id") not in (None, "")

def _merge_existing_max_account_into_current_account(
    *,
    max_account: Dict[str, Any],
    target_account: Dict[str, Any],
    verified: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Сценарий: пользователь случайно вошёл через MAX и получил отдельный workspace-аккаунт,
    затем вошёл в свой старый аккаунт и нажал «Привязать MAX».
    Целевым оставляем текущий аккаунт сайта, MAX-связку переносим на него,
    баланс/тариф временного MAX-аккаунта объединяем в текущий.
    """
    sb = _require_supabase()
    max_user_id = int(verified.get("id") or 0)
    source_id = int(max_account.get("id") or 0)
    target_id = int(target_account.get("id") or 0)
    if max_user_id <= 0 or source_id <= 0 or target_id <= 0:
        raise WorkspaceAccountError("Не удалось определить аккаунты для привязки MAX.")
    if source_id == target_id:
        return target_account

    if not _is_safe_standalone_max_account(max_account):
        raise WorkspaceAccountError(
            "Этот MAX уже привязан к другому полноценному аккаунту. "
            "Автоматически переносить такую связку небезопасно — обратись в поддержку."
        )

    current_target_max = target_account.get("max_user_id")
    if current_target_max not in (None, "") and int(current_target_max) != max_user_id:
        raise WorkspaceAccountError("К этому аккаунту уже привязан другой MAX.")

    # Сначала освобождаем unique max_user_id на старой строке. Старую строку не удаляем:
    # на неё могут ссылаться истории генераций, ledger и другие таблицы.
    clear_payload = {
        "max_user_id": None,
        "max_username": None,
        "max_first_name": None,
        "max_last_name": None,
        "max_photo_url": None,
        "max_linked_at": None,
        "updated_at": _now_iso(),
    }
    restore_payload = {
        "max_user_id": max_account.get("max_user_id"),
        "max_username": max_account.get("max_username"),
        "max_first_name": max_account.get("max_first_name"),
        "max_last_name": max_account.get("max_last_name"),
        "max_photo_url": max_account.get("max_photo_url"),
        "max_linked_at": max_account.get("max_linked_at"),
        "updated_at": _now_iso(),
    }
    try:
        sb.table("workspace_accounts").update(clear_payload).eq("id", source_id).execute()
    except Exception as exc:
        raise WorkspaceAccountError(f"Не удалось подготовить MAX-аккаунт к привязке: {exc}")

    payload = {
        "max_user_id": max_user_id,
        "max_username": verified.get("username"),
        "max_first_name": verified.get("first_name"),
        "max_last_name": verified.get("last_name"),
        "max_photo_url": verified.get("photo_url"),
        "max_linked_at": _now_iso(),
        "updated_at": _now_iso(),
        "last_login_at": _now_iso(),
    }
    if verified.get("username") and not target_account.get("username"):
        payload["username"] = verified.get("username")
    if verified.get("first_name") and not target_account.get("first_name"):
        payload["first_name"] = verified.get("first_name")
    if verified.get("last_name") and not target_account.get("last_name"):
        payload["last_name"] = verified.get("last_name")
    if verified.get("photo_url") and not target_account.get("photo_url"):
        payload["photo_url"] = verified.get("photo_url")
    if verified.get("language_code") and not target_account.get("language_code"):
        payload["language_code"] = verified.get("language_code")

    try:
        res = sb.table("workspace_accounts").update(payload).eq("id", target_id).execute()
    except Exception as exc:
        try:
            sb.table("workspace_accounts").update(restore_payload).eq("id", source_id).execute()
        except Exception as restore_exc:
            try:
                print(f"[workspace_account] failed to restore MAX account after link error: {restore_exc}")
            except Exception:
                pass
        raise WorkspaceAccountError(f"Не удалось привязать MAX к текущему аккаунту: {exc}")

    out = (getattr(res, "data", None) or [None])[0] or get_workspace_account_by_id(target_id)
    if not out:
        raise WorkspaceAccountError("Не удалось привязать MAX к текущему аккаунту.")

    try:
        merge_user_balance_records(source_user_id=source_id, target_user_id=target_id)
    except Exception as exc:
        try:
            print(f"[workspace_account] MAX account balance merge skipped: {exc}")
        except Exception:
            pass

    try:
        merge_user_subscription_records(source_user_id=source_id, target_user_id=target_id)
    except Exception as exc:
        try:
            print(f"[workspace_account] MAX account subscription merge skipped: {exc}")
        except Exception:
            pass

    ensure_user_row(target_id)
    return get_workspace_account_by_id(target_id) or out


def _merge_current_account_into_existing_telegram_account(
    *,
    current_account: Dict[str, Any],
    target_account: Dict[str, Any],
    verified: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Сценарий: пользователь уже входил через Telegram, потом вошёл через Google/email,
    и теперь привязывает тот же Telegram. Вместо ошибки объединяем аккаунты:
    целевым оставляем аккаунт, где уже стоит telegram_user_id, чтобы старый TG-баланс
    и Telegram-идентичность не потерялись.
    """
    sb = _require_supabase()
    tg_id = int(verified["id"])
    current_id = int(current_account.get("id") or 0)
    target_id = int(target_account.get("id") or 0)
    if current_id <= 0 or target_id <= 0:
        raise WorkspaceAccountError("Не удалось определить аккаунты для объединения.")
    if current_id == target_id:
        return target_account

    current_google_sub = str(current_account.get("google_sub") or "").strip()
    target_google_sub = str(target_account.get("google_sub") or "").strip()
    if target_google_sub and current_google_sub and target_google_sub != current_google_sub:
        raise WorkspaceAccountError("Этот Telegram уже привязан к аккаунту с другим Google.")

    def _normalize_optional_email(value: Any) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        return normalize_email(raw)

    current_email = _normalize_optional_email(current_account.get("email") or current_account.get("google_email"))
    target_email = _normalize_optional_email(target_account.get("email"))
    can_move_email = bool(current_email) and (not target_email or target_email == current_email)
    should_clear_current_email = bool(current_google_sub) or can_move_email

    # Сначала освобождаем уникальные email/google поля на временном аккаунте,
    # иначе обновление целевого аккаунта упрётся в unique index. Старую строку не удаляем:
    # на неё могут ссылаться истории генераций/ledger.
    clear_payload = {
        "google_sub": None,
        "google_email": None,
        "google_email_verified": False,
        "google_name": None,
        "google_picture": None,
        "updated_at": _now_iso(),
    }
    if should_clear_current_email:
        clear_payload["email"] = None
        clear_payload["email_verified"] = False
        clear_payload["password_hash"] = None
    restore_payload = {
        "google_sub": current_account.get("google_sub"),
        "google_email": current_account.get("google_email"),
        "google_email_verified": bool(current_account.get("google_email_verified", False)),
        "google_name": current_account.get("google_name"),
        "google_picture": current_account.get("google_picture"),
        "email": current_account.get("email"),
        "email_verified": bool(current_account.get("email_verified", False)),
        "password_hash": current_account.get("password_hash"),
        "updated_at": _now_iso(),
    }
    try:
        sb.table("workspace_accounts").update(clear_payload).eq("id", current_id).execute()
    except Exception as exc:
        raise WorkspaceAccountError(f"Не удалось подготовить аккаунт к объединению: {exc}")

    payload = {
        "telegram_user_id": tg_id,
        "username": verified.get("username") or target_account.get("username") or current_account.get("username"),
        "first_name": verified.get("first_name") or target_account.get("first_name") or current_account.get("first_name"),
        "last_name": verified.get("last_name") or target_account.get("last_name") or current_account.get("last_name"),
        "photo_url": verified.get("photo_url") or target_account.get("photo_url") or current_account.get("photo_url"),
        "updated_at": _now_iso(),
        "last_login_at": _now_iso(),
    }

    if current_google_sub and not target_google_sub:
        payload["google_sub"] = current_google_sub
    if current_account.get("google_email") or current_email:
        payload["google_email"] = current_account.get("google_email") or current_email
        payload["google_email_verified"] = bool(
            current_account.get("google_email_verified") or current_account.get("email_verified")
        )
    if current_account.get("google_name") and not target_account.get("google_name"):
        payload["google_name"] = current_account.get("google_name")
    if current_account.get("google_picture") and not target_account.get("google_picture"):
        payload["google_picture"] = current_account.get("google_picture")

    if can_move_email and not target_email:
        payload["email"] = current_email
        payload["email_verified"] = bool(current_account.get("email_verified") or current_account.get("google_email_verified"))
    if can_move_email and current_account.get("password_hash") and not target_account.get("password_hash"):
        payload["password_hash"] = current_account.get("password_hash")

    try:
        res = sb.table("workspace_accounts").update(payload).eq("id", target_id).execute()
    except Exception as exc:
        try:
            sb.table("workspace_accounts").update(restore_payload).eq("id", current_id).execute()
        except Exception as restore_exc:
            try:
                print(f"[workspace_account] failed to restore current account after merge error: {restore_exc}")
            except Exception:
                pass
        raise WorkspaceAccountError(f"Не удалось объединить аккаунты: {exc}")

    out = (getattr(res, "data", None) or [None])[0] or get_workspace_account_by_id(target_id)
    if not out:
        raise WorkspaceAccountError("Не удалось объединить аккаунты.")

    # Переносим баланс и тариф временного Google/email аккаунта в целевой Telegram-аккаунт.
    # Старый чистый Telegram-баланс/тариф также подтянется через _sync_linked_telegram_balance.
    try:
        merge_user_balance_records(source_user_id=current_id, target_user_id=target_id)
    except Exception as exc:
        try:
            print(f"[workspace_account] current account balance merge skipped: {exc}")
        except Exception:
            pass

    try:
        merge_user_subscription_records(source_user_id=current_id, target_user_id=target_id)
    except Exception as exc:
        try:
            print(f"[workspace_account] current account subscription merge skipped: {exc}")
        except Exception:
            pass

    _sync_linked_telegram_balance(out)
    ensure_user_row(target_id)
    track_user_activity(
        {
            "id": tg_id,
            "username": payload.get("username"),
            "first_name": payload.get("first_name"),
            "last_name": payload.get("last_name"),
            "photo_url": payload.get("photo_url"),
            "is_premium": target_account.get("is_premium") or current_account.get("is_premium"),
            "language_code": target_account.get("language_code") or current_account.get("language_code"),
        }
    )
    return get_workspace_account_by_id(target_id) or out


def link_telegram_to_account(*, account_id: int, verified: Dict[str, Any]) -> Dict[str, Any]:
    account = get_workspace_account_by_id(account_id)
    if not account:
        raise WorkspaceAccountNotFound("Аккаунт не найден")
    tg_id = int(verified["id"])
    existing_tg = get_workspace_account_by_telegram(tg_id)
    if existing_tg and int(existing_tg.get("id") or 0) != int(account_id):
        return _merge_current_account_into_existing_telegram_account(
            current_account=account,
            target_account=existing_tg,
            verified=verified,
        )

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
    ensure_user_row(int(out["id"]))
    return out
