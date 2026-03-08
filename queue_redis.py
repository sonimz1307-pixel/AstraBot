import os
import json
import time
from typing import Any, Dict, Optional, Sequence

import redis.asyncio as redis

_QUEUE_PREFIX = os.getenv("REDIS_QUEUE_PREFIX", "astrabot:queue").strip().rstrip(":")
_DEFAULT_QUEUE_NAME = os.getenv("REDIS_QUEUE_NAME", "gen").strip() or "gen"


def _redis_url() -> str:
    url = os.getenv("REDIS_URL")
    if not url:
        raise RuntimeError("REDIS_URL is not set")
    return url


def _queue_key(queue_name: Optional[str] = None) -> str:
    q = (queue_name or _DEFAULT_QUEUE_NAME or "gen").strip() or "gen"
    return f"{_QUEUE_PREFIX}:{q}"


async def get_redis() -> "redis.Redis":
    # decode_responses=True -> str keys/values (we store JSON strings)
    return redis.from_url(_redis_url(), decode_responses=True)


async def enqueue_job(job: Dict[str, Any], queue_name: Optional[str] = None) -> str:
    """
    Push job (dict) into Redis list.
    Returns job_id (string). If missing, generates one.
    """
    r = await get_redis()
    job_id = str(job.get("job_id") or job.get("id") or "")
    if not job_id:
        job_id = f"job_{int(time.time() * 1000)}"
        job["job_id"] = job_id
    await r.rpush(_queue_key(queue_name), json.dumps(job, ensure_ascii=False))
    return job_id


async def dequeue_job(
    timeout_sec: int = 10,
    queue_name: Optional[str] = None,
    queue_names: Optional[Sequence[str]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Blocking pop with timeout. Returns dict job or None.
    Supports one queue_name or multiple queue_names.
    """
    r = await get_redis()
    keys: list[str]
    if queue_names:
        keys = [_queue_key(q) for q in queue_names if str(q or "").strip()]
    else:
        keys = [_queue_key(queue_name)]
    res = await r.blpop(keys, timeout=timeout_sec)
    if not res:
        return None
    _key, payload = res
    try:
        return json.loads(payload)
    except Exception:
        return {"job_id": "bad_payload", "raw": payload}
