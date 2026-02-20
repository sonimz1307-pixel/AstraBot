from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

from app.services.mi_storage import get_supabase


def enqueue_task(*, job_id: str, task_type: str, payload: Optional[Dict[str, Any]] = None) -> str:
    sb = get_supabase()
    row = {
        "job_id": job_id,
        "task_type": task_type,
        "payload": payload or {},
        "status": "queued",
        "attempts": 0,
    }
    res = sb.table("mi_tasks").insert(row).execute()
    if not res.data:
        raise RuntimeError("Failed to enqueue task")
    return str(res.data[0]["id"])


def claim_next_task(*, worker_id: str) -> Optional[Dict[str, Any]]:
    """Best-effort claim. Safe for single worker. For multi-worker, use an RPC/SQL function."""
    sb = get_supabase()
    rows = (
        sb.table("mi_tasks")
        .select("*")
        .eq("status", "queued")
        .order("created_at", desc=False)
        .limit(1)
        .execute()
    ).data or []
    if not rows:
        return None
    task = rows[0]
    task_id = task.get("id")
    # Try to lock it
    upd = (
        sb.table("mi_tasks")
        .update(
            {
                "status": "running",
                "locked_by": worker_id,
                "locked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "attempts": int(task.get("attempts") or 0) + 1,
            }
        )
        .eq("id", task_id)
        .eq("status", "queued")
        .execute()
    ).data or []
    if not upd:
        return None
    return upd[0]


def finish_task(*, task_id: str, ok: bool, error: str = "") -> None:
    sb = get_supabase()
    patch = {
        "status": "done" if ok else "error",
        "error": (error or None),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    sb.table("mi_tasks").update(patch).eq("id", task_id).execute()
