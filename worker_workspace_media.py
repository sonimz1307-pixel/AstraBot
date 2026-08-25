import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from billing_db import supabase as billing_supabase
from wan3_billing import (
    list_wan3_recovery_charges, mark_wan3_refund_reconciled, refund_wan3_once, wan3_refund_exists,
)
from wan3_queue_guard import assert_wan3_queue_isolated
from wan3_reliable import wan3_reliable_enqueue_known, wan3_release_enqueue_marker
from queue_redis import (
    ack_reliable_job, delete_reliable_job_state, dequeue_job, dequeue_reliable_job,
    get_reliable_job_state, promote_due_delayed_jobs, requeue_reliable_job,
    set_reliable_job_state, touch_reliable_job,
)
from app.services.workspace_worker_jobs import (
    process_tg_grok_video_job,
    process_tg_kling3_turbo_video_job,
    process_tg_omni_flash_video_job,
    process_tg_tts_job,
    process_tg_veo_relax_video_job,
    process_tg_wan3_video_job,
    process_workspace_music_job,
    process_workspace_switchx_ref_job,
    process_workspace_tts_job,
    process_workspace_video_job,
)

WORKSPACE_MEDIA_QUEUE_NAME = (os.getenv("WORKSPACE_MEDIA_QUEUE_NAME", "workspace_media") or "workspace_media").strip() or "workspace_media"
WORKSPACE_VEO_RELAX_QUEUE_NAME = (os.getenv("WORKSPACE_VEO_RELAX_QUEUE_NAME", "workspace_veo_relax") or "workspace_veo_relax").strip() or "workspace_veo_relax"
WORKSPACE_GROK15_QUEUE_NAME = (os.getenv("WORKSPACE_GROK15_QUEUE_NAME", "workspace_grok15") or "workspace_grok15").strip() or "workspace_grok15"
TG_TTS_QUEUE_NAME = (os.getenv("TG_TTS_QUEUE_NAME", "workspace_tg_tts") or "workspace_tg_tts").strip() or "workspace_tg_tts"
SEEDANCE25_QUEUE_NAME = (os.getenv("SEEDANCE25_QUEUE_NAME", "seedance25") or "seedance25").strip() or "seedance25"
WAN3_QUEUE_NAME = (os.getenv("WAN3_QUEUE_NAME", "wan3") or "wan3").strip() or "wan3"
KLING3_KIE_QUEUE_NAME = (os.getenv("KLING3_KIE_QUEUE_NAME", "kling3_kie") or "kling3_kie").strip() or "kling3_kie"
VIDEO_CONCURRENCY = int(os.getenv("WORKSPACE_VIDEO_CONCURRENCY", "3"))
OMNI_CONCURRENCY = int(os.getenv("WORKSPACE_OMNI_CONCURRENCY", "3"))
MUSIC_CONCURRENCY = int(os.getenv("WORKSPACE_MUSIC_CONCURRENCY", "2"))
TTS_CONCURRENCY = int(os.getenv("WORKSPACE_TTS_CONCURRENCY", "4"))
TG_TTS_CONCURRENCY = int(os.getenv("TG_TTS_CONCURRENCY", "2"))
VEO_RELAX_CONCURRENCY = int(os.getenv("WORKSPACE_VEO_RELAX_CONCURRENCY", "2"))
GROK15_CONCURRENCY = int(os.getenv("WORKSPACE_GROK15_CONCURRENCY", "2"))
WAN3_CONCURRENCY = max(1, int(os.getenv("WAN3_CONCURRENCY", "2") or "2"))
WAN3_RELIABLE_STALE_SEC = max(120, int(os.getenv("WAN3_RELIABLE_STALE_SEC", "300") or "300"))
WAN3_RELIABLE_TOUCH_SEC = max(15, int(os.getenv("WAN3_RELIABLE_TOUCH_SEC", "60") or "60"))
WAN3_REFUND_RECONCILE_SEC = max(15, int(os.getenv("WAN3_REFUND_RECONCILE_SEC", "60") or "60"))
WAN3_ORPHAN_REFUND_GRACE_SEC = max(120, int(os.getenv("WAN3_ORPHAN_REFUND_GRACE_SEC", "900") or "900"))
WAN3_REFUND_SCAN_BATCH_SIZE = max(25, min(1000, int(os.getenv("WAN3_REFUND_SCAN_BATCH_SIZE", "250") or "250")))
DELAYED_PROMOTE_BATCH_SIZE = max(1, int(os.getenv("WORKSPACE_DELAYED_PROMOTE_BATCH_SIZE", "50") or "50"))

