# billing_db.py
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
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



def _coerce_positive_user_id(user_id: int) -> int:
    if user_id is None:
        raise ValueError("telegram_user_id is None")
    uid = int(user_id)
    if uid <= 0:
        raise ValueError(f"telegram_user_id invalid: {user_id}")
    return uid


def resolve_billing_user_id(user_id: int) -> int:
    """
    Возвращает канонический ID для баланса.

    Логика безопасная для старых аккаунтов:
    1) если переданный ID уже является workspace_accounts.id — используем его как есть;
    2) иначе, если это привязанный Telegram ID в workspace_accounts.telegram_user_id —
       используем workspace_accounts.id;
    3) иначе оставляем старое поведение и используем переданный Telegram ID.

    Так сайт/email-аккаунт и TG-бот начинают работать с одной строкой баланса,
    но Telegram-only пользователи не ломаются.
    """
    _require_client()
    uid = _coerce_positive_user_id(user_id)

    # Если это уже ID аккаунта сайта — не перекидываем его по telegram_user_id.
    try:
        exact = (
            supabase.table("workspace_accounts")
            .select("id")
            .eq("id", uid)
            .limit(1)
            .execute()
        )
        if getattr(exact, "data", None):
            return uid
    except Exception:
        # Если таблица недоступна/ещё не создана — не ломаем старую биллинговую схему.
        return uid

    # Если это Telegram ID, привязанный к email/site аккаунту — используем ID аккаунта сайта.
    try:
        linked = (
            supabase.table("workspace_accounts")
            .select("id")
            .eq("telegram_user_id", uid)
            .limit(1)
            .execute()
        )
        data = getattr(linked, "data", None) or []
        if data:
            account_id = int(data[0].get("id") or 0)
            if account_id > 0:
                return account_id
    except Exception:
        pass

    return uid


def _ensure_user_row_raw(uid: int) -> None:
    """Создаёт строку bot_user_balance строго для указанного ID, без alias/resolve."""
    _require_client()
    raw_uid = _coerce_positive_user_id(uid)
    try:
        supabase.table("bot_user_balance").insert(
            {
                "telegram_user_id": raw_uid,
                "updated_at": _now_iso(),
            }
        ).execute()
    except Exception:
        # row already exists (unique violation) or other non-critical error
        pass


def _read_balance_raw(uid: int) -> int:
    """Читает баланс строго по указанному ID, без alias/resolve."""
    _require_client()
    raw_uid = _coerce_positive_user_id(uid)
    r = (
        supabase.table("bot_user_balance")
        .select("balance_tokens")
        .eq("telegram_user_id", raw_uid)
        .limit(1)
        .execute()
    )
    if not getattr(r, "data", None):
        return 0
    try:
        return int(r.data[0].get("balance_tokens") or 0)
    except Exception:
        return 0


def _maybe_merge_linked_balance(source_user_id: int, canonical_user_id: int) -> None:
    """Best-effort перенос старого TG-баланса на канонический workspace account."""
    try:
        source = _coerce_positive_user_id(source_user_id)
        target = _coerce_positive_user_id(canonical_user_id)
        if source != target:
            merge_user_balance_records(source_user_id=source, target_user_id=target)
    except Exception as exc:
        # Не блокируем оплату/генерацию из-за вспомогательной миграции баланса.
        try:
            print(f"[billing] linked balance merge skipped: {exc}")
        except Exception:
            pass


def ensure_user_row(telegram_user_id: int) -> None:
    """
    Гарантируем строку в bot_user_balance для канонического billing ID.
    Если Telegram уже привязан к email/site аккаунту, используем workspace_accounts.id.
    ВАЖНО: НЕ ТРОГАЕМ balance_tokens (иначе можно обнулить баланс).
    """
    _require_client()
    raw_uid = _coerce_positive_user_id(telegram_user_id)
    uid = resolve_billing_user_id(raw_uid)
    _ensure_user_row_raw(uid)
    _maybe_merge_linked_balance(raw_uid, uid)


def get_balance(telegram_user_id: int) -> int:
    _require_client()
    raw_uid = _coerce_positive_user_id(telegram_user_id)
    uid = resolve_billing_user_id(raw_uid)
    _ensure_user_row_raw(uid)
    _maybe_merge_linked_balance(raw_uid, uid)
    return _read_balance_raw(uid)



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

