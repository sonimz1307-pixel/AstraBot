from __future__ import annotations

import re
import time
import uuid
from typing import Any, Dict, Optional
from uuid import uuid4

from billing_db import resolve_billing_user_id, supabase


SEEDANCE25_REFUND_RPC = "nabex_seedance25_refund_once"
# The already deployed Wan 3.0 RPC is transactionally generic: the caller
# supplies p_reason, p_ref_id and p_meta. Reusing that existing RPC gives
# Seedance 2.5 the same atomic balance-row lock and idempotent charge boundary
# without adding a worker or requiring another database migration.
SEEDANCE25_CHARGE_RPC = "nabex_wan3_charge_once"
SEEDANCE25_CHARGE_REASON = "seedance25_video"


class Seedance25InsufficientBalanceError(RuntimeError):
    def __init__(self, balance: int, required: int):
        self.balance = max(0, int(balance or 0))
        self.required = max(0, int(required or 0))
        super().__init__(f"Insufficient balance: have {self.balance}, need {self.required}")


def _rpc_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, list):
        if not value:
            return False
        return _rpc_bool(value[0])
    if isinstance(value, dict):
        for key in (SEEDANCE25_REFUND_RPC, "result", "ok", "refunded"):
            if key in value:
                return _rpc_bool(value.get(key))
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes"}
    return bool(value)


def _rpc_payload(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, list) and value:
        return _rpc_payload(value[0])
    return {}


def charge_seedance25_once(
    user_id: int,
    amount_tokens: int,
    *,
    ref_id: str,
    meta: Dict[str, Any] | None = None,
    attempts: int = 3,
) -> Dict[str, Any]:
    """Atomically and idempotently debit a Seedance 2.5 generation.

    The same stable ref_id is retried after an ambiguous transport failure. If
    PostgreSQL committed the first call but the HTTP response was lost, the next
    call returns ``already_charged`` instead of debiting the user twice.
    """
    amount = int(amount_tokens or 0)
    if amount <= 0:
        raise ValueError("Seedance 2.5 charge tokens must be positive")
    if supabase is None:
        raise RuntimeError("Supabase disabled: cannot execute Seedance 2.5 atomic charge")

    ref_text = str(ref_id or "").strip()
    if not ref_text:
        raise ValueError("Seedance 2.5 atomic charge requires ref_id")
    try:
        ref_uuid = str(uuid.UUID(ref_text))
    except Exception as exc:
        raise ValueError(f"Seedance 2.5 charge ref_id must be UUID: {ref_text}") from exc

    uid = int(resolve_billing_user_id(int(user_id)))
    charge_meta = dict(meta or {})
    charge_meta.setdefault("provider_kind", "seedance25")
    last_exc: Exception | None = None
    max_attempts = max(1, min(int(attempts or 3), 5))

    for attempt in range(1, max_attempts + 1):
        try:
            response = supabase.rpc(
                SEEDANCE25_CHARGE_RPC,
                {
                    "p_telegram_user_id": uid,
                    "p_amount": amount,
                    "p_reason": SEEDANCE25_CHARGE_REASON,
                    "p_ref_id": ref_uuid,
                    "p_meta": charge_meta,
                    "p_ledger_id": str(uuid4()),
                },
            ).execute()
            data = _rpc_payload(getattr(response, "data", response))
            if not data or not bool(data.get("ok")):
                raise RuntimeError(f"Seedance 2.5 atomic charge returned an invalid response: {data!r}")
            return {
                "ok": True,
                "charged": bool(data.get("charged")),
                "already_charged": bool(data.get("already_charged")),
                "ledger_id": str(data.get("ledger_id") or ""),
                "balance_tokens": int(data.get("balance_tokens") or 0),
                "telegram_user_id": int(data.get("telegram_user_id") or uid),
            }
        except Exception as exc:
            text = str(exc)
            match = re.search(
                r"(?:WAN3|SEEDANCE25)_INSUFFICIENT_BALANCE\s*:\s*(\d+)\s*:\s*(\d+)",
                text,
                re.IGNORECASE,
            )
            if match:
                raise Seedance25InsufficientBalanceError(int(match.group(1)), int(match.group(2))) from exc
            lowered = text.lower()
            if SEEDANCE25_CHARGE_RPC in lowered and ("not found" in lowered or "does not exist" in lowered):
                raise RuntimeError(
                    "Atomic billing RPC nabex_wan3_charge_once is not installed; "
                    "the existing Wan 3.0 billing migration must be present"
                ) from exc
            last_exc = exc
            if attempt < max_attempts:
                time.sleep(min(2.0, 0.5 * attempt))

    raise RuntimeError(
        f"Seedance 2.5 atomic charge failed after {max_attempts} attempts: {last_exc}"
    ) from last_exc


