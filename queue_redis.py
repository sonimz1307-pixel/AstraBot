import asyncio
import json
import os
import time
from typing import Any, Dict, Optional, Sequence

import redis.asyncio as redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

_QUEUE_PREFIX = os.getenv("REDIS_QUEUE_PREFIX", "astrabot:queue").strip().rstrip(":")
_DEFAULT_QUEUE_NAME = os.getenv("REDIS_QUEUE_NAME", "gen").strip() or "gen"

# BLPOP waits up to timeout_sec, so the socket read timeout must be higher
# than the blocking-pop timeout. Otherwise workers can die while simply
# waiting for a job.
_REDIS_SOCKET_TIMEOUT_SEC = int(os.getenv("REDIS_SOCKET_TIMEOUT_SEC", "30") or "30")
_REDIS_CONNECT_TIMEOUT_SEC = int(os.getenv("REDIS_CONNECT_TIMEOUT_SEC", "10") or "10")
_REDIS_HEALTH_CHECK_INTERVAL_SEC = int(os.getenv("REDIS_HEALTH_CHECK_INTERVAL_SEC", "30") or "30")
_REDIS_RECONNECT_SLEEP_SEC = float(os.getenv("REDIS_RECONNECT_SLEEP_SEC", "2") or "2")
_REDIS_ENQUEUE_ATTEMPTS = max(1, int(os.getenv("REDIS_ENQUEUE_ATTEMPTS", "3") or "3"))

_REDIS_CLIENT: Optional[redis.Redis] = None


def _redis_url() -> str:
    url = os.getenv("REDIS_URL")
    if not url:
        raise RuntimeError("REDIS_URL is not set")
    return url


def _queue_key(queue_name: Optional[str] = None) -> str:
    q = (queue_name or _DEFAULT_QUEUE_NAME or "gen").strip() or "gen"
    return f"{_QUEUE_PREFIX}:{q}"


def _delayed_queue_key(queue_name: Optional[str] = None) -> str:
    q = (queue_name or _DEFAULT_QUEUE_NAME or "gen").strip() or "gen"
    return f"{_QUEUE_PREFIX}:delayed:{q}"


async def get_redis() -> "redis.Redis":
    """Return a shared async Redis client for this process."""
    global _REDIS_CLIENT
    if _REDIS_CLIENT is None:
        # decode_responses=True -> str keys/values (we store JSON strings)
        _REDIS_CLIENT = redis.from_url(
            _redis_url(),
            decode_responses=True,
            socket_timeout=_REDIS_SOCKET_TIMEOUT_SEC,
            socket_connect_timeout=_REDIS_CONNECT_TIMEOUT_SEC,
            health_check_interval=_REDIS_HEALTH_CHECK_INTERVAL_SEC,
            retry_on_timeout=True,
        )
    return _REDIS_CLIENT


async def _reset_redis_client() -> None:
    global _REDIS_CLIENT
    client = _REDIS_CLIENT
    _REDIS_CLIENT = None
    if client is not None:
        try:
            await client.aclose()
        except Exception:
            pass


async def enqueue_job(job: Dict[str, Any], queue_name: Optional[str] = None) -> str:
    """
    Push job (dict) into Redis list.
    Returns job_id (string). If missing, generates one.
    """
    job_id = str(job.get("job_id") or job.get("id") or "")
    if not job_id:
        job_id = f"job_{int(time.time() * 1000)}"
        job["job_id"] = job_id

    payload = json.dumps(job, ensure_ascii=False)
    key = _queue_key(queue_name)
    last_exc: Optional[BaseException] = None

    for attempt in range(1, _REDIS_ENQUEUE_ATTEMPTS + 1):
        try:
            r = await get_redis()
            await r.rpush(key, payload)
            return job_id
        except (RedisTimeoutError, RedisConnectionError) as exc:
            last_exc = exc
            print(
                f"[queue_redis] Redis enqueue error attempt={attempt}/{_REDIS_ENQUEUE_ATTEMPTS} "
                f"queue={key}: {exc}",
                flush=True,
            )
            await _reset_redis_client()
            if attempt < _REDIS_ENQUEUE_ATTEMPTS:
                await asyncio.sleep(_REDIS_RECONNECT_SLEEP_SEC)

    raise RuntimeError(f"Redis enqueue failed after {_REDIS_ENQUEUE_ATTEMPTS} attempts: {last_exc}")



