# free_plan_limits.py
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from db_supabase import supabase
from subscriptions_db import get_current_subscription

FREE_CHAT_DAILY_LIMIT = int(os.getenv("FREE_CHAT_DAILY_LIMIT", "20") or "20")
FREE_TTS_DAILY_LIMIT = int(os.getenv("FREE_TTS_DAILY_LIMIT", "3") or "3")
FREE_TTS_MAX_CHARS = int(os.getenv("FREE_TTS_MAX_CHARS", "500") or "500")
FREE_PROMPT_LIFETIME_LIMIT = int(os.getenv("FREE_PROMPT_LIFETIME_LIMIT", "5") or "5")
FREE_LIMIT_TIMEZONE = (os.getenv("FREE_LIMIT_TIMEZONE", "Europe/Moscow") or "Europe/Moscow").strip()

FEATURE_CHAT = "chat"
FEATURE_TTS = "tts"


@dataclass(frozen=True)
class FreeLimitResult:
    allowed: bool
    feature: str
    limit: int
    used: int
    remaining: int
    usage_date: str
    plan_code: str = "free"
    is_paid_plan: bool = False
    reason: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "allowed": bool(self.allowed),
            "feature": self.feature,
            "limit": int(self.limit),
            "used": int(self.used),
            "remaining": int(self.remaining),
            "usage_date": self.usage_date,
            "plan_code": self.plan_code,
            "is_paid_plan": bool(self.is_paid_plan),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class FreePromptOpenResult:
    allowed: bool
    limit: int
    used: int
    remaining: int
    prompt_id: str = ""
    already_opened: bool = False
    plan_code: str = "free"
    is_paid_plan: bool = False
    reason: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "allowed": bool(self.allowed),
            "feature": "prompt_open",
            "limit": int(self.limit),
            "used": int(self.used),
            "remaining": int(self.remaining),
            "prompt_id": self.prompt_id,
            "already_opened": bool(self.already_opened),
            "plan_code": self.plan_code,
            "is_paid_plan": bool(self.is_paid_plan),
            "reason": self.reason,
        }


class FreePlanLimitError(RuntimeError):
    """Raised when a Free-plan action is blocked by a limit or validation rule."""

    def __init__(self, message: str, *, result: Optional[Any] = None, code: str = "free_limit_exceeded"):
        super().__init__(message)
        self.message = message
        self.result = result
        self.code = code


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return int(default)


def _today_key() -> str:
    try:
        tz = ZoneInfo(FREE_LIMIT_TIMEZONE)
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.now(tz).date().isoformat()


def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _canonical_user_id(user_id: Any) -> int:
    """Use the same canonical billing ID as balances/subscriptions.

    This prevents a linked site account and Telegram account from receiving
    separate Free limits.
    """
    uid = _safe_int(user_id)
    if uid <= 0:
        return 0
    try:
        from billing_db import resolve_billing_user_id

        resolved = _safe_int(resolve_billing_user_id(uid), uid)
        return resolved if resolved > 0 else uid
    except Exception:
        return uid


def _active_plan_code(user_id: Any) -> str:
    uid = _canonical_user_id(user_id)
    if uid <= 0:
        return "free"
    try:
        sub = get_current_subscription(uid)
        code = str(sub.get("plan_code") or "free").strip().lower() or "free"
        if bool(sub.get("is_active")) and code and code != "free":
            return code
    except Exception:
        pass
    return "free"


def is_free_plan_user(user_id: Any) -> bool:
    return _active_plan_code(user_id) == "free"


def _limit_for_feature(feature: str) -> int:
    key = str(feature or "").strip().lower()
    if key == FEATURE_CHAT:
        return max(0, FREE_CHAT_DAILY_LIMIT)
    if key == FEATURE_TTS:
        return max(0, FREE_TTS_DAILY_LIMIT)
    raise ValueError(f"Unknown Free-plan feature: {feature}")


