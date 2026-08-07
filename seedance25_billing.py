from __future__ import annotations

import uuid
from typing import Any, Dict
from uuid import uuid4

from billing_db import resolve_billing_user_id, supabase


SEEDANCE25_REFUND_RPC = "nabex_seedance25_refund_once"


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
