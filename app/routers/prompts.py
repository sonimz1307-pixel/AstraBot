from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Header, HTTPException, Query

from db_supabase import supabase
from free_plan_limits import (
    FREE_PROMPT_LIFETIME_LIMIT,
    FreePlanLimitError,
    consume_free_prompt_open,
    free_limit_http_detail,
    get_free_prompt_open_status,
    is_free_plan_user,
)
from app.routers.prompts_admin import _check_telegram_init_data, _require_admin

router = APIRouter()

ADMIN_IDS = {
    int(x.strip())
    for x in (os.getenv("ADMIN_IDS", "") or "").split(",")
    if x.strip().isdigit()
}

# Security default: do not trust a plain uid from the frontend for user access.
# Telegram WebApp sends signed initData; admin pages may use X-ADMIN-TOKEN.
# Set PROMPTS_ALLOW_UID_FALLBACK=1 only if you deliberately need the old uid-only behavior.
PROMPTS_ALLOW_UID_FALLBACK = (os.getenv("PROMPTS_ALLOW_UID_FALLBACK", "0") or "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _err(msg: str) -> Dict[str, Any]:
    return {"ok": False, "error": msg}


def _safe_uid(*values: Any) -> int:
    for value in values:
        try:
            text = str(value or "").strip()
            if text and text.isdigit():
                uid = int(text)
                if uid > 0:
                    return uid
        except Exception:
            continue
    return 0


def _uid_from_verified_initdata(x_tg_initdata: Optional[str]) -> int:
    init_data = str(x_tg_initdata or "").strip()
    if not init_data:
        return 0
    verified = _check_telegram_init_data(init_data)
    user = verified.get("user") if isinstance(verified, dict) else {}
    return _safe_uid((user or {}).get("id"))


def _resolve_prompts_user(
    uid: Optional[str] = None,
    x_uid: Optional[str] = None,
    x_tg_initdata: Optional[str] = None,
    x_admin_token: Optional[str] = None,
) -> Tuple[int, bool]:
    """Return (user_id, is_admin).

    Normal users must be verified via Telegram WebApp initData by default.
    X-UID fallback is intentionally disabled unless PROMPTS_ALLOW_UID_FALLBACK=1.
    """
    init_data = str(x_tg_initdata or "").strip()
    admin_token = str(x_admin_token or "").strip()

    if init_data:
        user_id = _uid_from_verified_initdata(init_data)
        if user_id <= 0:
            raise HTTPException(status_code=401, detail={"code": "prompt_auth_required", "message": "Не удалось определить пользователя."})
        return user_id, user_id in ADMIN_IDS

    if admin_token:
        verified = _require_admin(
            x_tg_initdata=init_data,
            x_admin_token=admin_token,
            x_uid=str(x_uid or uid or ""),
        )
        user = verified.get("user") if isinstance(verified, dict) else {}
        return _safe_uid((user or {}).get("id")), True

    fallback_uid = _safe_uid(uid, x_uid)
    if fallback_uid in ADMIN_IDS:
        return fallback_uid, True

    if PROMPTS_ALLOW_UID_FALLBACK and fallback_uid > 0:
        return fallback_uid, False

    raise HTTPException(
        status_code=401,
        detail={
            "code": "prompt_auth_required",
            "message": "Откройте библиотеку промтов через Telegram WebApp или авторизуйтесь заново.",
        },
    )


def _access_payload(user_id: int, is_admin: bool = False) -> Dict[str, Any]:
    if is_admin:
        return {
            "authenticated": True,
            "is_admin": True,
            "is_free_plan": False,
            "is_paid_plan": True,
            "plan_code": "admin",
            "prompt_limit": {
                "allowed": True,
                "feature": "prompt_open",
                "limit": FREE_PROMPT_LIFETIME_LIMIT,
                "used": 0,
                "remaining": FREE_PROMPT_LIFETIME_LIMIT,
                "already_opened": False,
                "plan_code": "admin",
                "is_paid_plan": True,
                "reason": "admin",
            },
        }

    status = get_free_prompt_open_status(user_id)
    return {
        "authenticated": user_id > 0,
        "is_admin": False,
        "is_free_plan": not status.is_paid_plan,
        "is_paid_plan": bool(status.is_paid_plan),
        "plan_code": status.plan_code,
        "prompt_limit": status.as_dict(),
    }


def _require_catalog_access(
    uid: Optional[str] = None,
    x_uid: Optional[str] = None,
    x_tg_initdata: Optional[str] = None,
    x_admin_token: Optional[str] = None,
) -> Tuple[int, bool, Dict[str, Any]]:
    user_id, is_admin = _resolve_prompts_user(uid, x_uid, x_tg_initdata, x_admin_token)
    access = _access_payload(user_id, is_admin)
    return user_id, is_admin, access


def _prompt_limit_exception(exc: FreePlanLimitError) -> HTTPException:
    return HTTPException(status_code=402, detail=free_limit_http_detail(exc))


def _public_catalog_access() -> Dict[str, Any]:
    """Public catalog payload.

    Categories, groups, and cards must be visible without authorization.
    Full prompt text is still protected by /item and free_prompt_opens logic.
    """
    return {
        "authenticated": False,
        "is_admin": False,
        "is_free_plan": True,
        "is_paid_plan": False,
        "plan_code": "guest",
        "prompt_limit": {
            "allowed": False,
            "feature": "prompt_open",
            "limit": FREE_PROMPT_LIFETIME_LIMIT,
            "used": 0,
            "remaining": 0,
            "already_opened": False,
            "plan_code": "guest",
            "is_paid_plan": False,
            "reason": "auth_required_for_full_prompt",
        },
    }


def _optional_catalog_access(
    uid: Optional[str] = None,
    x_uid: Optional[str] = None,
    x_tg_initdata: Optional[str] = None,
    x_admin_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Return real access when auth is valid, otherwise keep catalog public.

    This prevents /categories, /groups, and /items from returning 401 in Telegram
    WebApp when initData is absent/stale. Opening the full prompt is still
    checked by _require_catalog_access() in /item.
    """
    try:
        _, _, access = _require_catalog_access(uid, x_uid, x_tg_initdata, x_admin_token)
        return access
    except HTTPException:
        return _public_catalog_access()


@router.get("/categories")
def categories(
    uid: Optional[str] = Query(default=None),
    x_uid: Optional[str] = Header(default=None, alias="X-UID"),
    x_tg_initdata: Optional[str] = Header(default=None, alias="X-TG-INITDATA"),
    x_admin_token: Optional[str] = Header(default=None, alias="X-ADMIN-TOKEN"),
) -> Dict[str, Any]:
    access = _optional_catalog_access(uid, x_uid, x_tg_initdata, x_admin_token)
    if supabase is None:
        return _err("Supabase disabled (env/client not ready)")
    try:
        r = (
            supabase.table("prompt_categories")
            .select("id,slug,title,sort_order")
            .order("sort_order", desc=False)
            .order("title", desc=False)
            .execute()
        )
        return {"ok": True, "items": r.data or [], "access": access}
    except Exception as e:
        return _err(f"failed: {e}")


@router.get("/groups")
def groups(
    category: str = Query(..., description="Category slug, e.g. photo/video/ai"),
    uid: Optional[str] = Query(default=None),
    x_uid: Optional[str] = Header(default=None, alias="X-UID"),
    x_tg_initdata: Optional[str] = Header(default=None, alias="X-TG-INITDATA"),
    x_admin_token: Optional[str] = Header(default=None, alias="X-ADMIN-TOKEN"),
) -> Dict[str, Any]:
    access = _optional_catalog_access(uid, x_uid, x_tg_initdata, x_admin_token)
    if supabase is None:
        return _err("Supabase disabled (env/client not ready)")
    try:
        cat = (
            supabase.table("prompt_categories")
            .select("id,slug,title")
            .eq("slug", category)
            .limit(1)
            .execute()
        )
        if not cat.data:
            return _err(f"category not found: {category}")
        category_id = cat.data[0]["id"]

        r = (
            supabase.table("prompt_groups")
            .select("id,category_id,title,cover_url,sort_order")
            .eq("category_id", category_id)
            .order("sort_order", desc=False)
            .order("title", desc=False)
            .execute()
        )
        return {"ok": True, "items": r.data or [], "category": cat.data[0], "access": access}
    except Exception as e:
        return _err(f"failed: {e}")


@router.get("/items")
def items(
    group_id: str = Query(..., description="prompt_groups.id"),
    uid: Optional[str] = Query(default=None),
    x_uid: Optional[str] = Header(default=None, alias="X-UID"),
    x_tg_initdata: Optional[str] = Header(default=None, alias="X-TG-INITDATA"),
    x_admin_token: Optional[str] = Header(default=None, alias="X-ADMIN-TOKEN"),
) -> Dict[str, Any]:
    access = _optional_catalog_access(uid, x_uid, x_tg_initdata, x_admin_token)
    if supabase is None:
        return _err("Supabase disabled (env/client not ready)")
    try:
        # Important: do not return prompt_text in the list.
        # Free users spend one lifetime opening only when they open a single prompt via /item.
        r = (
            supabase.table("prompt_items")
            .select("id,group_id,title,preview_url,video_url,model_hint,is_pro,sort_order")
            .eq("group_id", group_id)
            .order("sort_order", desc=False)
            .order("title", desc=False)
            .execute()
        )
        return {"ok": True, "items": r.data or [], "access": access}
    except Exception as e:
        return _err(f"failed: {e}")


@router.get("/item")
def item(
    item_id: str = Query(..., description="prompt_items.id"),
    uid: Optional[str] = Query(default=None),
    x_uid: Optional[str] = Header(default=None, alias="X-UID"),
    x_tg_initdata: Optional[str] = Header(default=None, alias="X-TG-INITDATA"),
    x_admin_token: Optional[str] = Header(default=None, alias="X-ADMIN-TOKEN"),
) -> Dict[str, Any]:
    user_id, is_admin, access = _require_catalog_access(uid, x_uid, x_tg_initdata, x_admin_token)
    if supabase is None:
        return _err("Supabase disabled (env/client not ready)")

    try:
        r = (
            supabase.table("prompt_items")
            .select("id,group_id,title,preview_url,video_url,prompt_text,model_hint,is_pro,sort_order")
            .eq("id", item_id)
            .limit(1)
            .execute()
        )
        rows = list(getattr(r, "data", None) or [])
        if not rows:
            raise HTTPException(status_code=404, detail={"code": "prompt_not_found", "message": "Промт не найден."})

        prompt = rows[0]
        prompt_id = str(prompt.get("id") or item_id)

        if not is_admin and is_free_plan_user(user_id):
            try:
                free_result = consume_free_prompt_open(user_id, prompt_id)
            except FreePlanLimitError as exc:
                raise _prompt_limit_exception(exc)
            access = _access_payload(user_id, is_admin=False)
            access["prompt_limit"] = free_result.as_dict()

        return {"ok": True, "item": prompt, "access": access}
    except HTTPException:
        raise
    except Exception as e:
        return _err(f"failed: {e}")
