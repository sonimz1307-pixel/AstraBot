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

def _public_balance_meta(reason: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    """User history never exposes a Marketplace recipe or settlement internals."""
    if meta.get("source") != "trend_marketplace" and reason not in {"trend_generation", "trend_generation_refund"}:
        return meta
    safe = {key: meta[key] for key in ("source", "trend_id", "trend_version_id", "trend_run_id",
            "base_tokens", "creator_markup_tokens", "total_tokens") if key in meta}
    safe["source"] = "trend_marketplace"
    snapshot = meta.get("settlement_snapshot")
    if isinstance(snapshot, dict):
        for key in ("base_tokens", "creator_markup_tokens", "total_tokens"):
            if key in snapshot:
                safe[key] = snapshot[key]
    return safe


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
        meta = _public_balance_meta(str(row.get("reason") or ""), meta)
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

    # The UUID is stable across a transport retry of this invocation. Legacy
    # reason/ref semantics are preserved; only Marketplace uses unique op keys.
    ledger_id = str(uuid4())
    if ref_id:
        try:
            ref_id = str(uuid.UUID(str(ref_id)))
        except Exception:
            meta = dict(meta or {})
            meta.setdefault("ref_tag", str(ref_id))
            ref_id = str(uuid4())
    response = supabase.rpc(
        "nabex_balance_change",
        {"p_user_id": uid, "p_delta": delta, "p_reason": str(reason),
         "p_ref_id": ref_id, "p_meta": meta or {}, "p_ledger_id": ledger_id},
    ).execute()
    data = getattr(response, "data", response)
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict) or not data.get("ok"):
        raise RuntimeError("Atomic billing RPC returned an invalid response")
    return str(data.get("ledger_id") or ledger_id)



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

    merge_ref = str(uuid.uuid5(uuid.NAMESPACE_URL, f"astrabot:balance-merge:{source}->{target}"))
    response = supabase.rpc(
        "nabex_balance_merge",
        {"p_source": source, "p_target": target, "p_ref_id": merge_ref},
    ).execute()
    data = getattr(response, "data", response)
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict) or not data.get("ok"):
        raise RuntimeError("Atomic balance merge RPC returned an invalid response")
    return {**data, "source_user_id": source, "target_user_id": target}


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