def get_seedance25_charge_amount(user_id: int, *, ref_id: str) -> Optional[int]:
    """Return the exact committed debit for a stable Seedance 2.5 ref.

    ``None`` is a positive database answer that no charge exists.  Transport or
    malformed-ledger failures raise instead, so reconciliation never guesses
    that an ambiguous debit is safe to refund or release.
    """
    if supabase is None:
        raise RuntimeError("Supabase disabled: cannot reconcile Seedance 2.5 charge")
    ref_text = str(ref_id or "").strip()
    if not ref_text:
        raise ValueError("Seedance 2.5 charge lookup requires ref_id")
    try:
        ref_uuid = str(uuid.UUID(ref_text))
    except Exception as exc:
        raise ValueError(f"Seedance 2.5 charge ref_id must be UUID: {ref_text}") from exc

    uid = int(resolve_billing_user_id(int(user_id)))
    response = (
        supabase.table("bot_balance_ledger")
        .select("delta_tokens")
        .eq("telegram_user_id", uid)
        .eq("reason", SEEDANCE25_CHARGE_REASON)
        .eq("ref_id", ref_uuid)
        .limit(1)
        .execute()
    )
    rows = getattr(response, "data", None) or []
    if not rows:
        return None
    try:
        delta = int((rows[0] or {}).get("delta_tokens"))
    except Exception as exc:
        raise RuntimeError(f"Seedance 2.5 charge ledger is malformed for ref_id={ref_uuid}") from exc
    if delta >= 0:
        raise RuntimeError(
            f"Seedance 2.5 charge ledger has non-debit delta={delta} for ref_id={ref_uuid}"
        )
    return abs(delta)


def refund_seedance25_once(
    user_id: int,
    amount_tokens: int,
    *,
    reason: str,
    ref_id: str,
    meta: Dict[str, Any] | None = None,
) -> bool:
    """Atomically refund Seedance 2.5 tokens exactly once via Postgres RPC.

    The SQL function updates bot_user_balance and writes bot_balance_ledger in the
    same DB transaction and serializes concurrent calls for (user, reason, ref_id).
    Apply sql/NABEX_SEEDANCE25_REFUND_ONCE.sql before deploying this patch.
    """
    amount = int(amount_tokens or 0)
    if amount <= 0:
        return False
    if supabase is None:
        raise RuntimeError("Supabase disabled: cannot execute Seedance 2.5 atomic refund")

    ref_text = str(ref_id or "").strip()
    if not ref_text:
        raise ValueError("Seedance 2.5 atomic refund requires ref_id")
    try:
        ref_uuid = str(uuid.UUID(ref_text))
    except Exception as exc:
        raise ValueError(f"Seedance 2.5 refund ref_id must be UUID: {ref_text}") from exc

    uid = int(resolve_billing_user_id(int(user_id)))
    ledger_id = str(uuid4())
    response = supabase.rpc(
        SEEDANCE25_REFUND_RPC,
        {
            "p_telegram_user_id": uid,
            "p_amount": amount,
            "p_reason": str(reason or "seedance25_video_refund"),
            "p_ref_id": ref_uuid,
            "p_meta": dict(meta or {}),
            "p_ledger_id": ledger_id,
        },
    ).execute()
    return _rpc_bool(getattr(response, "data", response))