video_sem = asyncio.Semaphore(VIDEO_CONCURRENCY)
omni_sem = asyncio.Semaphore(OMNI_CONCURRENCY)
veo_relax_sem = asyncio.Semaphore(VEO_RELAX_CONCURRENCY)
grok15_sem = asyncio.Semaphore(GROK15_CONCURRENCY)
wan3_sem = asyncio.Semaphore(WAN3_CONCURRENCY)
music_sem = asyncio.Semaphore(MUSIC_CONCURRENCY)
tts_sem = asyncio.Semaphore(TTS_CONCURRENCY)
tg_tts_sem = asyncio.Semaphore(TG_TTS_CONCURRENCY)


def _job_kind(job: Dict[str, Any]) -> str:
    return str(job.get("kind") or "").strip().lower()


def _sem_for_job(job: Dict[str, Any]) -> asyncio.Semaphore:
    kind = _job_kind(job)
    provider = str(job.get("provider") or "").strip().lower()
    model = str(job.get("model") or "").strip().lower()
    if kind == "tg_omni_flash_video_run" or (kind == "workspace_video_run" and provider == "google"):
        return omni_sem
    if kind == "tg_veo_relax_video_run" or (provider == "veo" and model == "veo-3.1-fast-relax"):
        return veo_relax_sem
    if (kind == "tg_grok_video_run" and model == "grok-imagine-video-1.5") or (kind == "workspace_video_run" and provider == "grok" and model == "grok-imagine-video-1.5"):
        return grok15_sem
    if kind in {"workspace_video_run", "workspace_switchx_ref_run", "tg_grok_video_run", "tg_kling3_turbo_video_run"}:
        return video_sem
    if kind == "workspace_music_run":
        return music_sem
    if kind == "tg_tts_run":
        return tg_tts_sem
    return tts_sem


async def _handle(job: Dict[str, Any]) -> None:
    kind = _job_kind(job)
    sem = _sem_for_job(job)
    async with sem:
        if kind == "workspace_video_run":
            await process_workspace_video_job(job)
            print(f"[workspace_media] completed video job={job.get('job_id')}", flush=True)
            return
        if kind == "workspace_switchx_ref_run":
            await process_workspace_switchx_ref_job(job)
            print(f"[workspace_media] completed switchx_ref job={job.get('job_id')}", flush=True)
            return
        if kind == "tg_grok_video_run":
            await process_tg_grok_video_job(job)
            print(f"[workspace_media] completed tg_grok job={job.get('job_id')}", flush=True)
            return
        if kind == "tg_kling3_turbo_video_run":
            await process_tg_kling3_turbo_video_job(job)
            print(f"[workspace_media] completed tg_kling3_turbo job={job.get('job_id')}", flush=True)
            return
        if kind == "tg_omni_flash_video_run":
            await process_tg_omni_flash_video_job(job)
            print(f"[workspace_media] completed tg_omni_flash job={job.get('job_id')}", flush=True)
            return
        if kind == "tg_veo_relax_video_run":
            await process_tg_veo_relax_video_job(job)
            print(f"[workspace_media] completed tg_veo_relax job={job.get('job_id')}", flush=True)
            return
        if kind == "workspace_music_run":
            await process_workspace_music_job(job)
            print(f"[workspace_media] completed music job={job.get('job_id')}", flush=True)
            return
        if kind == "tg_tts_run":
            await process_tg_tts_job(job)
            print(f"[workspace_media] completed tg_tts job={job.get('job_id')}", flush=True)
            return
        if kind == "workspace_tts_run":
            await process_workspace_tts_job(job)
            print(f"[workspace_media] completed tts job={job.get('job_id')}", flush=True)
            return
        print(f"[workspace_media] skipped unsupported kind={kind} job={job.get('job_id')}", flush=True)


