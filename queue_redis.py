import asyncio
import json
import os
import re
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
_LOCK_PREFIX = (os.getenv("REDIS_LOCK_PREFIX", "astrabot:lock") or "astrabot:lock").strip().rstrip(":") or "astrabot:lock"

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


def _generation_lock_key(user_id: int, lock_name: str = "generation") -> str:
    try:
        uid = int(user_id)
    except Exception as exc:
        raise ValueError("invalid user_id for Redis generation lock") from exc
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(lock_name or "generation").strip()) or "generation"
    return f"{_LOCK_PREFIX}:{safe_name}:{uid}"


async def acquire_generation_lock(
    user_id: int,
    owner_id: str,
    *,
    lock_name: str = "generation",
    ttl_sec: int = 14_400,
) -> bool:
    """Atomically acquire a cross-process per-user generation lock.

    Returns True only for the caller that created the lock. The opaque owner_id
    is stored in Redis and is later used for safe refresh/release operations.
    """
    owner = str(owner_id or "").strip()
    if not owner:
        raise ValueError("owner_id is required for Redis generation lock")
    key = _generation_lock_key(user_id, lock_name)
    ttl = max(60, int(ttl_sec or 14_400))
    last_exc: Optional[BaseException] = None
    for attempt in range(1, _REDIS_ENQUEUE_ATTEMPTS + 1):
        try:
            r = await get_redis()
            return bool(await r.set(key, owner, ex=ttl, nx=True))
        except (RedisTimeoutError, RedisConnectionError) as exc:
            last_exc = exc
            print(
                f"[queue_redis] Redis lock acquire error attempt={attempt}/{_REDIS_ENQUEUE_ATTEMPTS} "
                f"key={key}: {exc}",
                flush=True,
            )
            await _reset_redis_client()
            if attempt < _REDIS_ENQUEUE_ATTEMPTS:
                await asyncio.sleep(_REDIS_RECONNECT_SLEEP_SEC)
    raise RuntimeError(f"Redis lock acquire failed after {_REDIS_ENQUEUE_ATTEMPTS} attempts: {last_exc}")


async def refresh_generation_lock(
    user_id: int,
    owner_id: str,
    *,
    lock_name: str = "generation",
    ttl_sec: int = 14_400,
) -> bool:
    """Refresh the lock TTL only when owner_id still owns the lock."""
    owner = str(owner_id or "").strip()
    if not owner:
        return False
    key = _generation_lock_key(user_id, lock_name)
    ttl = max(60, int(ttl_sec or 14_400))
    script = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
    return 1
