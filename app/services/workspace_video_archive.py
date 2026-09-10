"""Retry Storage persistence of proven video results; never call a generation API.

The SQL outbox survives worker restarts. A separate coroutine in worker_trends
handles it even when the marketplace feature is disabled. No balance mutations.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import suppress

LOG = logging.getLogger('nabex.video_archive')
LEASE_SECONDS = 180


def rpc(name: str, **params):
    from billing_db import supabase
    if supabase is None:
        raise RuntimeError('Supabase is required for archive recovery')
    result = supabase.rpc('nabex_video_archive_' + name, {'p_' + k: v for k, v in params.items()}).execute().data
    if not isinstance(result, dict):
        raise RuntimeError('Invalid archive recovery response')
    return result


def enqueue(generation_id: str, user_id: int, source_url: str, error_code: str = 'archive_pending') -> dict:
    return rpc('enqueue', generation_id=str(generation_id), user_id=int(user_id), source_url=str(source_url), error_code=error_code)


def retry_delay(attempts: int, *, too_large: bool = False) -> int:
    return min(21600 if too_large else 3600, (900 if too_large else 60) * 2 ** min(max(int(attempts)-1, 0), 8))


def get_history(job: dict) -> dict:
    from billing_db import supabase
    rows = supabase.table('workspace_video_generations').select('*').eq('id', job['generation_id']).eq('user_id', str(job['user_id'])).limit(1).execute().data or []
    return dict(rows[0]) if rows else {}


async def process(job: dict, worker_id: str) -> None:
    from app.routers import web_workspace_api as ww
    generation_id = job['generation_id']
    history = await asyncio.to_thread(get_history, job)
    if not history or history.get('deleted_at') or history.get('storage_path') or history.get('status') in {'failed', 'cancelled'}:
        await asyncio.to_thread(rpc, 'finish', generation_id=generation_id, worker_id=worker_id)
        return
    temporary = ''
    try:
        temporary, size, mime = await ww._download_video_to_tempfile(job['source_url'])
        upload_task = asyncio.create_task(asyncio.to_thread(ww._upload_workspace_video_file,
            local_path=temporary, user_id=int(job['user_id']), generation_id=generation_id, content_type=mime))
        # Do not remove a file while a cancelled to_thread upload still reads it.
        try:
            result = await asyncio.shield(upload_task)
        except asyncio.CancelledError:
            with suppress(Exception):
                await upload_task
            raise
        await asyncio.to_thread(rpc, 'finish', generation_id=generation_id, worker_id=worker_id,
            storage_path=result['storage_path'], file_size=int(result.get('file_size_bytes') or size), mime_type=result.get('mime_type') or mime)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        too_large = isinstance(exc, ww._WorkspaceStoragePayloadTooLargeError)
        # Even 403/404 can be temporary. After 3 failed fetches, make the
        # unavailable result visible for manual review instead of claiming success.
        response = getattr(exc, 'response', None)
        expired = not temporary and getattr(response, 'status_code', 0) in {401, 403, 404, 410}
        review = bool(expired and int(job.get('attempts') or 0) >= 3)
        await asyncio.to_thread(rpc, 'finish', generation_id=generation_id, worker_id=worker_id,
            error_code='archive_too_large' if too_large else 'source_unavailable' if expired else 'archive_error',
            delay_seconds=retry_delay(job.get('attempts') or 1, too_large=too_large), needs_review=review)
        LOG.warning('Archive deferred generation=%s kind=%s review=%s', generation_id, type(exc).__name__, review)
    finally:
        if temporary:
            with suppress(OSError):
                os.remove(temporary)


async def heartbeat(job: dict, worker_id: str) -> None:
    while True:
        await asyncio.sleep(LEASE_SECONDS // 3)
        await asyncio.to_thread(rpc, 'heartbeat', generation_id=job['generation_id'], worker_id=worker_id, lease_seconds=LEASE_SECONDS)


async def run_once(worker_id: str) -> bool:
    job = await asyncio.to_thread(rpc, 'claim', worker_id=worker_id, lease_seconds=LEASE_SECONDS)
    if not job:
        return False
    operation = asyncio.create_task(process(job, worker_id))
    renewal = asyncio.create_task(heartbeat(job, worker_id))
    try:
        done, _ = await asyncio.wait({operation, renewal}, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            await task
    finally:
        for task in (operation, renewal):
            task.cancel()
        for task in (operation, renewal):
            with suppress(asyncio.CancelledError):
                await task
    return True


async def run_forever(worker_id: str) -> None:
    last_scan = 0.0
    while True:
        try:
            if time.monotonic() - last_scan >= 60:
                await asyncio.to_thread(rpc, 'backfill', limit=25)
                last_scan = time.monotonic()
            if not await run_once(worker_id):
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning('Archive recovery deferred kind=%s', type(exc).__name__)
            await asyncio.sleep(5)