async def _consume_queue(queue_name: str, label: str, *, promote_delayed: bool = False) -> None:
    """Consume one Redis queue inside this worker process.

    Veo Relax intentionally uses its own queue, but this consumer runs inside the
    existing workspace media worker process. That keeps the Render topology unchanged
    while preventing Veo Relax jobs from mixing with the regular media queue.

    For Relax delayed jobs, this consumer first promotes due ZSET items into the
    normal list queue. The expensive provider call starts only after promotion,
    so the worker does not occupy a concurrency slot during the waiting period.
    """
    print(
        f"[workspace_media] consumer started label={label} queue={queue_name} "
        f"promote_delayed={promote_delayed}",
        flush=True,
    )
    tasks: set[asyncio.Task] = set()
    while True:
        if promote_delayed:
            await promote_due_delayed_jobs(queue_name=queue_name, limit=DELAYED_PROMOTE_BATCH_SIZE)
        job: Optional[Dict[str, Any]] = await dequeue_job(timeout_sec=5 if promote_delayed else 10, queue_name=queue_name)
        if not job:
            done = {t for t in tasks if t.done()}
            tasks -= done
            continue
        task = asyncio.create_task(_handle(job))
        tasks.add(task)
        done = {t for t in tasks if t.done()}
        tasks -= done


def _wan3_load_task_id_from_ledger(charge_ref_id: str) -> str:
    """Recover a created KIE taskId from the original Wan charge row."""
    ref = str(charge_ref_id or "").strip()
    if not ref or billing_supabase is None:
        return ""
    try:
        response = (
            billing_supabase.table("bot_balance_ledger")
            .select("id,meta")
            .eq("reason", "wan3_video")
            .eq("ref_id", ref)
            .limit(1)
            .execute()
        )
        rows = list(getattr(response, "data", None) or [])
        if not rows:
            return ""
        meta = (rows[0] or {}).get("meta")
        if not isinstance(meta, dict):
            return ""
        return str(meta.get("provider_task_id") or "").strip()
    except Exception as exc:
        print(f"[workspace_media/wan3] ledger taskId read failed ref={ref}: {exc}", flush=True)
        return ""


def _wan3_persist_task_id_to_ledger(charge_ref_id: str, task_id: str, job_id: str) -> bool:
    """Persist KIE taskId in JSONB meta of the existing ``wan3_video`` charge."""
    ref = str(charge_ref_id or "").strip()
    task = str(task_id or "").strip()
    if not ref or not task or billing_supabase is None:
        return False
    try:
        response = (
            billing_supabase.table("bot_balance_ledger")
            .select("id,meta")
            .eq("reason", "wan3_video")
            .eq("ref_id", ref)
            .limit(1)
            .execute()
        )
        rows = list(getattr(response, "data", None) or [])
        if not rows:
            return False
        row = rows[0] or {}
        row_id = str(row.get("id") or "").strip()
        if not row_id:
            return False
        meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
        merged = dict(meta)
        merged.update({
            "provider": "kie",
            "provider_task_id": task,
            "wan3_job_id": str(job_id or ""),
            "provider_task_saved_at": int(time.time()),
            # Once a provider taskId is durable, this charge is no longer an
            # orphan-enqueue candidate. Reliable task resume owns recovery now.
            "wan3_recovery_open": False,
            "wan3_recovery_closed_reason": "provider_task_id_saved",
            "wan3_recovery_closed_at": datetime.now(timezone.utc).isoformat(),
        })
        billing_supabase.table("bot_balance_ledger").update({"meta": merged}).eq("id", row_id).execute()
        return _wan3_load_task_id_from_ledger(ref) == task
    except Exception as exc:
        print(f"[workspace_media/wan3] ledger taskId persist failed ref={ref}: {exc}", flush=True)
        return False


