import os
from datetime import date, datetime, timezone
from typing import Dict, Any

from supabase import create_client

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    supabase = None
else:
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def track_user_activity(tg_user: Dict[str, Any]) -> None:
    """
    1) upsert пользователя в bot_users
    2) фиксирует DAU в bot_daily_active
    """
    if supabase is None:
        return

    try:
        telegram_user_id = int(tg_user["id"])

        # --- users ---
        supabase.table("bot_users").upsert(
            {
                "telegram_user_id": telegram_user_id,
                "username": tg_user.get("username"),
                "first_name": tg_user.get("first_name"),
                "last_name": tg_user.get("last_name"),
                "language_code": tg_user.get("language_code"),
                "is_bot": tg_user.get("is_bot"),
                "is_premium": tg_user.get("is_premium"),
                "last_seen_at": _now_iso(),
                "updated_at": _now_iso(),
            },
            on_conflict="telegram_user_id",
        ).execute()

        # --- DAU ---
        supabase.table("bot_daily_active").upsert(
            {
                "day": str(date.today()),
                "telegram_user_id": telegram_user_id,
            },
            on_conflict="day,telegram_user_id",
        ).execute()

    except Exception:
        # аналитика не должна ломать бота
        pass