def get_balance_history(telegram_user_id: int, *, limit: int = 30) -> List[Dict[str, Any]]:
    """Возвращает последние операции по балансу пользователя."""
    _require_client()
    raw_uid = _coerce_positive_user_id(telegram_user_id)
    uid = resolve_billing_user_id(raw_uid)
    _maybe_merge_linked_balance(raw_uid, uid)
    lim = max(1, min(int(limit or 30), 100))
    fields = "id, telegram_user_id, delta_tokens, reason, ref_id, meta, created_at"
    fallback_fields = "id, telegram_user_id, delta_tokens, reason, ref_id, meta"

    try:
        response = (
            supabase.table("bot_balance_ledger")
            .select(fields)
            .eq("telegram_user_id", uid)
            .order("created_at", desc=True)
            .limit(lim)
            .execute()
        )
    except Exception:
        response = (
            supabase.table("bot_balance_ledger")
            .select(fallback_fields)
            .eq("telegram_user_id", uid)
            .limit(lim)
            .execute()
        )

    rows = list(getattr(response, "data", None) or [])
    rows.sort(key=lambda row: str((row or {}).get("created_at") or ""), reverse=True)

    items: List[Dict[str, Any]] = []
    for row in rows[:lim]:
        meta = row.get("meta")
        if not isinstance(meta, dict):
            meta = {}
        try:
            delta = int(row.get("delta_tokens") or 0)
        except Exception:
            delta = 0
        items.append(
            {
                "id": str(row.get("id") or ""),
                "telegram_user_id": uid,
                "delta_tokens": delta,
                "reason": str(row.get("reason") or ""),
                "ref_id": row.get("ref_id"),
                "meta": meta,
                "created_at": row.get("created_at"),
            }
        )
    return items


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
    raw_uid = _coerce_positive_user_id(telegram_user_id)
    uid = resolve_billing_user_id(raw_uid)
    _maybe_merge_linked_balance(raw_uid, uid)
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
    # normalize ref_id for DB (column is UUID). If caller passes a non-UUID tag, keep it in meta.
    if ref_id:
        try:
            _ = uuid.UUID(str(ref_id))
            ref_id = str(ref_id)
        except Exception:
            meta = dict(meta or {})
            meta.setdefault('ref_tag', str(ref_id))
            ref_id = str(uuid4())

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



def merge_user_balance_records(*, source_user_id: int, target_user_id: int) -> Dict[str, Any]:
    """
    Безопасно переносит старый баланс Telegram ID на баланс workspace/email аккаунта.

    Используется при привязке Telegram к email-аккаунту и при последующих операциях баланса.
    Старые аккаунты не обнуляются вслепую: перенос выполняется только если source != target
    и у source есть положительный баланс.

    Повторный вызов не должен повторно начислять перенос, потому что используется
    детерминированный ref_id в ledger.
    """
    _require_client()
    source = _coerce_positive_user_id(source_user_id)
    target = resolve_billing_user_id(target_user_id)
    target = _coerce_positive_user_id(target)

    if source == target:
        _ensure_user_row_raw(target)
        return {"ok": True, "merged": False, "reason": "same_user_id", "source_user_id": source, "target_user_id": target}

    _ensure_user_row_raw(source)
    _ensure_user_row_raw(target)

    source_balance = _read_balance_raw(source)
    target_before = _read_balance_raw(target)

    if source_balance <= 0:
        return {
            "ok": True,
            "merged": False,
            "reason": "source_balance_not_positive",
            "source_user_id": source,
            "target_user_id": target,
            "source_balance": source_balance,
            "target_balance": target_before,
        }

    merge_ref = str(uuid.uuid5(uuid.NAMESPACE_URL, f"astrabot:balance-merge:{source}->{target}"))
    already_merged = ledger_ref_exists(reason="account_balance_merge", ref_id=merge_ref)

    if already_merged:
        # ВАЖНО: если перенос уже отмечен в ledger, НЕ зануляем source повторно.
        # Иначе можно потерять токены, которые по ошибке/старым кодом попали на старый Telegram ID
        # уже после первого переноса. Оставляем их на source для ручной проверки/отдельной миграции.
        return {
            "ok": True,
            "merged": False,
            "reason": "already_merged_source_kept",
            "source_user_id": source,
            "target_user_id": target,
            "source_balance": source_balance,
            "target_balance": target_before,
            "moved_tokens": 0,
        }

    # Начисляем ровно один раз через стандартную функцию, чтобы появился ledger.
    add_tokens(
        target,
        source_balance,
        reason="account_balance_merge",
        ref_id=merge_ref,
        meta={
            "source_user_id": source,
            "target_user_id": target,
            "source_balance_tokens": source_balance,
            "target_balance_before": target_before,
        },
    )

    target_after = _read_balance_raw(target)
    if target_after < target_before + source_balance:
        # Не зануляем source, если не можем подтвердить, что target получил перенос.
        return {
            "ok": False,
            "merged": False,
            "reason": "target_credit_not_verified_source_kept",
            "source_user_id": source,
            "target_user_id": target,
            "source_balance": source_balance,
            "target_balance_before": target_before,
            "target_balance_after": target_after,
            "moved_tokens": 0,
        }

    # После подтверждённого начисления на target зануляем старую TG-строку.
    # Дополнительная защита: зануляем только если balance_tokens всё ещё равен той сумме,
    # которую мы переносили. Если баланс source изменился параллельно — оставляем его как есть.
    try:
        supabase.table("bot_user_balance").update(
            {"balance_tokens": 0, "updated_at": _now_iso()}
        ).eq("telegram_user_id", source).eq("balance_tokens", source_balance).execute()
    except Exception as exc:
        return {
            "ok": False,
            "merged": True,
            "reason": "source_zero_failed_after_target_credit",
            "source_user_id": source,
            "target_user_id": target,
            "source_balance": source_balance,
            "target_balance_before": target_before,
            "target_balance_after": target_after,
            "moved_tokens": source_balance,
            "error": str(exc),
        }

    source_after = _read_balance_raw(source)
    return {
        "ok": True,
        "merged": True,
        "reason": "merged_source_zeroed" if source_after == 0 else "merged_source_changed_source_kept",
        "source_user_id": source,
        "target_user_id": target,
        "source_balance_before": source_balance,
        "source_balance_after": source_after,
        "moved_tokens": source_balance,
        "target_balance_before": target_before,
        "target_balance_after": target_after,
    }


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
    raw_uid = _coerce_positive_user_id(telegram_user_id)
    uid = resolve_billing_user_id(raw_uid)
    _maybe_merge_linked_balance(raw_uid, uid)
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