async def _wan3_persist_task_id_reliably(*, job: Dict[str, Any], task_id: str) -> None:
    """Do not continue after createTask until taskId exists in Redis or Supabase."""
    task = str(task_id or "").strip()
    job_id = str(job.get("job_id") or "").strip()
    charge_ref_id = str(job.get("charge_ref_id") or "").strip()
    if not task:
        raise RuntimeError("Wan 3.0 KIE returned an empty taskId")
    job["resume_task_id"] = task
    job["provider_task_id"] = task
    attempt = 0
    while True:
        attempt += 1
        db_ok = await asyncio.to_thread(
            _wan3_persist_task_id_to_ledger,
            charge_ref_id,
            task,
            job_id,
        )
        redis_ok = False
        if job_id:
            try:
                await set_reliable_job_state(
                    queue_name=WAN3_QUEUE_NAME,
                    job_id=job_id,
                    updates={"task_id": task, "phase": "provider_running"},
                )
                redis_ok = True
            except Exception as exc:
                print(
                    f"[workspace_media/wan3] Redis taskId persist retry job={job_id} attempt={attempt}: {exc}",
                    flush=True,
                )
        if db_ok and redis_ok:
            try:
                await wan3_release_enqueue_marker(queue_name=WAN3_QUEUE_NAME, job_id=job_id)
            except Exception as exc:
                print(f"[workspace_media/wan3] enqueue marker release deferred job={job_id}: {exc}", flush=True)
            return
        if db_ok or redis_ok:
            print(
                f"[workspace_media/wan3] taskId redundancy incomplete; retrying before provider polling "
                f"job={job_id} redis={redis_ok} supabase={db_ok}",
                flush=True,
            )
        if attempt == 1 or attempt % 12 == 0:
            print(
                f"[workspace_media/wan3] CRITICAL: taskId not durable yet; retrying "
                f"job={job_id} taskId={task} attempt={attempt}",
                flush=True,
            )
        await asyncio.sleep(min(5.0, 0.5 * attempt))


class _Wan3LeaseLost(RuntimeError):
    pass


async def _wan3_lease_heartbeat(job: Dict[str, Any]) -> None:
    while True:
        await asyncio.sleep(float(WAN3_RELIABLE_TOUCH_SEC))
        owned = await touch_reliable_job(queue_name=WAN3_QUEUE_NAME, job=job)
        if owned is False:
            raise _Wan3LeaseLost(f"Wan 3.0 reliable lease lost for job={job.get('job_id')}")
        # owned=None is a transient Redis problem. Keep the provider request
        # running for now; if another consumer reclaims the entry, the next
        # successful heartbeat observes ownership loss and cancels this handler.


def _wan3_charge_age_seconds(created_at: Any) -> Optional[float]:
    raw = str(created_at or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())
    except Exception:
        return None


