# subscriptions_db.py
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from db_supabase import supabase

DEFAULT_SUBSCRIPTION_PLANS: List[Dict[str, Any]] = [
    {
        "code": "free",
        "name": "Free",
        "price_rub": 0,
        "tokens": 0,
        "duration_days": 0,
        "is_active": True,
        "features": {"label": "Базовый доступ"},
    },
    {
        "code": "spark",
        "name": "Spark",
        "price_rub": 1140,
        "tokens": 120,
        "duration_days": 30,
        "is_active": True,
        "features": {"label": "Стартовый тариф"},
    },
    {
        "code": "pulse",
        "name": "Pulse",
        "price_rub": 2225,
        "tokens": 250,
        "duration_days": 30,
        "is_active": True,
        "features": {"label": "Оптимальный тариф"},
    },
    {
        "code": "nexus",
        "name": "Nexus",
        "price_rub": 5250,
        "tokens": 620,
        "duration_days": 30,
        "is_active": True,
        "features": {"label": "Максимальный тариф"},
    },
]


def _require_client():
    if supabase is None:
        raise RuntimeError("Supabase disabled: SUPABASE_URL / SUPABASE_SERVICE_KEY not set")
    return supabase


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime] = None) -> str:
    return (dt or _now()).isoformat()


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        text = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _default_plan(code: str) -> Optional[Dict[str, Any]]:
    code = str(code or "").strip().lower()
    for plan in DEFAULT_SUBSCRIPTION_PLANS:
        if plan["code"] == code:
            return dict(plan)
    return None


def ensure_default_subscription_plans() -> None:
    """Insert missing default plans only. Existing tariff rows must not be overwritten."""
    sb = _require_client()
    now_iso = _iso()
    for plan in DEFAULT_SUBSCRIPTION_PLANS:
        code = str(plan.get("code") or "").strip().lower()
        if not code:
            continue
        try:
            existing = sb.table("subscription_plans").select("code").eq("code", code).limit(1).execute()
            if list(getattr(existing, "data", None) or []):
                continue
            row = dict(plan)
            row["created_at"] = now_iso
            row["updated_at"] = now_iso
            sb.table("subscription_plans").insert(row).execute()
        except Exception:
            # Seeding is optional; SQL migration is the source of truth.
            pass


def list_subscription_plans(*, include_inactive: bool = False) -> List[Dict[str, Any]]:
    """Return plans from DB; fallback to constants if table is not available yet."""
    try:
        sb = _require_client()
        q = sb.table("subscription_plans").select("*").order("price_rub")
        if not include_inactive:
            q = q.eq("is_active", True)
        rows = list(getattr(q.execute(), "data", None) or [])
        if rows:
            return rows
    except Exception:
        pass
    return [dict(x) for x in DEFAULT_SUBSCRIPTION_PLANS if include_inactive or bool(x.get("is_active"))]


def get_subscription_plan(code: str) -> Dict[str, Any]:
    code = str(code or "").strip().lower()
    if not code:
        raise ValueError("plan_code is empty")
    try:
        sb = _require_client()
        res = sb.table("subscription_plans").select("*").eq("code", code).limit(1).execute()
        rows = list(getattr(res, "data", None) or [])
        if rows:
            return rows[0]
    except Exception:
        fallback = _default_plan(code)
        if fallback:
            return fallback
        raise
    fallback = _default_plan(code)
    if fallback:
        return fallback
    raise ValueError(f"Unknown subscription plan: {code}")


def _free_subscription(user_id: int, *, error: Optional[str] = None) -> Dict[str, Any]:
    plan = _default_plan("free") or {"code": "free", "name": "Free", "tokens": 0, "price_rub": 0}
    return {
        "is_active": False,
        "status": "free",
        "user_id": int(user_id),
        "plan_code": "free",
        "plan": plan,
        "subscription": None,
        "starts_at": None,
        "expires_at": None,
        "days_left": 0,
        "error": error,
    }


