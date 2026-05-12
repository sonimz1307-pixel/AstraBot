from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from db_supabase import supabase


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_int_or_none(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        n = int(value)
        return n if n > 0 else None
    except Exception:
        return None


def log_free_usage_event(
    *,
    source: str,
    service: str,
    model: Optional[str] = None,
    mode: Optional[str] = None,
    user_id: Any = None,
    telegram_user_id: Any = None,
    workspace_account_id: Any = None,
    status: str = "completed",
    ref_id: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> bool:
    """Best-effort logging for free AI usage.

    This function must never break chat/generation flows. It returns False on any
    Supabase/schema error and True when the row was inserted or already existed.
    """
    if supabase is None:
        return False

    clean_ref_id = str(ref_id or "").strip()[:160]
    clean_meta = meta if isinstance(meta, dict) else {}
    clean_source = (str(source or "site").strip().lower() or "site")[:40]
    clean_status = (str(status or "completed").strip().lower() or "completed")[:40]
    clean_service = (str(service or "unknown").strip() or "unknown")[:120]

    row: Dict[str, Any] = {
        "created_at": _now_iso(),
        "user_id": _safe_int_or_none(user_id),
        "telegram_user_id": _safe_int_or_none(telegram_user_id),
        "workspace_account_id": _safe_int_or_none(workspace_account_id),
        "source": clean_source,
        "service": clean_service,
        "model": (str(model or "").strip() or None),
        "mode": (str(mode or "").strip() or None),
        "status": clean_status,
        "ref_id": clean_ref_id or None,
        "meta": clean_meta,
    }

    # Remove nulls so older partial schemas are slightly more tolerant.
    payload = {k: v for k, v in row.items() if v is not None}

    try:
        if clean_ref_id:
            existing = (
                supabase.table("free_usage_events")
                .select("id")
                .eq("ref_id", clean_ref_id)
                .limit(1)
                .execute()
            )
            if getattr(existing, "data", None):
                return True
        supabase.table("free_usage_events").insert(payload).execute()
        return True
    except Exception as exc:
        try:
            print(f"[free_usage_events] log skipped service={clean_service} ref_id={clean_ref_id}: {exc}", flush=True)
        except Exception:
            pass
        return False


async def log_free_usage_event_async(**kwargs: Any) -> bool:
    return await asyncio.to_thread(lambda: log_free_usage_event(**kwargs))
