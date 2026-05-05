from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Optional

from queue_redis import get_redis

CHAT_JOB_STATUS_PREFIX = (os.getenv("CHAT_JOB_STATUS_PREFIX", "astrabot:chatjob") or "astrabot:chatjob").strip().rstrip(":")
CHAT_JOB_STATUS_TTL_SECONDS = int(os.getenv("CHAT_JOB_STATUS_TTL_SECONDS", "3600") or "3600")


def chat_job_status_key(job_id: str) -> str:
    return f"{CHAT_JOB_STATUS_PREFIX}:{str(job_id).strip()}"


def _normalize_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(payload or {})
    data.setdefault("updated_at", time.time())
    return data


async def create_chat_job_status(job_id: str, payload: Optional[Dict[str, Any]] = None) -> None:
    data = _normalize_payload({"job_id": str(job_id), "status": "queued", **(payload or {})})
    r = await get_redis()
    await r.set(chat_job_status_key(job_id), json.dumps(data, ensure_ascii=False), ex=CHAT_JOB_STATUS_TTL_SECONDS)


async def set_chat_job_status(job_id: str, **patch: Any) -> None:
    r = await get_redis()
    key = chat_job_status_key(job_id)
    current: Dict[str, Any] = {}
    raw = await r.get(key)
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                current = parsed
        except Exception:
            current = {}
    current.update(patch)
    current["job_id"] = str(job_id)
    current["updated_at"] = time.time()
    await r.set(key, json.dumps(current, ensure_ascii=False), ex=CHAT_JOB_STATUS_TTL_SECONDS)


async def get_chat_job_status(job_id: str) -> Optional[Dict[str, Any]]:
    r = await get_redis()
    raw = await r.get(chat_job_status_key(job_id))
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None
