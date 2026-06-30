import asyncio
import json
import os
import time
from typing import Any, Dict, Optional

from queue_redis import get_redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError


_QUEUE_PREFIX = os.getenv("REDIS_QUEUE_PREFIX", "astrabot:queue").strip().rstrip(":")
_DEFAULT_QUEUE_NAME = (os.getenv("TG_UPDATE_QUEUE_NAME", "tg_update") or "tg_update").strip()
_REDIS_RECONNECT_SLEEP_SEC = float(os.getenv("REDIS_RECONNECT_SLEEP_SEC", "2") or "2")


def _safe_queue_name(queue_name: Optional[str] = None) -> str:
    return (queue_name or _DEFAULT_QUEUE_NAME or "tg_update").strip() or "tg_update"


def _keys(queue_name: Optional[str] = None) -> Dict[str, str]:
    q = _safe_queue_name(queue_name)
    return {
        # Keep the ready-list key compatible with queue_redis naming style:
        # astrabot:queue:tg_update
        "ready": f"{_QUEUE_PREFIX}:{q}",
        "processing": f"{_QUEUE_PREFIX}:processing:{q}",
        "dead": f"{_QUEUE_PREFIX}:dead:{q}",
        "jobs": f"{_QUEUE_PREFIX}:jobs:{q}",
        "attempts": f"{_QUEUE_PREFIX}:attempts:{q}",
    }