async def _wan3_reconcile_refunds_once() -> None:
    try:
        rows = await asyncio.to_thread(list_wan3_recovery_charges, batch_size=WAN3_REFUND_SCAN_BATCH_SIZE)
    except Exception as exc:
        print(f"[workspace_media/wan3] refund reconcile ledger scan failed: {exc}", flush=True)
        return

    for row in rows:
        try:
            ref_id = str(row.get("ref_id") or "").strip()
            user_id = int(row.get("telegram_user_id") or 0)
            delta = int(row.get("delta_tokens") or 0)
            meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
            job_id = str(meta.get("wan3_job_id") or "").strip()
            queue_name = str(meta.get("wan3_queue_name") or WAN3_QUEUE_NAME).strip() or WAN3_QUEUE_NAME
            refund_reason = str(meta.get("wan3_refund_reason") or "wan3_video_refund").strip() or "wan3_video_refund"
            tokens = int(meta.get("wan3_refund_tokens") or abs(delta) or 0)
            if not ref_id or not user_id or not job_id or tokens <= 0:
                continue

            # A provider taskId is stronger evidence than Redis: this charge already
            # crossed the provider boundary and must be resumed, never orphan-refunded.
            # If a prior worker failed to bound the persistent enqueue marker after
            # saving taskId, cleanup it here without refreshing an existing TTL.
            if str(meta.get("provider_task_id") or "").strip():
                try:
                    await wan3_release_enqueue_marker(queue_name=queue_name, job_id=job_id)
                except Exception:
                    pass
                continue
            try:
                if await asyncio.to_thread(wan3_refund_exists, ref_id, reason=refund_reason):
                    try:
                        await wan3_release_enqueue_marker(queue_name=queue_name, job_id=job_id)
                    except Exception:
                        pass
                    try:
                        await asyncio.to_thread(mark_wan3_refund_reconciled, ref_id, stage="refund_already_exists")
                    except Exception:
                        pass
                    continue
            except Exception as exc:
                print(f"[workspace_media/wan3] refund existence check failed ref={ref_id}: {exc}", flush=True)
                continue

            known = await wan3_reliable_enqueue_known(queue_name=queue_name, job_id=job_id, make_durable=True)
            if known is None:
                continue
            if known:
                # The dedupe key is now persistent until the provider taskId is
                # durably stored, so even a multi-day worker outage cannot turn a
                # queued job into a false refund.
                continue

            age_sec = _wan3_charge_age_seconds(row.get("created_at"))
            pending = bool(meta.get("wan3_refund_pending"))
            if not pending and (age_sec is None or age_sec < WAN3_ORPHAN_REFUND_GRACE_SEC):
                continue

            await asyncio.to_thread(
                refund_wan3_once,
                user_id,
                tokens,
                reason=refund_reason,
                ref_id=ref_id,
                meta={
                    "origin": str(meta.get("origin") or "wan3_reconciler"),
                    "stage": "orphan_enqueue_reconcile",
                    "wan3_job_id": job_id,
                    "queue_name": queue_name,
                },
            )
            try:
                await asyncio.to_thread(mark_wan3_refund_reconciled, ref_id, stage="orphan_enqueue_reconcile")
            except Exception:
                pass
            print(f"[workspace_media/wan3] reconciled orphan charge ref={ref_id} job={job_id} tokens={tokens}", flush=True)
        except Exception as exc:
            print(f"[workspace_media/wan3] refund reconcile row failed: {exc}", flush=True)


async def _wan3_refund_reconciler_loop() -> None:
    # Small initial delay lets queue consumers establish Redis connections first.
    await asyncio.sleep(5.0)
    while True:
        try:
            await _wan3_reconcile_refunds_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[workspace_media/wan3] refund reconciler unexpected error: {exc}", flush=True)
        await asyncio.sleep(WAN3_REFUND_RECONCILE_SEC)


