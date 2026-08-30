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
_SEEDANCE25_DRAFT_PREFIX = (
    os.getenv("SEEDANCE25_DRAFT_PREFIX", f"{_QUEUE_PREFIX}:seedance25:draft")
    or f"{_QUEUE_PREFIX}:seedance25:draft"
).strip().rstrip(":")
try:
    _SEEDANCE25_DRAFT_TTL_SEC = max(
        3_600,
        min(7 * 24 * 3_600, int(os.getenv("SEEDANCE25_DRAFT_TTL_SECONDS", "86400") or "86400")),
    )
except Exception:
    _SEEDANCE25_DRAFT_TTL_SEC = 86_400

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


def _seedance25_draft_key(chat_id: int, user_id: int) -> str:
    """Stable cross-process key for one Telegram Seedance 2.5 draft."""
    try:
        cid = int(chat_id)
        uid = int(user_id)
    except Exception as exc:
        raise ValueError("invalid chat_id/user_id for Seedance 2.5 draft") from exc
    if not cid or not uid:
        raise ValueError("chat_id and user_id are required for Seedance 2.5 draft")
    return f"{_SEEDANCE25_DRAFT_PREFIX}:{cid}:{uid}"


async def save_seedance25_draft(
    chat_id: int,
    user_id: int,
    draft: Dict[str, Any],
    *,
    ttl_sec: Optional[int] = None,
) -> bool:
    """Persist the pre-enqueue Telegram draft so another process can resume it.

    The operation is deliberately awaited by the Telegram update handler before
    that update is acknowledged.  A successful bot reply must never be the only
    copy of the user's settings, references, prompt, or confirmation nonce.
    """
    if not isinstance(draft, dict):
        raise ValueError("Seedance 2.5 draft must be a dict")
    key = _seedance25_draft_key(chat_id, user_id)
    ttl = max(3_600, min(7 * 24 * 3_600, int(ttl_sec or _SEEDANCE25_DRAFT_TTL_SEC)))
    payload = dict(draft)
    payload["persisted_at"] = time.time()
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    last_exc: Optional[BaseException] = None
    for attempt in range(1, _REDIS_ENQUEUE_ATTEMPTS + 1):
        try:
            r = await get_redis()
            await r.set(key, encoded, ex=ttl)
            return True
        except (RedisTimeoutError, RedisConnectionError) as exc:
            last_exc = exc
            print(
                f"[queue_redis] Seedance 2.5 draft save error "
                f"attempt={attempt}/{_REDIS_ENQUEUE_ATTEMPTS} key={key}: {exc}",
                flush=True,
            )
            await _reset_redis_client()
            if attempt < _REDIS_ENQUEUE_ATTEMPTS:
                await asyncio.sleep(_REDIS_RECONNECT_SLEEP_SEC)
    raise RuntimeError(
        f"Seedance 2.5 draft save failed after {_REDIS_ENQUEUE_ATTEMPTS} attempts: {last_exc}"
    )