def _json_dumps(data: Dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


async def enqueue_tg_update_job(job: Dict[str, Any], queue_name: Optional[str] = None) -> str:
    """
    Reliable enqueue for Telegram updates.

    The ready list stores only job_id. The full payload is stored in a Redis hash.
    If Telegram retries the same update_id/webhook request, we do not enqueue
    duplicate job_ids while the job is still ready/processing.
    """
    if not isinstance(job, dict):
        raise TypeError("job must be dict")

    job_id = str(job.get("job_id") or job.get("id") or "").strip()
    if not job_id:
        job_id = f"tg_update:{int(time.time() * 1000)}"
        job["job_id"] = job_id

    now_ts = time.time()
    job.setdefault("enqueued_ts", now_ts)
    job.setdefault("created_ts", now_ts)

    payload = _json_dumps(job)
    keys = _keys(queue_name)

    # HSET payload and LPUSH job_id are done atomically. If the job already
    # exists, we update payload but do not push duplicate job_id into ready list.
    script = """
local jobs_key = KEYS[1]
local ready_key = KEYS[2]
local job_id = ARGV[1]
local payload = ARGV[2]
local exists = redis.call('HEXISTS', jobs_key, job_id)
redis.call('HSET', jobs_key, job_id, payload)
if exists == 0 then
    redis.call('LPUSH', ready_key, job_id)
end
return exists
"""
    r = await get_redis()
    await r.eval(script, 2, keys["jobs"], keys["ready"], job_id, payload)
    return job_id


async def dequeue_tg_update_job(
    *,
    timeout_sec: float = 5,
    queue_name: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Atomically move one job_id from ready -> processing and return its payload.

    This uses polling + Lua instead of BLPOP so that moving to processing and
    recording the processing timestamp happen as one Redis operation.
    """
    keys = _keys(queue_name)
    deadline = time.time() + max(0.0, float(timeout_sec or 0))

    script = """
local ready_key = KEYS[1]
local processing_key = KEYS[2]
local jobs_key = KEYS[3]
local attempts_key = KEYS[4]
local now_ts = ARGV[1]
local job_id = redis.call('RPOP', ready_key)
if not job_id then
    return nil
end
local payload = redis.call('HGET', jobs_key, job_id)
if not payload then
    return {job_id, '', '0'}
end
local attempt = redis.call('HINCRBY', attempts_key, job_id, 1)
redis.call('ZADD', processing_key, now_ts, job_id)
return {job_id, payload, tostring(attempt)}
"""

    while True:
        try:
            r = await get_redis()
            res = await r.eval(
                script,
                4,
                keys["ready"],
                keys["processing"],
                keys["jobs"],
                keys["attempts"],
                time.time(),
            )
        except (RedisTimeoutError, RedisConnectionError) as exc:
            print(f"[tg_update_queue] Redis dequeue error queue={keys['ready']}: {exc}", flush=True)
            await asyncio.sleep(_REDIS_RECONNECT_SLEEP_SEC)
            return None

        if res:
            job_id = str(res[0] or "")
            payload = str(res[1] or "")
            attempt = int(res[2] or 0)
            if not payload:
                # Stale job_id without payload. Remove from processing and continue.
                try:
                    await ack_tg_update_job(job_id, queue_name=queue_name)
                except Exception:
                    pass
                continue
            try:
                job = json.loads(payload)
                if not isinstance(job, dict):
                    job = {"job_id": job_id, "raw": payload}
            except Exception:
                job = {"job_id": job_id, "raw": payload}
            job["_queue_job_id"] = job_id
            job["_attempt"] = attempt
            return job

        if time.time() >= deadline:
            return None
        await asyncio.sleep(0.25)


async def ack_tg_update_job(job_id: str, queue_name: Optional[str] = None) -> None:
    """Remove a successfully processed job from processing/payload storage."""
    if not job_id:
        return
    keys = _keys(queue_name)
    script = """
redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('HDEL', KEYS[2], ARGV[1])
redis.call('HDEL', KEYS[3], ARGV[1])
return 1
"""
    r = await get_redis()
    await r.eval(script, 3, keys["processing"], keys["jobs"], keys["attempts"], job_id)


async def heartbeat_tg_update_job(job_id: str, queue_name: Optional[str] = None) -> None:
    """Refresh processing timestamp while the worker is alive."""
    if not job_id:
        return
    keys = _keys(queue_name)
    script = """
local exists = redis.call('ZSCORE', KEYS[1], ARGV[1])
if exists then
    redis.call('ZADD', KEYS[1], ARGV[2], ARGV[1])
    return 1
end
return 0
"""
    r = await get_redis()
    await r.eval(script, 1, keys["processing"], job_id, time.time())


async def fail_tg_update_job(
    job: Dict[str, Any],
    *,
    error: str = "",
    max_attempts: int = 3,
    queue_name: Optional[str] = None,
) -> str:
    """
    Return failed job to ready queue or move it to dead-letter after max_attempts.
    Returns: "retry" or "dead".
    """
    job_id = str(job.get("_queue_job_id") or job.get("job_id") or "").strip()
    if not job_id:
        return "dead"

    keys = _keys(queue_name)
    r = await get_redis()
    attempt_raw = await r.hget(keys["attempts"], job_id)
    try:
        attempt = int(attempt_raw or job.get("_attempt") or 1)
    except Exception:
        attempt = 1

    if attempt >= max(1, int(max_attempts or 3)):
        payload_raw = await r.hget(keys["jobs"], job_id)
        dead_payload: Dict[str, Any]
        try:
            dead_payload = json.loads(payload_raw or "{}")
            if not isinstance(dead_payload, dict):
                dead_payload = {"raw": payload_raw}
        except Exception:
            dead_payload = {"raw": payload_raw}
        dead_payload["job_id"] = job_id
        dead_payload["dead_ts"] = time.time()
        dead_payload["attempts"] = attempt
        dead_payload["last_error"] = (error or "")[-2000:]

        pipe = r.pipeline(transaction=True)
        pipe.zrem(keys["processing"], job_id)
        pipe.hdel(keys["jobs"], job_id)
        pipe.hdel(keys["attempts"], job_id)
        pipe.rpush(keys["dead"], _json_dumps(dead_payload))
        await pipe.execute()
        return "dead"

    # Retry later. LPUSH + RPOP keeps FIFO behavior for normal queued jobs.
    pipe = r.pipeline(transaction=True)
    pipe.zrem(keys["processing"], job_id)
    pipe.lpush(keys["ready"], job_id)
    await pipe.execute()
    return "retry"


async def requeue_stale_tg_updates(
    *,
    stale_after_sec: float = 1800,
    max_attempts: int = 3,
    queue_name: Optional[str] = None,
    limit: int = 25,
) -> Dict[str, int]:
    """
    Requeue processing jobs whose heartbeat stopped.

    If the attempt limit is already reached, move the update to dead-letter.
    """
    keys = _keys(queue_name)
    cutoff = time.time() - max(1.0, float(stale_after_sec or 1800))
    batch_limit = max(1, int(limit or 25))
    r = await get_redis()
    job_ids = await r.zrangebyscore(keys["processing"], "-inf", cutoff, start=0, num=batch_limit)
    requeued = 0
    dead = 0

    for job_id in job_ids or []:
        job_id = str(job_id)
        attempt_raw = await r.hget(keys["attempts"], job_id)
        try:
            attempt = int(attempt_raw or 1)
        except Exception:
            attempt = 1

        removed = await r.zrem(keys["processing"], job_id)
        if not removed:
            continue

        if attempt >= max(1, int(max_attempts or 3)):
            payload_raw = await r.hget(keys["jobs"], job_id)
            dead_payload: Dict[str, Any]
            try:
                dead_payload = json.loads(payload_raw or "{}")
                if not isinstance(dead_payload, dict):
                    dead_payload = {"raw": payload_raw}
            except Exception:
                dead_payload = {"raw": payload_raw}
            dead_payload["job_id"] = job_id
            dead_payload["dead_ts"] = time.time()
            dead_payload["attempts"] = attempt
            dead_payload["last_error"] = "processing timeout / worker died"

            pipe = r.pipeline(transaction=True)
            pipe.hdel(keys["jobs"], job_id)
            pipe.hdel(keys["attempts"], job_id)
            pipe.rpush(keys["dead"], _json_dumps(dead_payload))
            await pipe.execute()
            dead += 1
        else:
            await r.lpush(keys["ready"], job_id)
            requeued += 1

    return {"requeued": requeued, "dead": dead}


async def get_tg_update_queue_stats(queue_name: Optional[str] = None) -> Dict[str, Any]:
    keys = _keys(queue_name)
    r = await get_redis()
    ready, processing, dead, jobs = await asyncio.gather(
        r.llen(keys["ready"]),
        r.zcard(keys["processing"]),
        r.llen(keys["dead"]),
        r.hlen(keys["jobs"]),
    )
    return {
        "ok": True,
        "queue_name": _safe_queue_name(queue_name),
        "ready_key": keys["ready"],
        "processing_key": keys["processing"],
        "dead_key": keys["dead"],
        "ready": int(ready or 0),
        "processing": int(processing or 0),
        "dead": int(dead or 0),
        "stored_jobs": int(jobs or 0),
    }
