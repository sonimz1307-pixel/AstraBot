# billing_db.py
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import uuid4

from supabase import create_client

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    supabase = None
else:
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_client():
    if supabase is None:
        raise RuntimeError("Supabase disabled: SUPABASE_URL / SUPABASE_SERVICE_KEY not set")


def ensure_user_row(telegram_user_id: int) -> None:
    """
    Гарантируем строку в bot_user_balance.
    ВАЖНО: НЕ ТРОГАЕМ balance_tokens (иначе можно обнулить баланс).
    """
    _require_client()
    if telegram_user_id is None:
        raise ValueError("telegram_user_id is None")
    uid = int(telegram_user_id)
    if uid <= 0:
        raise ValueError(f"telegram_user_id invalid: {telegram_user_id}")

    # Вставляем строку, если её нет. Если есть — ничего не делаем.
    try:
        supabase.table("bot_user_balance").insert(
            {
                "telegram_user_id": uid,
                "updated_at": _now_iso(),
            }
        ).execute()
    except Exception:
        # row already exists (unique violation) or other non-critical error
        pass


def get_balance(telegram_user_id: int) -> int:
    _require_client()
    uid = int(telegram_user_id)

    r = (
        supabase.table("bot_user_balance")
        .select("balance_tokens")
        .eq("telegram_user_id", uid)
        .limit(1)
        .execute()
    )
    if not r.data:
        ensure_user_row(uid)
        return 0

    try:
        return int(r.data[0].get("balance_tokens") or 0)
    except Exception:
        return 0



def ledger_ref_exists(*, reason: str, ref_id: str) -> bool:
    """Проверка идемпотентности: есть ли уже запись в bot_balance_ledger по (reason, ref_id)."""
    _require_client()
    r = (
        supabase.table("bot_balance_ledger")
        .select("id")
        .eq("reason", str(reason))
        .eq("ref_id", str(ref_id))
        .limit(1)
        .execute()
    )
    return bool(getattr(r, "data", None))

def add_tokens(
    telegram_user_id: int,
    delta_tokens: int,
    *,
    reason: str,
    meta: Optional[Dict[str, Any]] = None,
    ref_id: Optional[str] = None,
) -> str:
    """
    Универсальное изменение баланса + запись в ledger.
    Возвращает id ledger-записи (uuid).
    """
    _require_client()
    uid = int(telegram_user_id)
    ensure_user_row(uid)

    delta = int(delta_tokens)
    if delta == 0:
        raise ValueError("delta_tokens cannot be 0")

    # получаем текущий
    bal = get_balance(uid)
    new_bal = bal + delta
    if new_bal < 0:
        raise RuntimeError(f"Insufficient balance: have {bal}, need {-delta}")

    # обновляем баланс
    supabase.table("bot_user_balance").update(
        {"balance_tokens": new_bal, "updated_at": _now_iso()}
    ).eq("telegram_user_id", uid).execute()

    # пишем ledger
    ledger_id = str(uuid4())
    supabase.table("bot_balance_ledger").insert(
        {
            "id": ledger_id,
            "telegram_user_id": uid,
            "delta_tokens": delta,
            "reason": str(reason),
            "ref_id": ref_id,
            "meta": meta or {},
        }
    ).execute()

    return ledger_id


def hold_tokens_for_kling(
    *,
    telegram_user_id: int,
    seconds: int,
    mode: str,
    tokens_cost: int,
    meta: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Создаёт job в статусе hold и сразу списывает токены с баланса.
    Возвращает job_id (uuid).
    """
    _require_client()
    uid = int(telegram_user_id)
    ensure_user_row(uid)

    sec = int(seconds)
    cost = int(tokens_cost)
    if sec <= 0:
        raise ValueError("seconds must be > 0")
    if cost <= 0:
        raise ValueError("tokens_cost must be > 0")

    job_id = str(uuid4())

    # 1) списываем токены (hold = списали сразу, если упадёт — вернём rollback)
    add_tokens(
        uid,
        -cost,
        reason="kling_hold",
        meta={"seconds": sec, "mode": mode, **(meta or {})},
        ref_id=job_id,
    )

    # 2) создаём job
    supabase.table("bot_kling_jobs").insert(
        {
            "id": job_id,
            "telegram_user_id": uid,
            "status": "hold",
            "seconds": sec,
            "mode": "pro" if str(mode).lower() in ("pro", "professional") else "std",
            "tokens_cost": cost,
            "meta": meta or {},
            "updated_at": _now_iso(),
        }
    ).execute()

    return job_id


def confirm_kling_job(job_id: str, *, out_url: Optional[str] = None, meta: Optional[Dict[str, Any]] = None) -> None:
    """
    Помечает job как success. Баланс уже списан на hold.
    """
    _require_client()
    jid = str(job_id)

    payload: Dict[str, Any] = {"status": "success", "updated_at": _now_iso()}
    if out_url:
        payload["out_url"] = out_url
    if meta:
        payload["meta"] = meta

    supabase.table("bot_kling_jobs").update(payload).eq("id", jid).execute()

    # (опционально) пишем ledger без изменения баланса — не нужно. Ledger уже содержит kling_hold.


def rollback_kling_job(job_id: str, *, error: str) -> None:
    """
    Помечает job как failed и возвращает токены пользователю.
    """
    _require_client()
    jid = str(job_id)

    # читаем job, чтобы понять кому и сколько возвращать
    r = supabase.table("bot_kling_jobs").select("telegram_user_id,tokens_cost").eq("id", jid).limit(1).execute()
    if not r.data:
        raise RuntimeError("Job not found for rollback")

    uid = int(r.data[0]["telegram_user_id"])
    cost = int(r.data[0]["tokens_cost"])

    # обновляем статус
    supabase.table("bot_kling_jobs").update(
        {"status": "failed", "error": (error or "")[:1500], "updated_at": _now_iso()}
    ).eq("id", jid).execute()

    # возвращаем токены
    add_tokens(
        uid,
        +cost,
        reason="kling_rollback",
        meta={"error": (error or "")[:300]},
        ref_id=jid,
    )
    # === SUNO BILLING ===

SUNO_GENERATION_COST = 2  # фиксировано


def charge_suno_generation(telegram_user_id: int, *, ref_id: str) -> None:
    """
    Списывает 2 токена за генерацию Suno.
    """
    add_tokens(
        telegram_user_id,
        -SUNO_GENERATION_COST,
        reason="suno_generation",
        ref_id=ref_id,
        meta={"cost": SUNO_GENERATION_COST},
    )


def refund_suno_generation(telegram_user_id: int, *, ref_id: str, error: str = "") -> None:
    """
    Возвращает токены при ошибке Suno.
    """
    add_tokens(
        telegram_user_id,
        +SUNO_GENERATION_COST,
        reason="suno_refund",
        ref_id=ref_id,
        meta={"error": (error or "")[:300]},
    )

