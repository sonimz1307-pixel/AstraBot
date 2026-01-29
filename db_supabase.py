import os
from datetime import date, datetime, timezone, timedelta
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


def get_basic_stats() -> dict:
    """
    Возвращает базовую статистику:
    - total_users: всего уникальных пользователей
    - dau_today: уникальных за сегодня
    - dau_yesterday: уникальных за вчера
    - last7: DAU по дням за последние 7 дней
    """
    if supabase is None:
        return {"ok": False, "error": "Supabase disabled (env/client not ready)"}

    today = date.today()
    yesterday = today - timedelta(days=1)
    week_start = today - timedelta(days=6)

    # Всего пользователей
    r_total = supabase.table("bot_users").select("telegram_user_id", count="exact").execute()
    total_users = r_total.count or 0

    # DAU сегодня/вчера
    r_today = (
        supabase.table("bot_daily_active")
        .select("telegram_user_id", count="exact")
        .eq("day", str(today))
        .execute()
    )
    r_yest = (
        supabase.table("bot_daily_active")
        .select("telegram_user_id", count="exact")
        .eq("day", str(yesterday))
        .execute()
    )
    dau_today = r_today.count or 0
    dau_yesterday = r_yest.count or 0

    # Последние 7 дней
    r_week = (
        supabase.table("bot_daily_active")
        .select("day,telegram_user_id")
        .gte("day", str(week_start))
        .lte("day", str(today))
        .execute()
    )

    by_day = {}
    for row in (r_week.data or []):
        d = str(row.get("day"))
        by_day[d] = by_day.get(d, 0) + 1

    last7 = dict(sorted(by_day.items(), reverse=True))

    return {
        "ok": True,
        "total_users": total_users,
        "dau_today": dau_today,
        "dau_yesterday": dau_yesterday,
        "last7": last7,
    }
