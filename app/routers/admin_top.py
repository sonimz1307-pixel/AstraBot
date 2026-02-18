from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Header, HTTPException
from fastapi.background import BackgroundTasks

from app.services.mi_storage import get_supabase
from app.routers.leads import run_full_job as _leads_run_full_job

router = APIRouter()

def _env(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()

def _require_admin_token(x_admin_token: Optional[str]) -> None:
    token = _env("ADMIN_TOKEN")
    if not token:
        # If token not configured, do not expose admin endpoints
        raise HTTPException(status_code=503, detail="ADMIN_TOKEN is not configured")
    if (x_admin_token or "").strip() != token:
        raise HTTPException(status_code=401, detail="Invalid admin token")

@router.post("/run_job")
async def run_job(
    payload: Dict[str, Any] = Body(...),
    background_tasks: BackgroundTasks = None,
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    _require_admin_token(x_admin_token)
    # Reuse existing orchestration
    return await _leads_run_full_job(payload, background_tasks)

@router.get("/jobs")
async def list_jobs(
    limit: int = 50,
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    _require_admin_token(x_admin_token)
    sb = get_supabase()
    limit = max(1, min(int(limit or 50), 200))

    st = sb.table("mi_job_state").select("*").order("updated_at", desc=True).limit(limit).execute()
    states = st.data or []
    job_ids = [x.get("job_id") for x in states if x.get("job_id")]

    jobs_map: Dict[str, Dict[str, Any]] = {}
    if job_ids:
        jb = sb.table("mi_jobs").select("id,created_at,city,query,queries,tg_user_id").in_("id", job_ids).execute()
        for j in (jb.data or []):
            jobs_map[str(j.get("id"))] = j

    out: List[Dict[str, Any]] = []
    for s in states:
        jid = str(s.get("job_id"))
        j = jobs_map.get(jid) or {}
        meta = s.get("meta") or {}
        out.append({
            "job_id": jid,
            "status": s.get("status"),
            "total": s.get("total"),
            "done": s.get("done"),
            "failed": s.get("failed"),
            "updated_at": s.get("updated_at"),
            "meta": meta,
            "created_at": j.get("created_at"),
            "city": j.get("city"),
            "query": j.get("query"),
            "queries": j.get("queries"),
            "tg_user_id": j.get("tg_user_id"),
        })

    return {"ok": True, "items": out}

@router.get("/job/{job_id}")
async def job_detail(
    job_id: str,
    include_raw: int = 0,
    raw_limit: int = 50,
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    _require_admin_token(x_admin_token)
    sb = get_supabase()
    job_id = (job_id or "").strip()

    st = sb.table("mi_job_state").select("*").eq("job_id", job_id).limit(1).execute()
    state = (st.data or [None])[0]

    pl = sb.table("mi_places").select("*").eq("job_id", job_id).order("created_at", desc=False).limit(5000).execute()
    places = pl.data or []

    raw_items: List[Dict[str, Any]] = []
    if int(include_raw or 0) == 1:
        raw_limit = max(1, min(int(raw_limit or 50), 500))
        rw = sb.table("mi_raw_items").select("id,created_at,source,place_key,run_id,actor_id,query,item").eq("job_id", job_id).order("created_at", desc=True).limit(raw_limit).execute()
        raw_items = rw.data or []

    return {"ok": True, "job_id": job_id, "state": state, "places": places, "raw_items": raw_items}