def _read_usage(user_id: int, feature: str, usage_date: str) -> int:
    if supabase is None:
        return 0
    res = (
        supabase.table("free_plan_usage")
        .select("used_count")
        .eq("user_id", int(user_id))
        .eq("usage_date", usage_date)
        .eq("feature", feature)
        .limit(1)
        .execute()
    )
    rows = list(getattr(res, "data", None) or [])
    if not rows:
        return 0
    return max(0, _safe_int(rows[0].get("used_count"), 0))


def _write_usage(user_id: int, feature: str, usage_date: str, used_count: int) -> None:
    """Non-atomic fallback for local/dev environments only."""
    if supabase is None:
        return
    now_iso = _now_iso()
    supabase.table("free_plan_usage").upsert(
        {
            "user_id": int(user_id),
            "usage_date": usage_date,
            "feature": feature,
            "used_count": int(max(0, used_count)),
            "updated_at": now_iso,
        },
        on_conflict="user_id,usage_date,feature",
    ).execute()


def _rpc_row(data: Any) -> Dict[str, Any]:
    if isinstance(data, list):
        return dict(data[0] or {}) if data else {}
    if isinstance(data, dict):
        return dict(data)
    return {}


def _consume_usage_atomic(user_id: int, feature: str, usage_date: str, amount: int, limit: int) -> Tuple[bool, int]:
    """Atomically increments daily usage in PostgreSQL, respecting the limit."""
    if supabase is None:
        return True, amount
    res = supabase.rpc(
        "consume_free_plan_usage",
        {
            "p_user_id": int(user_id),
            "p_usage_date": usage_date,
            "p_feature": feature,
            "p_amount": int(amount),
            "p_limit": int(limit),
        },
    ).execute()
    row = _rpc_row(getattr(res, "data", None))
    if not row:
        raise RuntimeError("Supabase RPC consume_free_plan_usage returned empty response")
    return bool(row.get("allowed")), max(0, _safe_int(row.get("used_count"), 0))


def get_free_usage_status(user_id: Any, feature: str) -> FreeLimitResult:
    raw_uid = _safe_int(user_id)
    uid = _canonical_user_id(raw_uid)
    feature_key = str(feature or "").strip().lower()
    limit = _limit_for_feature(feature_key)
    usage_date = _today_key()
    plan_code = _active_plan_code(uid)
    if plan_code != "free":
        return FreeLimitResult(
            allowed=True,
            feature=feature_key,
            limit=limit,
            used=0,
            remaining=limit,
            usage_date=usage_date,
            plan_code=plan_code,
            is_paid_plan=True,
            reason="paid_plan",
        )
    if uid <= 0:
        raise FreePlanLimitError("Не удалось определить пользователя для проверки лимита.", code="free_limit_user_missing")
    used = _read_usage(uid, feature_key, usage_date)
    remaining = max(0, limit - used)
    return FreeLimitResult(
        allowed=remaining > 0,
        feature=feature_key,
        limit=limit,
        used=used,
        remaining=remaining,
        usage_date=usage_date,
        plan_code="free",
        is_paid_plan=False,
        reason="ok" if remaining > 0 else "limit_exceeded",
    )


