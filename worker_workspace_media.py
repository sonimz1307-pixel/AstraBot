import asyncio
import os
from typing import Any, Dict, Optional

from queue_redis import dequeue_job, promote_due_delayed_jobs
from app.services.workspace_worker_jobs import (
    process_tg_grok_video_job,
    process_tg_kling3_turbo_video_job,
    process_tg_omni_flash_video_job,
    process_tg_veo_relax_video_job,
    process_workspace_music_job,
    process_workspace_switchx_ref_job,
    process_workspace_tts_job,
    process_workspace_video_job,
)

WORKSPACE_MEDIA_QUEUE_NAME = (os.getenv("WORKSPACE_MEDIA_QUEUE_NAME", "workspace_media") or "workspace_media").strip() or "workspace_media"
WORKSPACE_VEO_RELAX_QUEUE_NAME = (os.getenv("WORKSPACE_VEO_RELAX_QUEUE_NAME", "workspace_veo_relax") or "workspace_veo_relax").strip() or "workspace_veo_relax"
WORKSPACE_GROK15_QUEUE_NAME = (os.getenv("WORKSPACE_GROK15_QUEUE_NAME", "workspace_grok15") or "workspace_grok15").strip() or "workspace_grok15"
VIDEO_CONCURRENCY = int(os.getenv("WORKSPACE_VIDEO_CONCURRENCY", "3"))
OMNI_CONCURRENCY = int(os.getenv("WORKSPACE_OMNI_CONCURRENCY", "3"))
MUSIC_CONCURRENCY = int(os.getenv("WORKSPACE_MUSIC_CONCURRENCY", "2"))
TTS_CONCURRENCY = int(os.getenv("WORKSPACE_TTS_CONCURRENCY", "4"))
VEO_RELAX_CONCURRENCY = int(os.getenv("WORKSPACE_VEO_RELAX_CONCURRENCY", "2"))
GROK15_CONCURRENCY = int(os.getenv("WORKSPACE_GROK15_CONCURRENCY", "2"))
DELAYED_PROMOTE_BATCH_SIZE = max(1, int(os.getenv("WORKSPACE_DELAYED_PROMOTE_BATCH_SIZE", "50") or "50"))

video_sem = asyncio.Semaphore(VIDEO_CONCURRENCY)
omni_sem = asyncio.Semaphore(OMNI_CONCURRENCY)
veo_relax_sem = asyncio.Semaphore(VEO_RELAX_CONCURRENCY)
grok15_sem = asyncio.Semaphore(GROK15_CONCURRENCY)
music_sem = asyncio.Semaphore(MUSIC_CONCURRENCY)
tts_sem = asyncio.Semaphore(TTS_CONCURRENCY)


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


async def main() -> None:
    print(
        f"[workspace_media] worker started "
        f"media_queue={WORKSPACE_MEDIA_QUEUE_NAME} "
        f"veo_relax_queue={WORKSPACE_VEO_RELAX_QUEUE_NAME} "
        f"grok15_queue={WORKSPACE_GROK15_QUEUE_NAME} "
        f"video={VIDEO_CONCURRENCY} omni={OMNI_CONCURRENCY} "
        f"veo_relax={VEO_RELAX_CONCURRENCY} grok15={GROK15_CONCURRENCY} "
        f"music={MUSIC_CONCURRENCY} tts={TTS_CONCURRENCY}",
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
    await asyncio.gather(*consumers)


if __name__ == "__main__":
    asyncio.run(main())