async def _wan3_worker_slot(slot: int) -> None:
    consumer = f"workspace_media:wan3:{os.getpid()}:{slot}"
    print(f"[workspace_media] wan3 reliable slot={slot} queue={WAN3_QUEUE_NAME}", flush=True)
    while True:
        job: Optional[Dict[str, Any]] = None
        heartbeat: Optional[asyncio.Task] = None
        handler_task: Optional[asyncio.Task] = None
        try:
            job = await dequeue_reliable_job(
                queue_name=WAN3_QUEUE_NAME,
                consumer_name=consumer,
                timeout_sec=10,
                stale_after_sec=WAN3_RELIABLE_STALE_SEC,
            )
            if not job:
                continue

            job_id = str(job.get("job_id") or "").strip()
            state = await get_reliable_job_state(queue_name=WAN3_QUEUE_NAME, job_id=job_id) if job_id else {}
            terminal = str((state or {}).get("terminal") or "").strip().lower()
            if terminal in {"completed", "failed"}:
                print(f"[workspace_media] wan3 skip terminal replay job={job_id} terminal={terminal}", flush=True)
                acked = await ack_reliable_job(queue_name=WAN3_QUEUE_NAME, job=job)
                if acked and job_id:
                    await delete_reliable_job_state(queue_name=WAN3_QUEUE_NAME, job_id=job_id)
                    try:
                        await wan3_release_enqueue_marker(queue_name=WAN3_QUEUE_NAME, job_id=job_id)
                    except Exception:
                        pass
                job = None
                continue

            resume_task_id = str((state or {}).get("task_id") or job.get("resume_task_id") or job.get("provider_task_id") or "").strip()
            charge_ref_id = str(job.get("charge_ref_id") or "").strip()
            if not resume_task_id and charge_ref_id:
                resume_task_id = await asyncio.to_thread(_wan3_load_task_id_from_ledger, charge_ref_id)
                if resume_task_id:
                    job["provider_task_id"] = resume_task_id
                    if job_id:
                        try:
                            await set_reliable_job_state(
                                queue_name=WAN3_QUEUE_NAME,
                                job_id=job_id,
                                updates={"task_id": resume_task_id, "phase": "provider_running"},
                            )
                        except Exception:
                            pass
                    print(f"[workspace_media] wan3 recovered taskId from billing ledger job={job_id}", flush=True)
            if resume_task_id:
                job["resume_task_id"] = resume_task_id
                job["provider_task_id"] = resume_task_id

            async def _persist_task_id(task_id: str) -> None:
                await _wan3_persist_task_id_reliably(job=job, task_id=task_id)

            async def _run_job() -> bool:
                async with wan3_sem:
                    if _job_kind(job) == "tg_wan3_video_run":
                        return bool(await process_tg_wan3_video_job(job, on_provider_task_id=_persist_task_id))
                    return bool(await process_workspace_video_job(job, on_provider_task_id=_persist_task_id))

            heartbeat = asyncio.create_task(_wan3_lease_heartbeat(job))
            handler_task = asyncio.create_task(_run_job())
            done, _pending = await asyncio.wait({handler_task, heartbeat}, return_when=asyncio.FIRST_COMPLETED)

            if heartbeat in done:
                lease_exc = heartbeat.exception()
                if lease_exc is not None:
                    if handler_task and not handler_task.done():
                        handler_task.cancel()
                        try:
                            await handler_task
                        except asyncio.CancelledError:
                            pass
                    if isinstance(lease_exc, _Wan3LeaseLost):
                        print(f"[workspace_media] wan3 lease lost; stop old handler slot={slot} job={job_id}", flush=True)
                        # Ownership already moved to another consumer. Do not ACK
                        # or requeue from the old worker.
                        job = None
                        heartbeat = None
                        handler_task = None
                        continue
                    raise lease_exc

            ok = bool(await handler_task)
            handler_task = None
            if heartbeat:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass
                heartbeat = None

            if job_id:
                await set_reliable_job_state(
                    queue_name=WAN3_QUEUE_NAME,
                    job_id=job_id,
                    updates={
                        "task_id": str(job.get("resume_task_id") or resume_task_id or ""),
                        "terminal": "completed" if ok else "failed",
                        "phase": "completed" if ok else "failed",
                    },
                )
            acked = await ack_reliable_job(queue_name=WAN3_QUEUE_NAME, job=job)
            if acked and job_id:
                await delete_reliable_job_state(queue_name=WAN3_QUEUE_NAME, job_id=job_id)
                try:
                    await wan3_release_enqueue_marker(queue_name=WAN3_QUEUE_NAME, job_id=job_id)
                except Exception:
                    pass
            print(f"[workspace_media] wan3 {'completed' if ok else 'terminal-failed'} job={job_id}", flush=True)
            job = None
        except asyncio.CancelledError:
            if heartbeat:
                heartbeat.cancel()
            if handler_task and not handler_task.done():
                handler_task.cancel()
                try:
                    await handler_task
                except asyncio.CancelledError:
                    pass
            if job:
                try:
                    await asyncio.shield(requeue_reliable_job(queue_name=WAN3_QUEUE_NAME, job=job))
                    print(f"[workspace_media] wan3 requeued active job on shutdown slot={slot} job={job.get('job_id')}", flush=True)
                except Exception as requeue_exc:
                    print(f"[workspace_media] wan3 FAILED to requeue shutdown job slot={slot}: {requeue_exc}", flush=True)
            raise
        except Exception as exc:
            if heartbeat:
                heartbeat.cancel()
            if handler_task and not handler_task.done():
                handler_task.cancel()
            # Leave an ordinary retryable failure unacked. A stale reclaim resumes
            # the persisted KIE taskId instead of creating a second paid task.
            print(f"[workspace_media] wan3 retryable job={job.get('job_id') if job else None}: {exc}", flush=True)
            await asyncio.sleep(2.0)


