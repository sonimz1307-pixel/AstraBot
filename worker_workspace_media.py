import asyncio
import os
from typing import Any, Dict, Optional

from queue_redis import dequeue_job
from app.services.workspace_worker_jobs import (
    process_tg_grok_video_job,
    process_tg_omni_flash_video_job,
    process_workspace_music_job,
    process_workspace_switchx_ref_job,
    process_workspace_tts_job,
    process_workspace_video_job,
)

WORKSPACE_MEDIA_QUEUE_NAME = (os.getenv("WORKSPACE_MEDIA_QUEUE_NAME", "workspace_media") or "workspace_media").strip() or "workspace_media"
VIDEO_CONCURRENCY = int(os.getenv("WORKSPACE_VIDEO_CONCURRENCY", "3"))
OMNI_CONCURRENCY = int(os.getenv("WORKSPACE_OMNI_CONCURRENCY", "3"))
MUSIC_CONCURRENCY = int(os.getenv("WORKSPACE_MUSIC_CONCURRENCY", "2"))
TTS_CONCURRENCY = int(os.getenv("WORKSPACE_TTS_CONCURRENCY", "4"))

video_sem = asyncio.Semaphore(VIDEO_CONCURRENCY)
omni_sem = asyncio.Semaphore(OMNI_CONCURRENCY)
music_sem = asyncio.Semaphore(MUSIC_CONCURRENCY)
tts_sem = asyncio.Semaphore(TTS_CONCURRENCY)


def _job_kind(job: Dict[str, Any]) -> str:
    return str(job.get("kind") or "").strip().lower()


def _sem_for_job(job: Dict[str, Any]) -> asyncio.Semaphore:
    kind = _job_kind(job)
    provider = str(job.get("provider") or "").strip().lower()
    if kind == "tg_omni_flash_video_run" or (kind == "workspace_video_run" and provider == "google"):
        return omni_sem
    if kind in {"workspace_video_run", "workspace_switchx_ref_run", "tg_grok_video_run"}:
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
        if kind == "tg_omni_flash_video_run":
            await process_tg_omni_flash_video_job(job)
            print(f"[workspace_media] completed tg_omni_flash job={job.get('job_id')}", flush=True)
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


async def main() -> None:
    print(
        f"[workspace_media] worker started queue={WORKSPACE_MEDIA_QUEUE_NAME} "
        f"video={VIDEO_CONCURRENCY} omni={OMNI_CONCURRENCY} music={MUSIC_CONCURRENCY} tts={TTS_CONCURRENCY}",
        flush=True,
    )
    tasks: set[asyncio.Task] = set()
    while True:
        job: Optional[Dict[str, Any]] = await dequeue_job(timeout_sec=10, queue_name=WORKSPACE_MEDIA_QUEUE_NAME)
        if not job:
            done = {t for t in tasks if t.done()}
            tasks -= done
            continue
        task = asyncio.create_task(_handle(job))
        tasks.add(task)
        done = {t for t in tasks if t.done()}
        tasks -= done


if __name__ == "__main__":
    asyncio.run(main())
