from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from db_supabase import supabase as sb


def sb_get_user_state(user_id: int) -> Tuple[str, Optional[Dict[str, Any]]]:
    """
    Shared Supabase-backed Telegram state.
    Returns (state, payload) or ("idle", None).
    """
    if sb is None:
        return ("idle", None)
    try:
        r = (
            sb.table("bot_user_state")
            .select("state,payload")
            .eq("telegram_user_id", int(user_id))
            .limit(1)
            .execute()
        )
        if r.data:
            row = r.data[0] or {}
            payload = row.get("payload")
            return (str(row.get("state") or "idle"), payload if isinstance(payload, dict) else payload)
    except Exception:
        pass
    return ("idle", None)


def sb_set_user_state(user_id: int, state: str, payload: Optional[Dict[str, Any]] = None) -> None:
    if sb is None:
        return
    try:
        sb.table("bot_user_state").upsert(
            {
                "telegram_user_id": int(user_id),
                "state": str(state or "idle"),
                "payload": payload,
            },
            on_conflict="telegram_user_id",
        ).execute()
    except Exception:
        pass


def sb_clear_user_state(user_id: int) -> None:
    if sb is None:
        return
    try:
        sb.table("bot_user_state").upsert(
            {
                "telegram_user_id": int(user_id),
                "state": "idle",
                "payload": None,
            },
            on_conflict="telegram_user_id",
        ).execute()
    except Exception:
        pass