async def main() -> None:
    assert_wan3_queue_isolated(WAN3_QUEUE_NAME)
    print(
        f"[workspace_media] worker started "
        f"media_queue={WORKSPACE_MEDIA_QUEUE_NAME} "
        f"veo_relax_queue={WORKSPACE_VEO_RELAX_QUEUE_NAME} "
        f"grok15_queue={WORKSPACE_GROK15_QUEUE_NAME} "
        f"tg_tts_queue={TG_TTS_QUEUE_NAME} seedance25_queue={SEEDANCE25_QUEUE_NAME} wan3_queue={WAN3_QUEUE_NAME} "
        f"video={VIDEO_CONCURRENCY} omni={OMNI_CONCURRENCY} "
        f"veo_relax={VEO_RELAX_CONCURRENCY} grok15={GROK15_CONCURRENCY} "
        f"music={MUSIC_CONCURRENCY} tts={TTS_CONCURRENCY} tg_tts={TG_TTS_CONCURRENCY} wan3={WAN3_CONCURRENCY}",
        flush=True,
    )
    media_promotes_delayed = WORKSPACE_VEO_RELAX_QUEUE_NAME == WORKSPACE_MEDIA_QUEUE_NAME
    consumers = [
        asyncio.create_task(_consume_queue(WORKSPACE_MEDIA_QUEUE_NAME, "media", promote_delayed=media_promotes_delayed)),
    ]
    if WORKSPACE_VEO_RELAX_QUEUE_NAME != WORKSPACE_MEDIA_QUEUE_NAME:
        consumers.append(asyncio.create_task(_consume_queue(WORKSPACE_VEO_RELAX_QUEUE_NAME, "veo_relax", promote_delayed=True)))
    else:
        print(
            "[workspace_media] WARNING: WORKSPACE_VEO_RELAX_QUEUE_NAME equals WORKSPACE_MEDIA_QUEUE_NAME; "
            "Veo Relax jobs will share the regular media queue.",
            flush=True,
        )
    if WORKSPACE_GROK15_QUEUE_NAME not in {WORKSPACE_MEDIA_QUEUE_NAME, WORKSPACE_VEO_RELAX_QUEUE_NAME}:
        consumers.append(asyncio.create_task(_consume_queue(WORKSPACE_GROK15_QUEUE_NAME, "grok15")))
    else:
        print(
            "[workspace_media] WARNING: WORKSPACE_GROK15_QUEUE_NAME overlaps another queue; "
            "Grok 1.5 jobs will not have a fully separate Redis queue.",
            flush=True,
        )
    if TG_TTS_QUEUE_NAME not in {WORKSPACE_MEDIA_QUEUE_NAME, WORKSPACE_VEO_RELAX_QUEUE_NAME, WORKSPACE_GROK15_QUEUE_NAME}:
        consumers.append(asyncio.create_task(_consume_queue(TG_TTS_QUEUE_NAME, "tg_tts")))
    else:
        print(
            "[workspace_media] WARNING: TG_TTS_QUEUE_NAME overlaps another queue; "
            "Telegram TTS jobs will share an existing Redis queue.",
            flush=True,
        )
    # Wan 3.0 has a separate reliable queue and semaphore, but stays inside
    # this existing worker process. It never consumes regular video/Seedance slots.
    for slot in range(1, WAN3_CONCURRENCY + 1):
        consumers.append(asyncio.create_task(_wan3_worker_slot(slot)))
    # Reconcile the only cross-system uncertainty: charge committed in Supabase
    # while the Redis enqueue response was lost/unavailable. No extra worker is
    # created; this lightweight loop runs inside the existing media process.
    consumers.append(asyncio.create_task(_wan3_refund_reconciler_loop()))
    await asyncio.gather(*consumers)


if __name__ == "__main__":
    asyncio.run(main())
