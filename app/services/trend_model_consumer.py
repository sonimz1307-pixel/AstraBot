"""Consume the paid SQL outbox INSIDE existing model worker processes.

Ordinary Redis consumers and trend consumers share the SAME semaphore. No
private recipe or expiring signed URL is copied to another transport. A lease
is acquired only after a local execution slot is available.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
from uuid import uuid4

from app.services import trend_finance as finance

LOG = logging.getLogger('nabex.trends.models')
MODELS = frozenset({'gpt_image_2_kie', 'seedream_5_pro', 'wan3', 'seedance25'})
READY_SECONDS = 15


def trend_capacity(total: int) -> int:
    """Leave at least one normal slot when the configured pool has >1 slot.

    A one-slot pool alternates through the semaphore; it cannot reserve a
    second slot without increasing the user's configured provider concurrency.
    """
    try:
        requested = max(1, int(os.getenv('TREND_MODEL_CONCURRENCY', '1')))
    except ValueError:
        requested = 1
    return min(requested, max(1, total - 1))


async def run_model_group(name: str, model_keys: list[str], semaphore: asyncio.Semaphore,
                          capacity: int) -> None:
    if (os.getenv('TREND_MODEL_WORKER_ENABLED', '1') or '1').strip().lower() in {'0', 'false', 'off', 'no'}:
        return
    if capacity < 1 or not model_keys or not set(model_keys) <= MODELS:
        LOG.error('Trend consumer disabled: invalid group=%s capacity=%s', name, capacity)
        return
    try:
        from worker_trends import _with_lease, LEASE_SECONDS, POLL_SECONDS
    except Exception as exc:
        # A broken/missing optional addon cannot terminate ordinary consumers
        # and must not advertise readiness for the SQL cutover.
        LOG.error('Trend consumer unavailable group=%s error=%s', name, type(exc).__name__)
        return
    if not 30 <= LEASE_SECONDS <= 3600:
        # SQL rejects this lease on every claim. Do not let a healthy readiness
        # RPC falsely authorize a cutover to a consumer that cannot take jobs.
        LOG.error('Trend consumer unavailable group=%s: TREND_WORKER_LEASE_SECONDS '
                  'must not exceed 3600 (effective value=%s)', name, LEASE_SECONDS)
        return
    slots = trend_capacity(capacity)
    worker_id = f'models:{socket.gethostname()}:{os.getpid()}:{name}:{uuid4().hex[:8]}'
    enabled = asyncio.Event()
    print(f'[trends/model] group={name} models={",".join(model_keys)} '
          f'capacity={capacity} trend_capacity={slots}; waiting for SQL activation', flush=True)

    async def advertise() -> None:
        last_mode = None
        while True:
            try:
                state = await asyncio.to_thread(finance.touch_model_worker, worker_id, model_keys, capacity, slots)
                active = state.get('shared_workers_enabled') is True
                if active != last_mode:
                    print(f'[trends/model] group={name} mode={"active" if active else "standby"}', flush=True)
                    last_mode = active
                if active:
                    enabled.set()
                else:
                    enabled.clear()
            except Exception as exc:
                # Missing migration/temporary DB outage cannot stop ordinary jobs.
                enabled.clear()
                LOG.warning('Trend readiness deferred group=%s error=%s', name, type(exc).__name__)
            await asyncio.sleep(READY_SECONDS)

    async def consume(slot: int) -> None:
        owner = f'{worker_id}:{slot}'
        while True:
            await enabled.wait()
            run = None
            try:
                async with semaphore:
                    run = await asyncio.to_thread(finance.claim_model_run, worker_id=owner,
                                                  model_keys=model_keys, lease_seconds=LEASE_SECONDS)
                    if run:
                        # Prepare fresh URLs after claiming, never while queuing.
                        print(f'[trends/model] claimed group={name} run={run["id"]}', flush=True)
                        await _with_lease(run, owner, defer_settlement=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # SQL retains the lease/task identity for safe recovery. Never
                # expose provider payloads, private prompts, or URLs in logs.
                LOG.warning('Trend execution deferred group=%s run=%s error=%s', name,
                            (run or {}).get('id'), type(exc).__name__)
            await asyncio.sleep(POLL_SECONDS if not run else 0.1)

    tasks = [asyncio.create_task(advertise())]
    tasks.extend(asyncio.create_task(consume(slot)) for slot in range(1, slots + 1))
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
