import asyncio
import os
import time
import traceback
from typing import Any, Dict

from main import process_telegram_update
from tg_update_queue import (
    ack_tg_update_job,
    dequeue_tg_update_job,
    fail_tg_update_job,
    heartbeat_tg_update_job,
    requeue_stale_tg_updates,
)


TG_UPDATE_QUEUE_NAME = (os.getenv("TG_UPDATE_QUEUE_NAME", "tg_update") or "tg_update").strip()
TG_UPDATE_WORKER_SLEEP_SEC = float(os.getenv("TG_UPDATE_WORKER_SLEEP_SEC", "1") or "1")
TG_UPDATE_WARN_DELAY_SEC = float(os.getenv("TG_UPDATE_WARN_DELAY_SEC", "10") or "10")
TG_UPDATE_DEQUEUE_TIMEOUT_SEC = float(os.getenv("TG_UPDATE_DEQUEUE_TIMEOUT_SEC", "5") or "5")
TG_UPDATE_HEARTBEAT_SEC = float(os.getenv("TG_UPDATE_HEARTBEAT_SEC", "30") or "30")
TG_UPDATE_PROCESSING_TIMEOUT_SEC = float(os.getenv("TG_UPDATE_PROCESSING_TIMEOUT_SEC", "1800") or "1800")
TG_UPDATE_RECOVERY_INTERVAL_SEC = float(os.getenv("TG_UPDATE_RECOVERY_INTERVAL_SEC", "60") or "60")
TG_UPDATE_MAX_ATTEMPTS = max(1, int(os.getenv("TG_UPDATE_MAX_ATTEMPTS", "3") or "3"))


async def _heartbeat_loop(job_id: str, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=max(5.0, TG_UPDATE_HEARTBEAT_SEC))
        except asyncio.TimeoutError:
            try:
                await heartbeat_tg_update_job(job_id, queue_name=TG_UPDATE_QUEUE_NAME)
            except Exception as exc:
                print(f"[tg_update_worker] heartbeat error job_id={job_id}: {exc!r}", flush=True)


async def _handle_job(job: Dict[str, Any]) -> None:
    update = job.get("update")
    if not isinstance(update, dict):
        print(f"[tg_update_worker] skip bad job job_id={job.get('job_id')}", flush=True)
        return

    received_ts = float(job.get("received_ts") or 0)
    if received_ts:
        delay = time.time() - received_ts
        if delay >= TG_UPDATE_WARN_DELAY_SEC:
            print(
                f"[tg_update_worker] queue_delay={delay:.1f}s "
                f"job_id={job.get('job_id')} update_id={job.get('update_id')} attempt={job.get('_attempt')}",
                flush=True,
            )

    await process_telegram_update(update)


async def _recover_if_needed(last_recovery_ts: float) -> float:
    now_ts = time.time()
    if now_ts - last_recovery_ts < TG_UPDATE_RECOVERY_INTERVAL_SEC:
        return last_recovery_ts

    try:
        stats = await requeue_stale_tg_updates(
            stale_after_sec=TG_UPDATE_PROCESSING_TIMEOUT_SEC,
            max_attempts=TG_UPDATE_MAX_ATTEMPTS,
            queue_name=TG_UPDATE_QUEUE_NAME,
        )
        if stats.get("requeued") or stats.get("dead"):
            print(f"[tg_update_worker] recovered stale processing jobs: {stats}", flush=True)
    except Exception as exc:
        print(f"[tg_update_worker] stale recovery error: {exc!r}", flush=True)
        traceback.print_exc()

    return now_ts


async def main_loop() -> None:
    print(
        f"[tg_update_worker] started queue={TG_UPDATE_QUEUE_NAME} "
        f"max_attempts={TG_UPDATE_MAX_ATTEMPTS} processing_timeout={TG_UPDATE_PROCESSING_TIMEOUT_SEC}s",
        flush=True,
    )

    last_recovery_ts = 0.0

    while True:
        try:
            last_recovery_ts = await _recover_if_needed(last_recovery_ts)

            job = await dequeue_tg_update_job(
                timeout_sec=TG_UPDATE_DEQUEUE_TIMEOUT_SEC,
                queue_name=TG_UPDATE_QUEUE_NAME,
            )
            if not job:
                continue

            job_id = str(job.get("_queue_job_id") or job.get("job_id") or "")
            stop_heartbeat = asyncio.Event()
            heartbeat_task = asyncio.create_task(_heartbeat_loop(job_id, stop_heartbeat)) if job_id else None

            try:
                await _handle_job(job)
                if job_id:
                    await ack_tg_update_job(job_id, queue_name=TG_UPDATE_QUEUE_NAME)
            except Exception as exc:
                print(f"[tg_update_worker] job error job_id={job_id}: {exc!r}", flush=True)
                traceback.print_exc()
                if job_id:
                    action = await fail_tg_update_job(
                        job,
                        error=repr(exc),
                        max_attempts=TG_UPDATE_MAX_ATTEMPTS,
                        queue_name=TG_UPDATE_QUEUE_NAME,
                    )
                    print(f"[tg_update_worker] job_id={job_id} action={action}", flush=True)
            finally:
                stop_heartbeat.set()
                if heartbeat_task:
                    heartbeat_task.cancel()
                    try:
                        await heartbeat_task
                    except asyncio.CancelledError:
                        pass

        except Exception as exc:
            print(f"[tg_update_worker] loop error: {exc!r}", flush=True)
            traceback.print_exc()
            await asyncio.sleep(TG_UPDATE_WORKER_SLEEP_SEC)


if __name__ == "__main__":
    asyncio.run(main_loop())