async def load_seedance25_draft(chat_id: int, user_id: int) -> Optional[Dict[str, Any]]:
    """Load a shared Telegram draft, returning None only when it does not exist."""
    key = _seedance25_draft_key(chat_id, user_id)
    last_exc: Optional[BaseException] = None
    for attempt in range(1, _REDIS_ENQUEUE_ATTEMPTS + 1):
        try:
            r = await get_redis()
            raw = await r.get(key)
            if raw is None:
                return None
            decoded = json.loads(raw)
            if not isinstance(decoded, dict):
                raise RuntimeError("Seedance 2.5 draft payload is not an object")
            return decoded
        except (RedisTimeoutError, RedisConnectionError) as exc:
            last_exc = exc
            print(
                f"[queue_redis] Seedance 2.5 draft load error "
                f"attempt={attempt}/{_REDIS_ENQUEUE_ATTEMPTS} key={key}: {exc}",
                flush=True,
            )
            await _reset_redis_client()
            if attempt < _REDIS_ENQUEUE_ATTEMPTS:
                await asyncio.sleep(_REDIS_RECONNECT_SLEEP_SEC)
        except (TypeError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
            raise RuntimeError(f"Seedance 2.5 draft is corrupted for key={key}: {exc}") from exc
    raise RuntimeError(
        f"Seedance 2.5 draft load failed after {_REDIS_ENQUEUE_ATTEMPTS} attempts: {last_exc}"
    )


async def clear_seedance25_draft(chat_id: int, user_id: int) -> bool:
    """Delete a shared draft after cancel/reset or a confirmed Redis enqueue."""
    key = _seedance25_draft_key(chat_id, user_id)
    last_exc: Optional[BaseException] = None
    for attempt in range(1, _REDIS_ENQUEUE_ATTEMPTS + 1):
        try:
            r = await get_redis()
            await r.delete(key)
            return True
        except (RedisTimeoutError, RedisConnectionError) as exc:
            last_exc = exc
            print(
                f"[queue_redis] Seedance 2.5 draft clear error "
                f"attempt={attempt}/{_REDIS_ENQUEUE_ATTEMPTS} key={key}: {exc}",
                flush=True,
            )
            await _reset_redis_client()
            if attempt < _REDIS_ENQUEUE_ATTEMPTS:
                await asyncio.sleep(_REDIS_RECONNECT_SLEEP_SEC)
    raise RuntimeError(
        f"Seedance 2.5 draft clear failed after {_REDIS_ENQUEUE_ATTEMPTS} attempts: {last_exc}"
    )


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


def _reliable_submission_key(queue_name: str, job_id: str) -> str:
    q = (queue_name or _DEFAULT_QUEUE_NAME or "gen").strip() or "gen"
    jid = str(job_id or "").strip()
    if not jid:
        raise ValueError("job_id is required")
    return f"{_RELIABLE_STATE_PREFIX}:submission:{q}:{jid}"


def _reliable_user_settlement_key(queue_name: str, user_id: int) -> str:
    q = (queue_name or _DEFAULT_QUEUE_NAME or "gen").strip() or "gen"
    uid = int(user_id or 0)
    if uid <= 0:
        raise ValueError("user_id is required")
    return f"{_RELIABLE_STATE_PREFIX}:settlement:{q}:{uid}"


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


async def reliable_job_was_enqueued(*, queue_name: str, job_id: str) -> Optional[bool]:
    """Return Redis' durable proof for an uncertain reliable enqueue.

    ``True`` means the Lua transaction created the dedupe key together with
    XADD. ``False`` means Redis answered and the job was not committed. ``None``
    means Redis itself is unavailable, so the caller must not guess whether it
    is safe to refund a charge.
    """
    if not str(job_id or "").strip():
        return False
    dedupe_key = _reliable_dedupe_key(queue_name, job_id)
    for attempt in range(1, _REDIS_ENQUEUE_ATTEMPTS + 1):
        try:
            r = await get_redis()
            return bool(await r.exists(dedupe_key))
        except (RedisTimeoutError, RedisConnectionError) as exc:
            print(
                f"[queue_redis] Redis enqueue proof error attempt={attempt}/{_REDIS_ENQUEUE_ATTEMPTS} "
                f"key={dedupe_key}: {exc}",
                flush=True,
            )
            await _reset_redis_client()
            if attempt < _REDIS_ENQUEUE_ATTEMPTS:
                await asyncio.sleep(_REDIS_RECONNECT_SLEEP_SEC)
        except Exception as exc:
            print(f"[queue_redis] Redis enqueue proof unavailable key={dedupe_key}: {exc}", flush=True)
            return None
    return None


async def claim_reliable_submission(
    *,
    queue_name: str,
    job_id: str,
    submission_nonce: str,
    owner_token: str,
    ttl_sec: int = 259200,
    stale_after_sec: int = 600,
) -> Dict[str, Any]:
    """Atomically claim one user submission before any token charge.

    A Telegram callback can be retried by the existing update worker.  The
    caller therefore derives ``job_id`` from a draft nonce and uses this guard
    to ensure that only the first handling may cross the billing boundary.
    ``ready`` is reusable immediately. A stale ``claimed``/``charged`` lease
    may be reclaimed with the same nonce and deterministic billing reference;
    terminal or unresolved states remain fail-closed.
    """
    nonce = str(submission_nonce or "").strip()
    owner = str(owner_token or "").strip()
    if not nonce:
        raise ValueError("submission_nonce is required")
    if not owner:
        raise ValueError("owner_token is required")
    key = _reliable_submission_key(queue_name, job_id)
    now_ts = time.time()
    stale_before_ts = now_ts - max(60, int(stale_after_sec or 600))
    script = """
local current_status = redis.call('HGET', KEYS[1], 'status')
local current_nonce = redis.call('HGET', KEYS[1], 'nonce')
local current_updated_at = tonumber(redis.call('HGET', KEYS[1], 'updated_at') or '0')
if current_nonce and current_nonce ~= ARGV[1] then
    return {'conflict', '0', tostring(current_updated_at)}
end
if (not current_status) or current_status == '' or current_status == 'ready' then
    redis.call('HSET', KEYS[1],
        'nonce', ARGV[1],
        'status', 'claimed',
        'owner_token', ARGV[2],
        'updated_at', ARGV[3])
    redis.call('EXPIRE', KEYS[1], ARGV[4])
    return {'claimed', '1', ARGV[3], ARGV[2]}
end
if (current_status == 'claimed' or current_status == 'charged')
        and current_updated_at <= tonumber(ARGV[5]) then
    redis.call('HSET', KEYS[1],
        'owner_token', ARGV[2],
        'updated_at', ARGV[3])
    redis.call('EXPIRE', KEYS[1], ARGV[4])
    return {current_status, '1', ARGV[3], ARGV[2]}
end
return {current_status, '0', tostring(current_updated_at), redis.call('HGET', KEYS[1], 'owner_token') or ''}
"""
    r = await get_redis()
    result = await r.eval(
        script,
        1,
        key,
        nonce,
        owner,
        now_ts,
        max(3600, int(ttl_sec or 259200)),
        stale_before_ts,
    )
    status = str((result or ["unavailable"])[0] or "unavailable")
    claimed = str((result or ["", "0"])[1] or "0") == "1"
    try:
        updated_at = float((result or ["", "", "0"])[2] or 0.0)
    except Exception:
        updated_at = 0.0
    current_owner = str((result or ["", "", "", ""])[3] or "")
    return {
        "status": status,
        "claimed": claimed,
        "updated_at": updated_at,
        "owner_token": current_owner,
    }


async def get_reliable_submission_status(*, queue_name: str, job_id: str) -> Dict[str, Any]:
    """Read the durable submission state used by V5 reconciliation."""
    key = _reliable_submission_key(queue_name, job_id)
    r = await get_redis()
    raw = await r.hgetall(key)
    return dict(raw or {})


async def refresh_reliable_submission_claim(
    *,
    queue_name: str,
    job_id: str,
    user_id: int,
    submission_nonce: str,
    owner_token: str,
    ttl_sec: int = 259200,
) -> bool:
    """Refresh an active charge lease only while the caller still owns it."""
    nonce = str(submission_nonce or "").strip()
    owner = str(owner_token or "").strip()
    if not nonce or not owner:
        return False
    submission_key = _reliable_submission_key(queue_name, job_id)
    settlement_key = _reliable_user_settlement_key(queue_name, user_id)
    now_ts = time.time()
    script = """
if redis.call('HGET', KEYS[1], 'nonce') ~= ARGV[1]
        or redis.call('HGET', KEYS[1], 'owner_token') ~= ARGV[2] then
    return 0
end
local current_status = redis.call('HGET', KEYS[1], 'status') or ''
if current_status ~= 'claimed' and current_status ~= 'charged' then
    return 0
end
if redis.call('HGET', KEYS[2], 'job_id') ~= ARGV[3] then
    return 0
end
redis.call('HSET', KEYS[1], 'updated_at', ARGV[4])
redis.call('EXPIRE', KEYS[1], ARGV[5])
redis.call('HSET', KEYS[2], 'updated_at', ARGV[4])
redis.call('EXPIRE', KEYS[2], ARGV[5])
return 1
"""
    r = await get_redis()
    return bool(int(await r.eval(
        script,
        2,
        submission_key,
        settlement_key,
        nonce,
        owner,
        str(job_id),
        now_ts,
        max(3600, int(ttl_sec or 259200)),
    ) or 0))


async def set_reliable_submission_status(
    *,
    queue_name: str,
    job_id: str,
    submission_nonce: str,
    status: str,
    owner_token: str = "",
    force_enqueued: bool = False,
    ttl_sec: int = 259200,
) -> bool:
    """Persist a financial/queue settlement state for one submission."""
    nonce = str(submission_nonce or "").strip()
    owner = str(owner_token or "").strip()
    next_status = str(status or "").strip().lower()
    if not nonce:
        raise ValueError("submission_nonce is required")
    if next_status not in {
        "ready",
        "claimed",
        "charged",
        "enqueued",
        "refunded",
        "refund_pending",
        "review_required",
        "reconciling",
        "not_charged",
    }:
        raise ValueError(f"unsupported reliable submission status: {next_status}")
    key = _reliable_submission_key(queue_name, job_id)
    script = """
local current_nonce = redis.call('HGET', KEYS[1], 'nonce')
if current_nonce and current_nonce ~= ARGV[1] then
    return 0
end
local current_status = redis.call('HGET', KEYS[1], 'status') or ''
local next_status = ARGV[2]
local owner_token = ARGV[5]
local force_enqueued = ARGV[6] == '1'
if current_status == 'enqueued' or current_status == 'refunded' or current_status == 'not_charged' then
    if current_status == next_status then
        return 1
    end
    return 0
end
if force_enqueued then
    if next_status ~= 'enqueued' then
        return 0
    end
elseif owner_token == '' or redis.call('HGET', KEYS[1], 'owner_token') ~= owner_token then
    return 0
end
local allowed = false
if current_status == 'claimed' then
    allowed = next_status == 'ready' or next_status == 'charged'
        or next_status == 'refunded' or next_status == 'refund_pending'
        or next_status == 'review_required' or next_status == 'not_charged'
elseif current_status == 'charged' then
    allowed = next_status == 'enqueued' or next_status == 'refunded'
        or next_status == 'refund_pending' or next_status == 'review_required'
elseif current_status == 'reconciling' then
    allowed = next_status == 'enqueued' or next_status == 'refunded'
        or next_status == 'refund_pending' or next_status == 'review_required'
        or next_status == 'not_charged'
elseif current_status == 'refund_pending' or current_status == 'review_required' then
    allowed = next_status == 'reconciling'
else
    allowed = current_status == next_status
end
if not allowed then
    return 0
end
redis.call('HSET', KEYS[1],
    'nonce', ARGV[1],
    'status', ARGV[2],
    'updated_at', ARGV[3])
if next_status == 'enqueued' or next_status == 'refunded' or next_status == 'not_charged' then
    redis.call('HDEL', KEYS[1], 'owner_token')
end
redis.call('EXPIRE', KEYS[1], ARGV[4])
return 1
"""
    r = await get_redis()
    changed = int(await r.eval(
        script,
        1,
        key,
        nonce,
        next_status,
        time.time(),
        max(3600, int(ttl_sec or 259200)),
        owner,
        "1" if force_enqueued else "0",
    ) or 0)
    return bool(changed)


async def enqueue_reliable_submission_job(
    job: Dict[str, Any],
    queue_name: str,
    *,
    submission_nonce: str,
    owner_token: str,
    dedupe_ttl_sec: int = 259200,
) -> Dict[str, Any]:
    """Atomically fence the owner, XADD the job and mark it enqueued."""
    job_id = str(job.get("job_id") or job.get("id") or "").strip()
    nonce = str(submission_nonce or "").strip()
    owner = str(owner_token or "").strip()
    if not job_id or not nonce or not owner:
        raise ValueError("job_id, submission_nonce and owner_token are required")
    payload = json.dumps(job, ensure_ascii=False)
    stream, _group = await _ensure_reliable_group(queue_name)
    dedupe_key = _reliable_dedupe_key(queue_name, job_id)
    submission_key = _reliable_submission_key(queue_name, job_id)
    script = """
if redis.call('EXISTS', KEYS[2]) == 1 then
    if redis.call('HGET', KEYS[3], 'nonce') ~= ARGV[2] then
        return {'conflict', '0'}
    end
    redis.call('HSET', KEYS[3], 'status', 'enqueued', 'updated_at', ARGV[5])
    redis.call('HDEL', KEYS[3], 'owner_token')
    redis.call('EXPIRE', KEYS[3], ARGV[4])
    return {'enqueued', '0'}
end
if redis.call('HGET', KEYS[3], 'nonce') ~= ARGV[2]
        or redis.call('HGET', KEYS[3], 'owner_token') ~= ARGV[3]
        or redis.call('HGET', KEYS[3], 'status') ~= 'charged' then
    return {'owner_lost', '0'}
end
redis.call('XADD', KEYS[1], '*', 'payload', ARGV[1], 'job_id', ARGV[6])
local dedupe_ttl = tonumber(ARGV[7]) or 0
if dedupe_ttl > 0 then
    redis.call('SET', KEYS[2], '1', 'EX', dedupe_ttl)
else
    redis.call('SET', KEYS[2], '1')
end
redis.call('HSET', KEYS[3], 'status', 'enqueued', 'updated_at', ARGV[5])
redis.call('HDEL', KEYS[3], 'owner_token')
redis.call('EXPIRE', KEYS[3], ARGV[4])
return {'enqueued', '1'}
"""
    last_exc: Optional[BaseException] = None
    for attempt in range(1, _REDIS_ENQUEUE_ATTEMPTS + 1):
        try:
            r = await get_redis()
            result = await r.eval(
                script,
                3,
                stream,
                dedupe_key,
                submission_key,
                payload,
                nonce,
                owner,
                max(3600, int(dedupe_ttl_sec or 259200)),
                time.time(),
                job_id,
                max(0, int(dedupe_ttl_sec or 0)),
            )
            status = str((result or ["unavailable"])[0] or "unavailable")
            inserted = str((result or ["", "0"])[1] or "0") == "1"
            return {"status": status, "enqueued": status == "enqueued", "inserted": inserted}
        except (RedisTimeoutError, RedisConnectionError) as exc:
            last_exc = exc
            await _reset_redis_client()
            if attempt < _REDIS_ENQUEUE_ATTEMPTS:
                await asyncio.sleep(_REDIS_RECONNECT_SLEEP_SEC)
    raise RuntimeError(
        f"Redis fenced reliable enqueue failed after {_REDIS_ENQUEUE_ATTEMPTS} attempts: {last_exc}"
    )


async def claim_reliable_user_settlement_reconciliation(
    *,
    queue_name: str,
    user_id: int,
    job_id: str,
    reconciliation_token: str,
    ttl_sec: int = 259200,
    stale_after_sec: int = 600,
) -> Dict[str, Any]:
    """Take a stale/unresolved pre-enqueue settlement for safe reconciliation."""
    token = str(reconciliation_token or "").strip()
    jid = str(job_id or "").strip()
    if not token or not jid:
        raise ValueError("job_id and reconciliation_token are required")
    settlement_key = _reliable_user_settlement_key(queue_name, user_id)
    submission_key = _reliable_submission_key(queue_name, jid)
    now_ts = time.time()
    stale_before = now_ts - max(60, int(stale_after_sec or 600))
    script = """
if redis.call('HGET', KEYS[1], 'job_id') ~= ARGV[1] then
    return {'settlement_changed', '0', '', ''}
end
local block_status = redis.call('HGET', KEYS[1], 'status') or ''
local block_updated = tonumber(redis.call('HGET', KEYS[1], 'updated_at') or '0')
local submission_status = redis.call('HGET', KEYS[2], 'status') or ''
local nonce = redis.call('HGET', KEYS[2], 'nonce') or ''
if nonce == '' then
    return {'missing_submission', '0', '', submission_status}
end
if submission_status == 'enqueued' or submission_status == 'refunded'
        or submission_status == 'not_charged' then
    return {submission_status, '0', nonce, submission_status}
end
if block_status == 'in_progress' and block_updated > tonumber(ARGV[4]) then
    return {'in_progress', '0', nonce, submission_status}
end
if block_status == 'reconciling' and block_updated > tonumber(ARGV[4]) then
    return {'reconciling', '0', nonce, submission_status}
end
redis.call('HSET', KEYS[1],
    'status', 'reconciling',
    'owner_token', ARGV[2],
    'updated_at', ARGV[3])
redis.call('EXPIRE', KEYS[1], ARGV[5])
redis.call('HSET', KEYS[2],
    'status', 'reconciling',
    'owner_token', ARGV[2],
    'updated_at', ARGV[3])
redis.call('EXPIRE', KEYS[2], ARGV[5])
return {'reconciling', '1', nonce, submission_status}
"""
    r = await get_redis()
    result = await r.eval(
        script,
        2,
        settlement_key,
        submission_key,
        jid,
        token,
        now_ts,
        stale_before,
        max(3600, int(ttl_sec or 259200)),
    )
    return {
        "status": str((result or ["unavailable"])[0] or "unavailable"),
        "claimed": str((result or ["", "0"])[1] or "0") == "1",
        "submission_nonce": str((result or ["", "", ""])[2] or ""),
        "previous_status": str((result or ["", "", "", ""])[3] or ""),
        "owner_token": token,
    }


async def get_reliable_user_settlement_block(
    *,
    queue_name: str,
    user_id: int,
) -> Dict[str, Any]:
    """Return a durable per-user block for an unresolved paid submission."""
    key = _reliable_user_settlement_key(queue_name, user_id)
    r = await get_redis()
    raw = await r.hgetall(key)
    return dict(raw or {})


async def set_reliable_user_settlement_block(
    *,
    queue_name: str,
    user_id: int,
    job_id: str,
    status: str,
    code: str,
    ttl_sec: int = 259200,
) -> bool:
    """Block every new Seedance 2.5 charge until one job is reconciled."""
    jid = str(job_id or "").strip()
    normalized = str(status or "").strip().lower()
    if not jid:
        raise ValueError("job_id is required")
    if normalized not in {"in_progress", "refund_pending", "review_required"}:
        raise ValueError(f"unsupported settlement block status: {normalized}")
    key = _reliable_user_settlement_key(queue_name, user_id)
    script = """
local current_job = redis.call('HGET', KEYS[1], 'job_id')
if current_job and current_job ~= ARGV[1] then
    return 0
end
redis.call('HSET', KEYS[1],
    'job_id', ARGV[1],
    'status', ARGV[2],
    'code', ARGV[3],
    'updated_at', ARGV[4])
redis.call('EXPIRE', KEYS[1], ARGV[5])
return 1
"""
    r = await get_redis()
    stored = int(await r.eval(
        script,
        1,
        key,
        jid,
        normalized,
        str(code or "").strip(),
        time.time(),
        max(3600, int(ttl_sec or 259200)),
    ) or 0)
    return bool(stored)


async def clear_reliable_user_settlement_block(
    *,
    queue_name: str,
    user_id: int,
    job_id: str,
) -> bool:
    """Clear only the settlement block owned by ``job_id``."""
    jid = str(job_id or "").strip()
    if not jid:
        return False
    key = _reliable_user_settlement_key(queue_name, user_id)
    script = """
local current_job = redis.call('HGET', KEYS[1], 'job_id')
if not current_job then
    return 1
end
if current_job ~= ARGV[1] then
    return 0
end
redis.call('DEL', KEYS[1])
return 1
"""
    r = await get_redis()
    return bool(int(await r.eval(script, 1, key, jid) or 0))


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
