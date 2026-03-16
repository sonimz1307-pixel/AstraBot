import asyncio
import os
from typing import Any, Dict

from queue_redis import dequeue_job
from app.services.video_editor_service import VIDEO_EDIT_QUEUE_NAME, process_workspace_video_edit_job

MAX_CONCURRENCY = int(os.getenv("VIDEO_EDIT_WORKER_CONCURRENCY", "2"))


async def _handle(job: Dict[str, Any], sem: asyncio.Semaphore) -> None:
    async with sem:
        job_id = str(job.get("job_id") or job.get("id") or "").strip()
        if not job_id:
            return
        try:
            await asyncio.to_thread(process_workspace_video_edit_job, job_id)
            print(f"[video_edit] completed job={job_id}", flush=True)
        except Exception as exc:
            print(f"[video_edit] failed job={job_id} error={exc}", flush=True)


async def main() -> None:
    print(f"[video_edit] worker started queue={VIDEO_EDIT_QUEUE_NAME} concurrency={MAX_CONCURRENCY}", flush=True)
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    tasks: set[asyncio.Task] = set()
    while True:
        job = await dequeue_job(timeout_sec=10, queue_name=VIDEO_EDIT_QUEUE_NAME)
        if not job:
            done = {t for t in tasks if t.done()}
            tasks -= done
            continue
        task = asyncio.create_task(_handle(job, sem))
        tasks.add(task)
        done = {t for t in tasks if t.done()}
        tasks -= done


if __name__ == "__main__":
    asyncio.run(main())