end
return 0
"""
    try:
        r = await get_redis()
        return bool(int(await r.eval(script, 1, key, owner, ttl) or 0))
    except (RedisTimeoutError, RedisConnectionError) as exc:
        print(f"[queue_redis] Redis lock refresh error key={key}: {exc}", flush=True)
        await _reset_redis_client()
        return False


async def release_generation_lock(
    user_id: int,
    owner_id: str,
    *,
    lock_name: str = "generation",
) -> bool:
    """Delete the lock only when owner_id still owns it."""
    owner = str(owner_id or "").strip()
    if not owner:
        return False
    key = _generation_lock_key(user_id, lock_name)
    script = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""
    last_exc: Optional[BaseException] = None
    for attempt in range(1, _REDIS_ENQUEUE_ATTEMPTS + 1):
        try:
            r = await get_redis()
            return bool(int(await r.eval(script, 1, key, owner) or 0))
        except (RedisTimeoutError, RedisConnectionError) as exc:
            last_exc = exc
            print(
                f"[queue_redis] Redis lock release error attempt={attempt}/{_REDIS_ENQUEUE_ATTEMPTS} "
                f"key={key}: {exc}",
                flush=True,
            )
            await _reset_redis_client()
            if attempt < _REDIS_ENQUEUE_ATTEMPTS:
                await asyncio.sleep(_REDIS_RECONNECT_SLEEP_SEC)
    raise RuntimeError(f"Redis lock release failed after {_REDIS_ENQUEUE_ATTEMPTS} attempts: {last_exc}")


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

# ---------------------------------------------------------------------------
# Reliable Redis Stream queue helpers.
# Used by Seedance 2.5 only; legacy list queues keep their existing behavior.
# ---------------------------------------------------------------------------
_RELIABLE_STREAM_PREFIX = (os.getenv("REDIS_RELIABLE_STREAM_PREFIX", "astrabot:stream") or "astrabot:stream").strip().rstrip(":") or "astrabot:stream"
_RELIABLE_GROUP_PREFIX = (os.getenv("REDIS_RELIABLE_GROUP_PREFIX", "astrabot:group") or "astrabot:group").strip().rstrip(":") or "astrabot:group"
_RELIABLE_STATE_PREFIX = (os.getenv("REDIS_RELIABLE_STATE_PREFIX", "astrabot:jobstate") or "astrabot:jobstate").strip().rstrip(":") or "astrabot:jobstate"


def _reliable_stream_key(queue_name: str) -> str:
    q = (queue_name or _DEFAULT_QUEUE_NAME or "gen").strip() or "gen"
    return f"{_RELIABLE_STREAM_PREFIX}:{q}"


def _reliable_group_name(queue_name: str) -> str:
    q = (queue_name or _DEFAULT_QUEUE_NAME or "gen").strip() or "gen"
    return f"{_RELIABLE_GROUP_PREFIX}:{q}"


def _reliable_dedupe_key(queue_name: str, job_id: str) -> str:
    q = (queue_name or _DEFAULT_QUEUE_NAME or "gen").strip() or "gen"
    jid = str(job_id or "").strip()
    if not jid:
        raise ValueError("job_id is required")
    return f"{_RELIABLE_STATE_PREFIX}:dedupe:{q}:{jid}"


def _reliable_state_key(queue_name: str, job_id: str) -> str:
    q = (queue_name or _DEFAULT_QUEUE_NAME or "gen").strip() or "gen"
    jid = str(job_id or "").strip()
    if not jid:
        raise ValueError("job_id is required")
    return f"{_RELIABLE_STATE_PREFIX}:{q}:{jid}"


async def _ensure_reliable_group(queue_name: str) -> tuple[str, str]:
    stream = _reliable_stream_key(queue_name)
    group = _reliable_group_name(queue_name)
    r = await get_redis()
    try:
        await r.xgroup_create(stream, group, id="0-0", mkstream=True)
    except Exception as exc:
        # BUSYGROUP is the normal steady-state response once the group exists.
        if "BUSYGROUP" not in str(exc).upper():
            raise
    return stream, group


async def enqueue_reliable_job(
    job: Dict[str, Any], queue_name: str, *, dedupe_ttl_sec: int = 259200
) -> str:
    """Enqueue a job in a Redis Stream for at-least-once delivery with ACK.

    ``dedupe_ttl_sec=0`` keeps the dedupe key persistent. Wan 3.0 uses that
    mode so XADD and its durable proof-of-enqueue are committed atomically in
    the same Redis Lua transaction; the Wan worker restores a bounded TTL after
    provider taskId persistence/terminal settlement. Existing callers keep the
    historical 3-day TTL by default.
    """
    job_id = str(job.get("job_id") or job.get("id") or "").strip()
    if not job_id:
        job_id = f"job_{int(time.time() * 1000)}"
        job["job_id"] = job_id
    payload = json.dumps(job, ensure_ascii=False)
    stream, _group = await _ensure_reliable_group(queue_name)
    last_exc: Optional[BaseException] = None
    for attempt in range(1, _REDIS_ENQUEUE_ATTEMPTS + 1):
        try:
            r = await get_redis()
            dedupe_key = _reliable_dedupe_key(queue_name, job_id)
            script = """
if redis.call('EXISTS', KEYS[2]) == 1 then
    return 0
end
redis.call('XADD', KEYS[1], '*', 'payload', ARGV[1], 'job_id', ARGV[2])
local ttl = tonumber(ARGV[3]) or 0
if ttl > 0 then
    redis.call('SET', KEYS[2], '1', 'EX', ttl)
else
    redis.call('SET', KEYS[2], '1')