def get_current_subscription(user_id: int) -> Dict[str, Any]:
    uid = _safe_int(user_id)
    if uid <= 0:
        raise ValueError("user_id must be positive")
    now_iso = _iso()
    try:
        sb = _require_client()
        res = (
            sb.table("user_subscriptions")
            .select("*")
            .eq("user_id", uid)
            .eq("status", "active")
            .gt("expires_at", now_iso)
            .order("expires_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = list(getattr(res, "data", None) or [])
    except Exception as exc:
        return _free_subscription(uid, error=str(exc))
    if not rows:
        return _free_subscription(uid)
    sub = rows[0]
    plan_code = str(sub.get("plan_code") or "free").lower()
    try:
        plan = get_subscription_plan(plan_code)
    except Exception:
        plan = _default_plan(plan_code) or {"code": plan_code, "name": plan_code.title()}
    expires_at = _parse_dt(sub.get("expires_at"))
    days_left = 0
    if expires_at:
        seconds_left = max(0, int((expires_at - _now()).total_seconds()))
        days_left = int((seconds_left + 86399) // 86400)
    return {
        "is_active": True,
        "status": "active",
        "user_id": uid,
        "plan_code": plan_code,
        "plan": plan,
        "subscription": sub,
        "starts_at": sub.get("starts_at"),
        "expires_at": sub.get("expires_at"),
        "days_left": days_left,
    }


def _insert_subscription_event(
    *,
    user_id: int,
    plan_code: Optional[str],
    event_type: str,
    source: str,
    payment_id: Optional[str] = None,
    admin_id: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    try:
        _require_client().table("subscription_events").insert(
            {
                "user_id": int(user_id),
                "plan_code": plan_code,
                "event_type": str(event_type or "").strip()[:80],
                "source": str(source or "manual").strip()[:80],
                "payment_id": payment_id,
                "admin_id": admin_id,
                "meta": meta or {},
                "created_at": _iso(),
            }
        ).execute()
    except Exception:
        # Event logging must never break plan management.
        pass


def cancel_user_subscription(
    user_id: int,
    *,
    source: str = "admin",
    admin_id: Optional[str] = None,
    comment: Optional[str] = None,
) -> Dict[str, Any]:
    uid = _safe_int(user_id)
    if uid <= 0:
        raise ValueError("user_id must be positive")
    sb = _require_client()
    before = get_current_subscription(uid)
    now_iso = _iso()
    cancel_comment = comment or ""

    try:
        active_res = sb.table("user_subscriptions").select("id,meta").eq("user_id", uid).eq("status", "active").execute()
        active_rows = list(getattr(active_res, "data", None) or [])
    except Exception:
        active_rows = []

    if active_rows:
        for row in active_rows:
            sub_id = row.get("id")
            old_meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
            new_meta = dict(old_meta or {})
            new_meta.update({"cancel_comment": cancel_comment, "cancelled_at": now_iso})
            try:
                sb.table("user_subscriptions").update(
                    {"status": "cancelled", "updated_at": now_iso, "meta": new_meta}
                ).eq("id", sub_id).execute()
            except Exception:
                sb.table("user_subscriptions").update({"status": "cancelled", "updated_at": now_iso}).eq("id", sub_id).execute()
    else:
        try:
            sb.table("user_subscriptions").update({"status": "cancelled", "updated_at": now_iso}).eq("user_id", uid).eq("status", "active").execute()
        except Exception:
            pass

    _insert_subscription_event(
        user_id=uid,
        plan_code=before.get("plan_code"),
        event_type="plan_cancelled",
        source=source,
        admin_id=admin_id,
        meta={"comment": cancel_comment, "before": before},
    )
    return {"ok": True, "before": before, "current": get_current_subscription(uid)}


def set_user_subscription(
    user_id: int,
    plan_code: str,
    *,
    duration_days: Optional[int] = None,
    source: str = "admin",
    payment_id: Optional[str] = None,
    admin_id: Optional[str] = None,
    comment: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    uid = _safe_int(user_id)
    if uid <= 0:
        raise ValueError("user_id must be positive")
    plan_code_norm = str(plan_code or "").strip().lower()
    if plan_code_norm == "free":
        return cancel_user_subscription(uid, source=source, admin_id=admin_id, comment=comment or "set_free")

    plan = get_subscription_plan(plan_code_norm)
    days = _safe_int(duration_days, _safe_int(plan.get("duration_days"), 30))
    days = max(1, min(days, 3660))
    starts = _now()
    expires = starts + timedelta(days=days)
    sb = _require_client()
    before = get_current_subscription(uid)

    try:
        sb.table("user_subscriptions").update({"status": "replaced", "updated_at": _iso()}).eq("user_id", uid).eq("status", "active").execute()
    except Exception:
        pass

    payload_meta: Dict[str, Any] = dict(meta or {})
    payload_meta.update({"comment": comment or "", "duration_days": days})
    row = {
        "user_id": uid,
        "plan_code": plan_code_norm,
        "status": "active",
        "starts_at": _iso(starts),
        "expires_at": _iso(expires),
        "source": source,
        "payment_id": payment_id,
        "created_by": str(admin_id or "") or None,
        "meta": payload_meta,
        "created_at": _iso(),
        "updated_at": _iso(),
    }
    created = sb.table("user_subscriptions").insert(row).execute()
    created_rows = list(getattr(created, "data", None) or [])
    after = get_current_subscription(uid)
    _insert_subscription_event(
        user_id=uid,
        plan_code=plan_code_norm,
        event_type="plan_set" if source == "admin" else "plan_purchased",
        source=source,
        payment_id=payment_id,
        admin_id=admin_id,
        meta={"comment": comment or "", "before": before, "created": created_rows[0] if created_rows else row},
    )
    return {"ok": True, "plan": plan, "created": created_rows[0] if created_rows else row, "before": before, "current": after}


def extend_user_subscription(
    user_id: int,
    *,
    days: int = 30,
    source: str = "admin",
    admin_id: Optional[str] = None,
    comment: Optional[str] = None,
) -> Dict[str, Any]:
    uid = _safe_int(user_id)
    days_int = max(1, min(_safe_int(days, 30), 3660))
    current = get_current_subscription(uid)
    sub = current.get("subscription") or {}
    sub_id = sub.get("id")
    if not current.get("is_active") or not sub_id:
        raise ValueError("У пользователя нет активного тарифа для продления")
    expires_at = _parse_dt(sub.get("expires_at")) or _now()
    base = max(expires_at, _now())
    new_expires = base + timedelta(days=days_int)
    sb = _require_client()
    updated = sb.table("user_subscriptions").update({"expires_at": _iso(new_expires), "updated_at": _iso()}).eq("id", sub_id).execute()
    updated_rows = list(getattr(updated, "data", None) or [])
    after = get_current_subscription(uid)
    _insert_subscription_event(
        user_id=uid,
        plan_code=current.get("plan_code"),
        event_type="plan_extended",
        source=source,
        admin_id=admin_id,
        meta={"days": days_int, "comment": comment or "", "before": current, "updated": updated_rows[0] if updated_rows else {}},
    )
    return {"ok": True, "before": current, "current": after}