async def enqueue_job_delayed(
    job: Dict[str, Any],
    *,
    delay_sec: float = 0,
    queue_name: Optional[str] = None,
    not_before_ts: Optional[float] = None,
) -> str:
    """
    Put job into a Redis sorted set and promote it to the normal list only
    when its due timestamp is reached. This is used for Relax queues where
    the user should see the job as accepted immediately, while the expensive
    provider call starts later without occupying a worker concurrency slot.
    """
    job_id = str(job.get("job_id") or job.get("id") or "")
    if not job_id:
        job_id = f"job_{int(time.time() * 1000)}"
        job["job_id"] = job_id

    now = time.time()
    try:
        due_ts = float(not_before_ts) if not_before_ts is not None else now + max(0.0, float(delay_sec or 0))
    except Exception:
        due_ts = now
    job["not_before_ts"] = due_ts
    job["delayed_queue_name"] = (queue_name or _DEFAULT_QUEUE_NAME or "gen").strip() or "gen"

    payload = json.dumps(job, ensure_ascii=False)
    key = _delayed_queue_key(queue_name)
    last_exc: Optional[BaseException] = None

    for attempt in range(1, _REDIS_ENQUEUE_ATTEMPTS + 1):
        try:
            r = await get_redis()
            await r.zadd(key, {payload: due_ts})
            return job_id
        except (RedisTimeoutError, RedisConnectionError) as exc:
            last_exc = exc
            print(
                f"[queue_redis] Redis delayed enqueue error attempt={attempt}/{_REDIS_ENQUEUE_ATTEMPTS} "
                f"queue={key}: {exc}",
                flush=True,
            )
            await _reset_redis_client()
            if attempt < _REDIS_ENQUEUE_ATTEMPTS:
                await asyncio.sleep(_REDIS_RECONNECT_SLEEP_SEC)

    raise RuntimeError(f"Redis delayed enqueue failed after {_REDIS_ENQUEUE_ATTEMPTS} attempts: {last_exc}")


async def promote_due_delayed_jobs(queue_name: Optional[str] = None, *, limit: int = 50) -> int:
    """
    Atomically move due delayed jobs from Redis ZSET to the normal Redis list.
    The Lua step prevents a lost job if Redis disconnects between ZREM and RPUSH.
    Safe for multiple workers: a payload is moved only if it is removed from ZSET.
    """
    delayed_key = _delayed_queue_key(queue_name)
    ready_key = _queue_key(queue_name)
    now = time.time()
    batch_limit = max(1, int(limit or 50))
    script = """
local delayed_key = KEYS[1]
local ready_key = KEYS[2]
local now_ts = ARGV[1]
local batch_limit = tonumber(ARGV[2]) or 50
local payloads = redis.call('ZRANGEBYSCORE', delayed_key, '-inf', now_ts, 'LIMIT', 0, batch_limit)
local moved = 0
for _, payload in ipairs(payloads) do
    local removed = redis.call('ZREM', delayed_key, payload)
    if removed and removed > 0 then
        redis.call('RPUSH', ready_key, payload)
        moved = moved + 1
    end
end
return moved
"""

    try:
        r = await get_redis()
        moved = int(await r.eval(script, 2, delayed_key, ready_key, now, batch_limit) or 0)
        if moved:
            print(f"[queue_redis] promoted delayed jobs queue={queue_name or _DEFAULT_QUEUE_NAME} count={moved}", flush=True)
        return moved
    except (RedisTimeoutError, RedisConnectionError) as exc:
        print(f"[queue_redis] Redis delayed promote error queue={delayed_key}: {exc}", flush=True)
        await _reset_redis_client()
        await asyncio.sleep(_REDIS_RECONNECT_SLEEP_SEC)
        return 0


async def dequeue_job(
    timeout_sec: int = 10,
    queue_name: Optional[str] = None,
    queue_names: Optional[Sequence[str]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Blocking pop with timeout. Returns dict job or None.
    Supports one queue_name or multiple queue_names.
    Transient Redis timeout/connection errors do not crash workers.
    """
    keys: list[str]
    if queue_names:
        keys = [_queue_key(q) for q in queue_names if str(q or "").strip()]
    else:
        keys = [_queue_key(queue_name)]

    try:
        r = await get_redis()
        res = await r.blpop(keys, timeout=timeout_sec)
    except (RedisTimeoutError, RedisConnectionError) as exc:
        print(f"[queue_redis] Redis dequeue error queues={keys}: {exc}", flush=True)
        await _reset_redis_client()
        await asyncio.sleep(_REDIS_RECONNECT_SLEEP_SEC)
        return None

    if not res:
        return None
    _key, payload = res
    try:
        return json.loads(payload)
    except Exception:
        return {"job_id": "bad_payload", "raw": payload}