end
return 1
"""
            await r.eval(script, 2, stream, dedupe_key, payload, job_id, max(0, int(dedupe_ttl_sec or 0)))
            return job_id
        except (RedisTimeoutError, RedisConnectionError) as exc:
            last_exc = exc
            await _reset_redis_client()
            if attempt < _REDIS_ENQUEUE_ATTEMPTS:
                await asyncio.sleep(_REDIS_RECONNECT_SLEEP_SEC)
    raise RuntimeError(f"Redis reliable enqueue failed after {_REDIS_ENQUEUE_ATTEMPTS} attempts: {last_exc}")


async def _promote_one_legacy_job_to_reliable(queue_name: str) -> bool:
    """Atomically move one pre-v3 list job into the reliable stream."""
    legacy = _queue_key(queue_name)
    stream, _group = await _ensure_reliable_group(queue_name)
    script = """
local payload = redis.call('LPOP', KEYS[1])
if not payload then return 0 end
redis.call('XADD', KEYS[2], '*', 'payload', payload, 'job_id', '')
return 1
"""
    try:
        r = await get_redis()
        return bool(int(await r.eval(script, 2, legacy, stream) or 0))
    except Exception:
        return False


async def _claim_stale_reliable_job(
    *, queue_name: str, consumer_name: str, stale_after_sec: int
) -> Optional[Dict[str, Any]]:
    stream, group = await _ensure_reliable_group(queue_name)
    r = await get_redis()
    stale_ms = max(30_000, int(stale_after_sec) * 1000)
    try:
        pending = await r.xpending_range(stream, group, min="-", max="+", count=10, idle=stale_ms)
    except TypeError:
        pending = await r.xpending_range(stream, group, min="-", max="+", count=10)
    except Exception:
        return None

    for item in pending or []:
        message_id = None
        idle_ms = 0
        if isinstance(item, dict):
            message_id = item.get("message_id") or item.get("messageId")
            idle_ms = int(item.get("time_since_delivered") or item.get("idle") or 0)
        elif isinstance(item, (list, tuple)) and item:
            message_id = item[0]
            if len(item) > 2:
                try:
                    idle_ms = int(item[2] or 0)
                except Exception:
                    idle_ms = 0
        if not message_id or idle_ms < stale_ms:
            continue
        try:
            claimed = await r.xclaim(
                stream,
                group,
                consumer_name,
                min_idle_time=stale_ms,
                message_ids=[message_id],
            )
        except Exception:
            continue
        if not claimed:
            continue
        claimed_id, fields = claimed[0]
        payload = (fields or {}).get("payload") if isinstance(fields, dict) else None
        if not payload:
            try:
                await r.xack(stream, group, claimed_id)
                await r.xdel(stream, claimed_id)
            except Exception:
                pass
            continue
        try:
            job = json.loads(payload)
        except Exception:
            job = {"job_id": "bad_payload", "raw": payload}
        job["_reliable_stream_id"] = str(claimed_id)
        job["_reliable_consumer"] = str(consumer_name)
        return job
    return None


async def dequeue_reliable_job(
    *,
    queue_name: str,
    consumer_name: str,
    timeout_sec: int = 10,
    stale_after_sec: int = 300,
) -> Optional[Dict[str, Any]]:
    """Read one new job or reclaim one stale pending job from a Redis Stream."""
    try:
        await _promote_one_legacy_job_to_reliable(queue_name)
        stale = await _claim_stale_reliable_job(
            queue_name=queue_name,
            consumer_name=consumer_name,
            stale_after_sec=stale_after_sec,
        )
        if stale:
            return stale
        stream, group = await _ensure_reliable_group(queue_name)
        r = await get_redis()
        rows = await r.xreadgroup(
            group,
            consumer_name,
            streams={stream: ">"},
            count=1,
            block=max(1, int(timeout_sec)) * 1000,
        )
    except (RedisTimeoutError, RedisConnectionError) as exc:
        print(f"[queue_redis] reliable dequeue error queue={queue_name}: {exc}", flush=True)
        await _reset_redis_client()
        await asyncio.sleep(_REDIS_RECONNECT_SLEEP_SEC)
        return None

    if not rows:
        return None
    _stream_name, messages = rows[0]
    if not messages:
        return None
    message_id, fields = messages[0]
    payload = (fields or {}).get("payload") if isinstance(fields, dict) else None
    if not payload:
        try:
            await r.xack(stream, group, message_id)
            await r.xdel(stream, message_id)
        except Exception:
            pass
        return None
    try:
        job = json.loads(payload)
    except Exception:
        job = {"job_id": "bad_payload", "raw": payload}
    job["_reliable_stream_id"] = str(message_id)
    job["_reliable_consumer"] = str(consumer_name)
    return job


async def touch_reliable_job(*, queue_name: str, job: Dict[str, Any]) -> Optional[bool]:
    """Refresh a pending entry only while this consumer still owns it.

    Returns True when refreshed, False when ownership was lost / entry vanished,
    and None for a transient Redis error. The ownership check + XCLAIM are done
    atomically in Lua so an old worker cannot steal a job back after recovery.
    """
    message_id = str(job.get("_reliable_stream_id") or "").strip()
    consumer = str(job.get("_reliable_consumer") or "").strip()
    if not message_id or not consumer:
        return False
    stream, group = await _ensure_reliable_group(queue_name)
    script = r"""