def consume_free_usage(user_id: Any, feature: str, *, amount: int = 1) -> FreeLimitResult:
    raw_uid = _safe_int(user_id)
    uid = _canonical_user_id(raw_uid)
    feature_key = str(feature or "").strip().lower()
    amount = max(1, _safe_int(amount, 1))
    status = get_free_usage_status(uid, feature_key)
    if status.is_paid_plan:
        return status
    if uid <= 0:
        raise FreePlanLimitError("Не удалось определить пользователя для проверки лимита.", code="free_limit_user_missing")
    if status.limit <= 0:
        blocked = FreeLimitResult(
            allowed=False,
            feature=feature_key,
            limit=status.limit,
            used=status.used,
            remaining=0,
            usage_date=status.usage_date,
            plan_code="free",
            is_paid_plan=False,
            reason="limit_exceeded",
        )
        raise FreePlanLimitError(_limit_message(feature_key, status.limit), result=blocked)

    allowed, used_after = _consume_usage_atomic(uid, feature_key, status.usage_date, amount, status.limit)
    result = FreeLimitResult(
        allowed=allowed,
        feature=feature_key,
        limit=status.limit,
        used=used_after,
        remaining=max(0, status.limit - used_after),
        usage_date=status.usage_date,
        plan_code="free",
        is_paid_plan=False,
        reason="consumed" if allowed else "limit_exceeded",
    )
    if not allowed:
        raise FreePlanLimitError(_limit_message(feature_key, status.limit), result=result)
    return result


def release_free_usage(user_id: Any, feature: str, *, amount: int = 1) -> None:
    """Best-effort rollback for actions that failed before an AI/TTS job was accepted."""
    uid = _canonical_user_id(user_id)
    if uid <= 0 or not is_free_plan_user(uid):
        return
    feature_key = str(feature or "").strip().lower()
    amount = max(1, _safe_int(amount, 1))
    usage_date = _today_key()
    if supabase is None:
        return
    try:
        supabase.rpc(
            "release_free_plan_usage",
            {
                "p_user_id": int(uid),
                "p_usage_date": usage_date,
                "p_feature": feature_key,
                "p_amount": int(amount),
            },
        ).execute()
    except Exception:
        # Fallback for dev DBs without the RPC. This is best-effort only.
        try:
            used = _read_usage(uid, feature_key, usage_date)
            _write_usage(uid, feature_key, usage_date, max(0, used - amount))
        except Exception:
            pass


def _prompt_id_str(prompt_id: Any) -> str:
    return str(prompt_id or "").strip()


def _read_free_prompt_open_count(user_id: int) -> int:
    if supabase is None:
        return 0
    res = (
        supabase.table("free_prompt_opens")
        .select("prompt_id")
        .eq("user_id", int(user_id))
        .execute()
    )
    rows = list(getattr(res, "data", None) or [])
    return max(0, len(rows))


def _has_free_prompt_open(user_id: int, prompt_id: str) -> bool:
    if supabase is None or not prompt_id:
        return False
    res = (
        supabase.table("free_prompt_opens")
        .select("id")
        .eq("user_id", int(user_id))
        .eq("prompt_id", prompt_id)
        .limit(1)
        .execute()
    )
    return bool(getattr(res, "data", None) or [])


def _consume_free_prompt_open_atomic(user_id: int, prompt_id: str, limit: int) -> Tuple[bool, int, bool]:
    """Atomically records a unique prompt opening for Free users.

    Re-opening the same prompt does not increment the lifetime counter.
    """
    if supabase is None:
        return True, 0, False
    res = supabase.rpc(
        "consume_free_prompt_open",
        {
            "p_user_id": int(user_id),
            "p_prompt_id": prompt_id,
            "p_limit": int(limit),
        },
    ).execute()
    row = _rpc_row(getattr(res, "data", None))
    if not row:
        raise RuntimeError("Supabase RPC consume_free_prompt_open returned empty response")
    return (
        bool(row.get("allowed")),
        max(0, _safe_int(row.get("used_count"), 0)),
        bool(row.get("already_opened")),
    )


