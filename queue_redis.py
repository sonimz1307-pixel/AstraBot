import os
import json
import time
from typing import Any, Dict, Optional

import redis.asyncio as redis

_QUEUE_KEY = os.getenv("REDIS_QUEUE_KEY", "astrabot:jobs")


def _redis_url() -> str:
    url = os.getenv("REDIS_URL")
    if not url:
        raise RuntimeError("REDIS_URL is not set")
    return url


async def get_redis() -> "redis.Redis":
    # decode_responses=True -> str keys/values (we store JSON strings)
    return redis.from_url(_redis_url(), decode_responses=True)


async def enqueue_job(job: Dict[str, Any]) -> str:
    """
    Push job (dict) into Redis list.
    Returns job_id (string). If missing, generates one.
    """
    r = await get_redis()
    job_id = str(job.get("job_id") or job.get("id") or "")
    if not job_id:
        job_id = f"job_{int(time.time()*1000)}"
        job["job_id"] = job_id
    await r.rpush(_QUEUE_KEY, json.dumps(job, ensure_ascii=False))
    return job_id


async def dequeue_job(timeout_sec: int = 10) -> Optional[Dict[str, Any]]:
    """
    Blocking pop with timeout. Returns dict job or None.
    """
    r = await get_redis()
    res = await r.blpop(_QUEUE_KEY, timeout=timeout_sec)
    if not res:
        return None
    _key, payload = res
    try:
        return json.loads(payload)
    except Exception:
        return {"job_id": "bad_payload", "raw": payload}