local pending = redis.call('XPENDING', KEYS[1], ARGV[1], ARGV[2], ARGV[2], 1)
if (not pending) or (#pending == 0) then
    return 0
end
if tostring(pending[1][2]) ~= tostring(ARGV[3]) then
    return -1
end
local claimed = redis.call('XCLAIM', KEYS[1], ARGV[1], ARGV[3], 0, ARGV[2], 'JUSTID')
if claimed and #claimed > 0 then
    return 1
end
return 0
"""
    try:
        r = await get_redis()
        result = int(await r.eval(script, 1, stream, group, message_id, consumer) or 0)
        return result == 1
    except (RedisTimeoutError, RedisConnectionError) as exc:
        print(f"[queue_redis] reliable touch transient error queue={queue_name} id={message_id}: {exc}", flush=True)
        await _reset_redis_client()
        return None
    except Exception as exc:
        print(f"[queue_redis] reliable touch failed queue={queue_name} id={message_id}: {exc}", flush=True)
        return None


async def ack_reliable_job(*, queue_name: str, job: Dict[str, Any]) -> bool:
    message_id = str(job.get("_reliable_stream_id") or "").strip()
    if not message_id:
        return False
    stream, group = await _ensure_reliable_group(queue_name)
    r = await get_redis()
    acked = int(await r.xack(stream, group, message_id) or 0)
    try:
        await r.xdel(stream, message_id)
    except Exception:
        pass
    return bool(acked)


async def requeue_reliable_job(*, queue_name: str, job: Dict[str, Any]) -> bool:
    """Atomically copy an active pending job back as a new stream entry and ACK the old one."""
    message_id = str(job.get("_reliable_stream_id") or "").strip()
    if not message_id:
        return False
    clean_job = {k: v for k, v in job.items() if not str(k).startswith("_reliable_")}
    payload = json.dumps(clean_job, ensure_ascii=False)
    job_id = str(clean_job.get("job_id") or clean_job.get("id") or "").strip()
    stream, group = await _ensure_reliable_group(queue_name)
    script = """
redis.call('XADD', KEYS[1], '*', 'payload', ARGV[3], 'job_id', ARGV[4])
redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
redis.call('XDEL', KEYS[1], ARGV[2])
return 1
"""
    r = await get_redis()
    return bool(int(await r.eval(script, 1, stream, group, message_id, payload, job_id) or 0))


async def get_reliable_job_state(*, queue_name: str, job_id: str) -> Dict[str, Any]:
    key = _reliable_state_key(queue_name, job_id)
    try:
        r = await get_redis()
        raw = await r.get(key)
        if not raw:
            return {}
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


async def set_reliable_job_state(
    *, queue_name: str, job_id: str, updates: Dict[str, Any], ttl_sec: int = 259200
) -> Dict[str, Any]:
    key = _reliable_state_key(queue_name, job_id)
    current = await get_reliable_job_state(queue_name=queue_name, job_id=job_id)
    current.update(dict(updates or {}))
    current["updated_at"] = time.time()
    r = await get_redis()
    await r.set(key, json.dumps(current, ensure_ascii=False), ex=max(3600, int(ttl_sec)))
    return current


async def delete_reliable_job_state(*, queue_name: str, job_id: str) -> None:
    try:
        r = await get_redis()
        await r.delete(_reliable_state_key(queue_name, job_id))
    except Exception:
        pass
