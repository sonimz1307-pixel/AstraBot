from __future__ import annotations

import os
import hmac
import hashlib
import time
import urllib.parse
from typing import Any, Dict

from fastapi import APIRouter, Header, HTTPException, UploadFile, File, Form
from db_supabase import supabase

router = APIRouter()

ADMIN_IDS = set(
    int(x.strip())
    for x in (os.getenv("ADMIN_IDS", "")).split(",")
    if x.strip().isdigit()
)

# Token бота нужен только для проверки подписи Telegram WebApp initData
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# Опциональный простой bypass (если на Desktop initData иногда пустой)
# Включается только если задан ADMIN_TOKEN в env и клиент передал X-ADMIN-TOKEN.
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()

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


def _require_admin(
    x_tg_initdata: str,
    x_admin_token: str,
) -> Dict[str, Any]:
    # 1) Если передан корректный ADMIN_TOKEN — допускаем без initData.
    if ADMIN_TOKEN and x_admin_token and hmac.compare_digest(x_admin_token.strip(), ADMIN_TOKEN):
        return {"data": {"auth": "admin_token"}, "user": {"id": None, "username": "admin_token"}}

    # 2) Иначе — обычная проверка initData + admin ids
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
def me(
    x_tg_initdata: str = Header("", alias="X-TG-INITDATA"),
    x_admin_token: str = Header("", alias="X-ADMIN-TOKEN"),
) -> Dict[str, Any]:
    v = _require_admin(x_tg_initdata, x_admin_token)
    return _ok(user=v.get("user", {}), auth=v.get("data", {}).get("auth"))


@router.post("/create_group")
async def create_group(
    category_slug: str = Form(""),
    title: str = Form(""),
    cover_url: str = Form(""),
    sort_order: int = Form(0),
    x_tg_initdata: str = Header("", alias="X-TG-INITDATA"),
    x_admin_token: str = Header("", alias="X-ADMIN-TOKEN"),
) -> Dict[str, Any]:
    _require_admin(x_tg_initdata, x_admin_token)

    category_slug = (category_slug or "").strip()
    title = (title or "").strip()
    cover_url = (cover_url or "").strip()

    if not category_slug:
        return _err("category_slug is required")
    if not title:
        return _err("title is required")

    payload = {
        "category_id": None,  # если у тебя category_id есть — можно маппить slug->id в отдельной таблице
        "slug": category_slug,  # оставляем совместимость
        "title": title,
        "cover_url": cover_url or None,
        "sort_order": int(sort_order or 0),
    }

    try:
        r = supabase.table("prompt_groups").insert(payload).execute()
        item = (r.data or [None])[0]
        return _ok(item=item)
    except Exception as e:
        return _err(f"db insert failed: {e}")


@router.post("/create_item")
async def create_item(
    group_id: str = Form(""),
    title: str = Form(""),
    prompt_text: str = Form(""),
    preview: UploadFile | None = File(None),
    x_tg_initdata: str = Header("", alias="X-TG-INITDATA"),
    x_admin_token: str = Header("", alias="X-ADMIN-TOKEN"),
) -> Dict[str, Any]:
    _require_admin(x_tg_initdata, x_admin_token)

    group_id = (group_id or "").strip()
    title = (title or "").strip()
    prompt_text = (prompt_text or "").strip()

    if not group_id:
        return _err("group_id is required")
    if not title:
        return _err("title is required")
    if not prompt_text:
        return _err("prompt_text is required")

    preview_url = None
    try:
        if preview is not None:
            content = await preview.read()
            if content:
                # Сохраняем в Supabase Storage
                # bucket: PROMPTS_BUCKET
                # path: previews/<group_id>/<timestamp>_<filename>
                safe_name = (preview.filename or "preview").replace("/", "_")
                path = f"previews/{group_id}/{int(time.time())}_{safe_name}"
                supabase.storage.from_(PROMPTS_BUCKET).upload(
                    path,
                    content,
                    {
                        "content-type": preview.content_type or "application/octet-stream",
                        "x-upsert": "true",
                    },
                )
                preview_url = supabase.storage.from_(PROMPTS_BUCKET).get_public_url(path)
    except Exception as e:
        # не фейлим создание промпта из-за превью
        preview_url = None

    payload = {
        "group_id": group_id,
        "title": title,
        "prompt_text": prompt_text,
        "preview_url": preview_url,
    }

    try:
        r = supabase.table("prompt_items").insert(payload).execute()
        item = (r.data or [None])[0]
        return _ok(item=item)
    except Exception as e:
        return _err(f"db insert failed: {e}")
