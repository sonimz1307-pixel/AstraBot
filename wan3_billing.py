from __future__ import annotations

from datetime import datetime, timezone
import re
import uuid
from typing import Any, Dict, List, Optional
from uuid import uuid4

from billing_db import ledger_ref_exists, resolve_billing_user_id, supabase
from seedance25_billing import refund_seedance25_once


WAN3_CHARGE_REASON = "wan3_video"
WAN3_DEFAULT_REFUND_REASON = "wan3_video_refund"
WAN3_CHARGE_RPC = "nabex_wan3_charge_once"


class Wan3InsufficientBalanceError(RuntimeError):
    def __init__(self, balance: int, required: int):
        self.balance = max(0, int(balance or 0))
        self.required = max(0, int(required or 0))
        super().__init__(f"Insufficient balance: have {self.balance}, need {self.required}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rpc_payload(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, list) and value:
        return _rpc_payload(value[0])
    return {}


def _wan3_charge_uuid(ref_id: str) -> str:
    ref = str(ref_id or "").strip()
    if not ref:
        raise ValueError("Wan 3.0 atomic charge requires charge_ref_id")
    try:
        return str(uuid.UUID(ref))
    except Exception as exc:
        raise ValueError(f"Wan 3.0 charge_ref_id must be UUID: {ref}") from exc


def charge_wan3_once(
    user_id: int,
    tokens: int,
    *,
    ref_id: str,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Atomically debit Wan 3.0 tokens and write the charge ledger row.

    This RPC is the V5 money boundary. PostgreSQL locks the canonical balance row,
    checks funds, debits it and inserts ``bot_balance_ledger`` in one transaction.
    The operation is idempotent for ``(reason='wan3_video', ref_id)``.

    There is intentionally no fallback to ``billing_db.add_tokens``: deploying V5
    application code without the V5 SQL must fail closed rather than re-introduce
    the non-transactional charge race that V5 is designed to remove.
    """
    amount = int(tokens or 0)
    if amount <= 0:
        raise ValueError("Wan 3.0 charge tokens must be positive")
    if supabase is None:
        raise RuntimeError("Supabase disabled: cannot execute Wan 3.0 atomic charge")

    ref_uuid = _wan3_charge_uuid(ref_id)
    uid = int(resolve_billing_user_id(int(user_id)))
    ledger_id = str(uuid4())
    charge_meta = dict(meta or {})
    charge_meta.setdefault("wan3_recovery_open", True)
    charge_meta.setdefault("wan3_recovery_opened_at", _now_iso())

    try:
        response = supabase.rpc(
            WAN3_CHARGE_RPC,
            {
                "p_telegram_user_id": uid,
                "p_amount": amount,
                "p_reason": WAN3_CHARGE_REASON,
                "p_ref_id": ref_uuid,
                "p_meta": charge_meta,
                "p_ledger_id": ledger_id,
            },
        ).execute()
    except Exception as exc:
        text = str(exc)
        match = re.search(r"WAN3_INSUFFICIENT_BALANCE\s*:\s*(\d+)\s*:\s*(\d+)", text)
        if match:
            raise Wan3InsufficientBalanceError(int(match.group(1)), int(match.group(2))) from exc
        if "nabex_wan3_charge_once" in text.lower() and ("not found" in text.lower() or "does not exist" in text.lower()):
            raise RuntimeError(
                "Wan 3.0 V5 atomic billing RPC is not installed. "
                "Apply sql/NABEX_WAN3_V5_ATOMIC_BILLING.sql before deploying V5."
            ) from exc
        raise

    data = _rpc_payload(getattr(response, "data", response))
    if not data or not bool(data.get("ok")):
        raise RuntimeError(f"Wan 3.0 atomic charge returned an invalid response: {data!r}")
    try:
        balance_after = int(data.get("balance_tokens") or 0)
    except Exception:
        balance_after = 0
    return {
        "ok": True,
        "charged": bool(data.get("charged")),
        "already_charged": bool(data.get("already_charged")),
        "ledger_id": str(data.get("ledger_id") or ""),
        "balance_tokens": balance_after,
        "telegram_user_id": int(data.get("telegram_user_id") or uid),
    }


def refund_wan3_once(user_id: int, tokens: int, *, reason: str, ref_id: str, meta: Optional[Dict[str, Any]] = None) -> bool:
    """Atomic/idempotent NABEX refund using the already deployed generic refund RPC."""
    refunded = refund_seedance25_once(
        int(user_id), int(tokens), reason=str(reason or WAN3_DEFAULT_REFUND_REASON), ref_id=str(ref_id or ""), meta=dict(meta or {})
    )
    if refunded:
        # Closing recovery metadata is best-effort only. The refund itself is the
        # financial source of truth; the reconciler can close the row later if
        # this metadata update is temporarily unavailable.
        try:
            mark_wan3_refund_reconciled(str(ref_id or ""), stage="refund_rpc_success")
        except Exception:
            pass
    return bool(refunded)


def _wan3_charge_row(ref_id: str) -> Optional[Dict[str, Any]]:
    ref = str(ref_id or "").strip()
    if not ref or supabase is None:
        return None
    response = (
        supabase.table("bot_balance_ledger")
        .select("id,telegram_user_id,delta_tokens,reason,ref_id,meta,created_at")
        .eq("reason", WAN3_CHARGE_REASON)
        .eq("ref_id", ref)
        .limit(1)
        .execute()
    )
    rows = list(getattr(response, "data", None) or [])
    return dict(rows[0]) if rows and isinstance(rows[0], dict) else None


def patch_wan3_charge_meta(ref_id: str, updates: Dict[str, Any]) -> bool:
    """Merge recovery metadata into the original Wan charge ledger row."""
    ref = str(ref_id or "").strip()
    if not ref or supabase is None:
        return False
    row = _wan3_charge_row(ref)
    if not row:
        return False
    row_id = str(row.get("id") or "").strip()
    if not row_id:
        return False
    meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
    merged = dict(meta)
    merged.update(dict(updates or {}))
    merged["wan3_recovery_updated_at"] = _now_iso()
    supabase.table("bot_balance_ledger").update({"meta": merged}).eq("id", row_id).execute()
    return True


def mark_wan3_refund_pending(
    ref_id: str,
    *,
    job_id: str,
    queue_name: str,
    tokens: int,
    refund_reason: str,
    stage: str,
    error: str = "",
) -> bool:
    return patch_wan3_charge_meta(
        ref_id,
        {
            "wan3_recovery_open": True,
            "wan3_job_id": str(job_id or "").strip(),
            "wan3_queue_name": str(queue_name or "wan3").strip() or "wan3",
            "wan3_refund_pending": True,
            "wan3_refund_tokens": int(tokens or 0),
            "wan3_refund_reason": str(refund_reason or WAN3_DEFAULT_REFUND_REASON),
            "wan3_refund_stage": str(stage or "unknown")[:120],
            "wan3_refund_error": str(error or "")[:500],
            "wan3_refund_pending_at": _now_iso(),
        },
    )


def mark_wan3_refund_reconciled(ref_id: str, *, stage: str = "reconciler") -> bool:
    return patch_wan3_charge_meta(
        ref_id,
        {
            "wan3_recovery_open": False,
            "wan3_recovery_closed_at": _now_iso(),
            "wan3_recovery_closed_reason": str(stage or "reconciler")[:120],
            "wan3_refund_pending": False,
            "wan3_refund_reconciled": True,
            "wan3_refund_reconciled_at": _now_iso(),
            "wan3_refund_reconciled_stage": str(stage or "reconciler")[:120],
        },
    )


def wan3_refund_exists(ref_id: str, *, reason: str = WAN3_DEFAULT_REFUND_REASON) -> bool:
    ref = str(ref_id or "").strip()
    if not ref:
        return False
    return bool(ledger_ref_exists(reason=str(reason or WAN3_DEFAULT_REFUND_REASON), ref_id=ref))


def list_wan3_recovery_charges(*, batch_size: int = 250) -> List[Dict[str, Any]]:
    """Return *all* currently-open V5/V4 Wan recovery charges, in bounded pages.

    V4 scanned only the newest N charge rows, so an old unresolved charge could
    fall out of the window. V5 marks unresolved charge rows with
    ``meta.wan3_recovery_open=true`` and keyset-pages every such row by UUID.
    There is no global "last 500" cap. The V5 SQL backfills eligible V4 rows.
    """
    if supabase is None:
        return []

    page_size = max(25, min(int(batch_size or 250), 1000))
    out: List[Dict[str, Any]] = []
    cursor_id = ""
    while True:
        query = (
            supabase.table("bot_balance_ledger")
            .select("id,telegram_user_id,delta_tokens,reason,ref_id,meta,created_at")
            .eq("reason", WAN3_CHARGE_REASON)
            .contains("meta", {"wan3_recovery_open": True})
            .order("id", desc=False)
            .limit(page_size)
        )
        if cursor_id:
            query = query.gt("id", cursor_id)
        response = query.execute()
        rows = [dict(row) for row in list(getattr(response, "data", None) or []) if isinstance(row, dict)]
        if not rows:
            break
        out.extend(rows)
        next_cursor = str(rows[-1].get("id") or "").strip()
        if not next_cursor or next_cursor == cursor_id:
            break
        cursor_id = next_cursor
        if len(rows) < page_size:
            break
    return out
