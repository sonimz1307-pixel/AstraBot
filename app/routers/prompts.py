from __future__ import annotations

from fastapi import APIRouter, Query
from typing import Any, Dict

from db_supabase import supabase

router = APIRouter()

def _err(msg: str) -> Dict[str, Any]:
    return {"ok": False, "error": msg}

@router.get("/categories")
def categories() -> Dict[str, Any]:
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
        return {"ok": True, "items": r.data or []}
    except Exception as e:
        return _err(f"failed: {e}")

@router.get("/groups")
def groups(category: str = Query(..., description="Category slug, e.g. photo/video/ai")) -> Dict[str, Any]:
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
        return {"ok": True, "items": r.data or [], "category": cat.data[0]}
    except Exception as e:
        return _err(f"failed: {e}")

@router.get("/items")
def items(group_id: str = Query(..., description="prompt_groups.id")) -> Dict[str, Any]:
    if supabase is None:
        return _err("Supabase disabled (env/client not ready)")
    try:
        r = (
            supabase.table("prompt_items")
            .select("id,group_id,title,preview_url,prompt_text,model_hint,is_pro,sort_order")
            .eq("group_id", group_id)
            .order("sort_order", desc=False)
            .order("title", desc=False)
            .execute()
        )
        return {"ok": True, "items": r.data or []}
    except Exception as e:
        return _err(f"failed: {e}")
