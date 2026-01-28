# db_supabase.py
import os
from datetime import date, datetime, timezone
from typing import Any, Dict, Optional

from supabase import create_client

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    supabase = None
else:
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def upsert_bot_user(tg_user: Dict[str, Any]) -> None:
    """
    Создаёт/обновляет пользователя в public.bot_users по telegram_user_id.
    tg_user ожидается формата Telegram User.
    """
    if supabase is None:
        return

    telegram_user_id = int(tg_user["id"])

    payload = {
        "telegram_user_id": telegram_user_id,
        "username": tg_user.get("username"),
        "first_name": tg_user.get("first_name"),
        "last_name": tg_user.get("last_name"),
        "language_code": tg_user.get("language_code"),
        "is_bot": tg_user.get("is_bot"),
        "is_premium": tg_user.get("is_premium"),
        "last_seen_at": _now_iso(),
        "updated_at": _now_iso(),
    }

    # on_conflict — имя UNIQUE-колонки
    supabase.table("bot_users").upsert(payload, on_conflict="telegram_user_id").execute()


def upsert_daily_active(telegram_user_id: int) -> None:
    """
    Гарантирует 1 строку в public.bot_daily_active на пользователя в день.
    """
    if supabase is None:
        return

    supabase.table("bot_daily_active").upsert(
        {
            "day": str(date.today()),
            "telegram_user_id": int(telegram_user_id),
            "first_event_at": _now_iso(),
        },
        on_conflict="day,telegram_user_id",
    ).execute()


def track_user_activity(tg_user: Dict[str, Any]) -> None:
    """
    Единая точка входа:
    - upsert пользователя
    - upsert DAU (unique/day)
    """
    try:
        upsert_bot_user(tg_user)
        upsert_daily_active(int(tg_user["id"]))
    except Exception:
        # Нельзя ронять бота из-за аналитики/БД
        # При желании добавим логирование в файл/сервис
        return
