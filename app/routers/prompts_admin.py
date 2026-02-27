from __future__ import annotations

import os
import hmac
import hashlib
import time
import urllib.parse
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException, UploadFile, File, Form
from db_supabase import supabase

router = APIRouter()

ADMIN_IDS = set(
    int(x.strip())
    for x in (os.getenv("ADMIN_IDS", "")).split(",")
    if x.strip().isdigit()
)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
PROMPTS_BUCKET = os.getenv("PROMPTS_BUCKET", "prompts").strip()

def _err(msg: str) -> Dict[str, Any]:
    return {"ok": False, "error": msg}

def _ok(**kwargs) -> Dict[str, Any]:
    d = {"ok": True}
    d.update(kwargs)
    return d

def _parse_init_data(init_data: str) -> Dict[str, str]:
    parsed = urllib.parse.parse_qsl(init_data, keep_blank_values=True)
    return {k: v for k, v in parsed}

def _check_telegram_init_data(init_data: str) -> Dict[str, Any]:
    # Telegram WebApp initData verification
    if not init_data:
        raise HTTPException(status_code=401, detail="missing initData")
    if not TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=500, detail="server misconfigured: TELEGRAM_BOT_TOKEN missing")

    data = _parse_init_data(init_data)
    received_hash = data.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="missing hash")

    pairs = [f"{k}={data[k]}" for k in sorted(data.keys())]
    data_check_string = "\n".join(pairs)

    secret_key = hmac.new(b"WebAppData", TELEGRAM_BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        raise HTTPException(status_code=401, detail="invalid initData signature")

    auth_date = data.get("auth_date")
    if auth_date and auth_date.isdigit():
        if int(time.time()) - int(auth_date) > 24 * 3600:
            raise HTTPException(status_code=401, detail="initData expired")

    user_raw = data.get("user")
    user = {}
    if user_raw:
        try:
            import json
            user = json.loads(user_raw)
        except Exception:
            user = {}

    return {"data": data, "user": user}

def _require_admin(x_tg_initdata: str) -> Dict[str, Any]:
    verified = _check_telegram_init_data(x_tg_initdata)
    uid = verified.get("user", {}).get("id")
    if uid is None:
        raise HTTPException(status_code=401, detail="user missing in initData")
    try:
        uid_int = int(uid)
    except Exception:
        raise HTTPException(status_code=401, detail="bad user id")
    if uid_int not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="forbidden")
    return verified

@router.get("/me")
def me(x_tg_initdata: str = Header("", alias="X-TG-INITDATA")) -> Dict[str, Any]:
    v = _require_admin(x_tg_initdata)
    return _ok(user=v.get("user", {}))

@router.post("/create_group")
def create_group(
    x_tg_initdata: str = Header("", alias="X-TG-INITDATA"),
    category_slug: str = Form(...),
    title: str = Form(...),
    cover_url: Optional[str] = Form(None),
    sort_order: int = Form(100),
) -> Dict[str, Any]:
    _require_admin(x_tg_initdata)
    if supabase is None:
        return _err("Supabase disabled")
    try:
        cat = (
            supabase.table("prompt_categories")
            .select("id")
            .eq("slug", category_slug)
            .limit(1)
            .execute()
        )
        if not cat.data:
            return _err(f"category not found: {category_slug}")
        category_id = cat.data[0]["id"]

        ins = (
            supabase.table("prompt_groups")
            .insert({
                "category_id": category_id,
                "title": title,
                "cover_url": cover_url,
                "sort_order": sort_order,
            })
            .execute()
        )
        return _ok(item=(ins.data or [None])[0])
    except Exception as e:
        return _err(f"failed: {e}")

@router.post("/create_item")
async def create_item(
    x_tg_initdata: str = Header("", alias="X-TG-INITDATA"),
    group_id: str = Form(...),
    title: str = Form(...),
    prompt_text: str = Form(...),
    model_hint: str = Form(""),
    sort_order: int = Form(100),
    preview: Optional[UploadFile] = File(None),
) -> Dict[str, Any]:
    _require_admin(x_tg_initdata)
    if supabase is None:
        return _err("Supabase disabled")

    preview_url: Optional[str] = None

    try:
        if preview is not None:
            content = await preview.read()
            if not content:
                return _err("empty file")
            ext = (preview.filename or "").split(".")[-1].lower()
            if ext not in ("png","jpg","jpeg","webp"):
                ext = "png"
            import secrets
            path = f"{group_id}/{int(time.time())}_{secrets.token_hex(6)}.{ext}"

            storage = supabase.storage.from_(PROMPTS_BUCKET)
            storage.upload(
                path,
                content,
                file_options={"content-type": preview.content_type or "image/png", "upsert": True},
            )
            try:
                preview_url = storage.get_public_url(path)
                if isinstance(preview_url, dict):
                    preview_url = preview_url.get("publicUrl") or preview_url.get("public_url")
            except Exception:
                preview_url = None

        ins = (
            supabase.table("prompt_items")
            .insert({
                "group_id": group_id,
                "title": title,
                "preview_url": preview_url,
                "prompt_text": prompt_text,
                "model_hint": model_hint,
                "is_pro": False,
                "sort_order": sort_order,
            })
            .execute()
        )
        return _ok(item=(ins.data or [None])[0])
    except Exception as e:
        return _err(f"failed: {e}")

@router.post("/delete_item")
def delete_item(
    x_tg_initdata: str = Header("", alias="X-TG-INITDATA"),
    item_id: str = Form(...),
) -> Dict[str, Any]:
    _require_admin(x_tg_initdata)
    if supabase is None:
        return _err("Supabase disabled")
    try:
        supabase.table("prompt_items").delete().eq("id", item_id).execute()
        return _ok(deleted=True)
    except Exception as e:
        return _err(f"failed: {e}")
