from __future__ import annotations

import os
from typing import Optional

from queue_redis import get_redis


REDIS_RELIABLE_STATE_PREFIX = (os.getenv("REDIS_RELIABLE_STATE_PREFIX", "astrabot:jobstate") or "astrabot:jobstate").strip().rstrip(":") or "astrabot:jobstate"
WAN3_POST_START_DEDUPE_TTL_SEC = max(86400, int(os.getenv("WAN3_POST_START_DEDUPE_TTL_SEC", "259200") or "259200"))


def wan3_reliable_dedupe_key(queue_name: str, job_id: str) -> str:
    q = str(queue_name or "wan3").strip() or "wan3"
    jid = str(job_id or "").strip()
    if not jid:
        raise ValueError("Wan 3.0 job_id is required")
    return f"{REDIS_RELIABLE_STATE_PREFIX}:dedupe:{q}:{jid}"


async def wan3_reliable_enqueue_known(*, queue_name: str, job_id: str, make_durable: bool = True) -> Optional[bool]:
    """Return True/False when Redis can prove whether reliable enqueue committed.

    Redis/network errors return None. Callers must never refund on None because
    the XADD may have committed even if its reply was lost.
    """
    key = wan3_reliable_dedupe_key(queue_name, job_id)
    try:
        redis = await get_redis()
        exists = bool(await redis.exists(key))
        if exists and make_durable:
            await redis.persist(key)
        return exists
    except Exception:
        return None


async def wan3_release_enqueue_marker(*, queue_name: str, job_id: str) -> bool:
    """Restore bounded dedupe retention after provider start/terminal settlement.

    Do not refresh an already bounded TTL. That makes this safe to call from the
    reconciler as cleanup for a previous transient Redis failure without keeping
    successful-job dedupe keys alive forever.
    """
    key = wan3_reliable_dedupe_key(queue_name, job_id)
    redis = await get_redis()
    ttl = int(await redis.ttl(key))
    if ttl == -2:
        return False
    if ttl == -1:
        await redis.expire(key, WAN3_POST_START_DEDUPE_TTL_SEC)
    return True