def get_free_prompt_open_status(user_id: Any, prompt_id: Any = "") -> FreePromptOpenResult:
    uid = _canonical_user_id(user_id)
    prompt_key = _prompt_id_str(prompt_id)
    limit = max(0, FREE_PROMPT_LIFETIME_LIMIT)
    plan_code = _active_plan_code(uid)

    if plan_code != "free":
        return FreePromptOpenResult(
            allowed=True,
            limit=limit,
            used=0,
            remaining=limit,
            prompt_id=prompt_key,
            already_opened=False,
            plan_code=plan_code,
            is_paid_plan=True,
            reason="paid_plan",
        )

    if uid <= 0:
        raise FreePlanLimitError("Не удалось определить пользователя для проверки лимита промтов.", code="free_limit_user_missing")

    used = _read_free_prompt_open_count(uid)
    already_opened = bool(prompt_key and _has_free_prompt_open(uid, prompt_key))
    remaining = max(0, limit - used)
    allowed = already_opened or remaining > 0
    return FreePromptOpenResult(
        allowed=allowed,
        limit=limit,
        used=used,
        remaining=remaining,
        prompt_id=prompt_key,
        already_opened=already_opened,
        plan_code="free",
        is_paid_plan=False,
        reason="already_opened" if already_opened else ("ok" if allowed else "limit_exceeded"),
    )


def consume_free_prompt_open(user_id: Any, prompt_id: Any) -> FreePromptOpenResult:
    uid = _canonical_user_id(user_id)
    prompt_key = _prompt_id_str(prompt_id)
    if not prompt_key:
        raise FreePlanLimitError("Не удалось определить промт для проверки лимита.", code="free_prompt_missing")

    status = get_free_prompt_open_status(uid, prompt_key)
    if status.is_paid_plan:
        return status
    if uid <= 0:
        raise FreePlanLimitError("Не удалось определить пользователя для проверки лимита промтов.", code="free_limit_user_missing")
    if status.limit <= 0:
        blocked = FreePromptOpenResult(
            allowed=False,
            limit=status.limit,
            used=status.used,
            remaining=0,
            prompt_id=prompt_key,
            already_opened=False,
            plan_code="free",
            is_paid_plan=False,
            reason="limit_exceeded",
        )
        raise FreePlanLimitError(_prompt_limit_message(status.limit), result=blocked, code="free_prompt_limit_exceeded")

    allowed, used_after, already_opened = _consume_free_prompt_open_atomic(uid, prompt_key, status.limit)
    result = FreePromptOpenResult(
        allowed=allowed,
        limit=status.limit,
        used=used_after,
        remaining=max(0, status.limit - used_after),
        prompt_id=prompt_key,
        already_opened=already_opened,
        plan_code="free",
        is_paid_plan=False,
        reason="already_opened" if already_opened else ("consumed" if allowed else "limit_exceeded"),
    )
    if not allowed:
        raise FreePlanLimitError(_prompt_limit_message(status.limit), result=result, code="free_prompt_limit_exceeded")
    return result


def validate_free_tts_text(user_id: Any, text: str) -> None:
    if not is_free_plan_user(user_id):
        return
    length = len(str(text or "").strip())
    if length > FREE_TTS_MAX_CHARS:
        raise FreePlanLimitError(
            f"На тарифе Free можно озвучить до {FREE_TTS_MAX_CHARS} символов за раз. Сократите текст и попробуйте снова.",
            code="free_tts_text_too_long",
        )


def _limit_message(feature: str, limit: int) -> str:
    if feature == FEATURE_CHAT:
        return f"Вы исчерпали дневной лимит бесплатного чата: {limit} сообщений в день. Доступ обновится завтра."
    if feature == FEATURE_TTS:
        return f"Вы исчерпали дневной лимит бесплатной озвучки: {limit} генерации в день. Доступ обновится завтра."
    return "Дневной лимит Free исчерпан. Доступ обновится завтра."


def _prompt_limit_message(limit: int) -> str:
    return (
        f"Вы уже открыли {limit} промтов на бесплатном тарифе. "
        "Полный доступ к библиотеке промтов доступен на платных тарифах."
    )


def free_limit_http_detail(exc: FreePlanLimitError) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "message": exc.message,
        "code": exc.code,
    }
    if exc.result is not None:
        payload["limit"] = exc.result.as_dict()
    return payload