# === PHOTOSESSION BILLING ===

PHOTOSESSION_GENERATION_COST = 1  # фиксировано: 1 токен за генерацию


def charge_photosession_generation(telegram_user_id: int, *, ref_id: str) -> None:
    """
    Списывает 1 токен за нейро-фотосессию.
    Идемпотентность: если по (reason, ref_id) уже есть ledger — повторно не списываем.
    """
    if ledger_ref_exists(reason="photosession_generation", ref_id=ref_id):
        return

    add_tokens(
        telegram_user_id,
        -PHOTOSESSION_GENERATION_COST,
        reason="photosession_generation",
        ref_id=ref_id,
        meta={"cost": PHOTOSESSION_GENERATION_COST},
    )


def refund_photosession_generation(telegram_user_id: int, *, ref_id: str, error: str = "") -> None:
    """
    Возвращает 1 токен при ошибке нейро-фотосессии.
    Идемпотентность: если refund уже был — повторно не возвращаем.
    """
    if ledger_ref_exists(reason="photosession_refund", ref_id=ref_id):
        return

    add_tokens(
        telegram_user_id,
        +PHOTOSESSION_GENERATION_COST,
        reason="photosession_refund",
        ref_id=ref_id,
        meta={"error": (error or "")[:300]},
    )



# === WELCOME BONUS ===

WELCOME_BONUS_DEFAULT = int(os.getenv('WELCOME_BONUS_TOKENS', '3'))


def grant_welcome_bonus_once(telegram_user_id: int, *, amount: int | None = None) -> bool:
    """Начисляет приветственный бонус ТОЛЬКО 1 раз (использовать только в /start).

    Важно: RPC вызываем по исходному Telegram ID, а не по workspace account id.
    Так старый Telegram-only пользователь после привязки email не получит welcome-бонус второй раз.
    После проверки/начисления делаем best-effort перенос баланса на канонический account id.

    Возвращает True если бонус начислен сейчас, иначе False.
    """
    _require_client()
    raw_uid = _coerce_positive_user_id(telegram_user_id)
    canonical_uid = resolve_billing_user_id(raw_uid)
    _ensure_user_row_raw(raw_uid)

    amt = int(WELCOME_BONUS_DEFAULT if amount is None else amount)
    if amt <= 0:
        _maybe_merge_linked_balance(raw_uid, canonical_uid)
        return False

    r = supabase.rpc('grant_welcome_bonus', {'p_telegram_user_id': raw_uid, 'p_amount': amt}).execute()
    data = getattr(r, 'data', None)

    # После RPC переносим старый TG-баланс на linked workspace/email account, если он есть.
    _maybe_merge_linked_balance(raw_uid, canonical_uid)

    # supabase-py может вернуть bool или список/словарь
    if isinstance(data, bool):
        return data
    if isinstance(data, list) and data:
        # иногда возвращает [{'grant_welcome_bonus': true}]
        v = data[0]
        if isinstance(v, dict):
            return bool(next(iter(v.values())))
        return bool(v)
    if isinstance(data, dict):
        return bool(next(iter(data.values())))
    return False
