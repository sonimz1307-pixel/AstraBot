import asyncio
import os
from typing import Any, Dict, Optional

from queue_redis import dequeue_job
from app.services.workspace_worker_jobs import process_workspace_image_job

WORKSPACE_IMAGE_QUEUE_NAME = (os.getenv("WORKSPACE_IMAGE_QUEUE_NAME", "workspace_image") or "workspace_image").strip() or "workspace_image"
WORKSPACE_IMAGE_CONCURRENCY = int(os.getenv("WORKSPACE_IMAGE_CONCURRENCY", "3"))
image_sem = asyncio.Semaphore(WORKSPACE_IMAGE_CONCURRENCY)


async def _handle(job: Dict[str, Any]) -> None:
    kind = str(job.get("kind") or "").strip().lower()
    async with image_sem:
        if kind != "workspace_image_run":
            print(f"[workspace_image] skipped unsupported kind={kind} job={job.get('job_id')}", flush=True)
            return
        await process_workspace_image_job(job)
        print(f"[workspace_image] completed image job={job.get('job_id')}", flush=True)


async def main() -> None:
    print(f"[workspace_image] worker started queue={WORKSPACE_IMAGE_QUEUE_NAME} concurrency={WORKSPACE_IMAGE_CONCURRENCY}", flush=True)
    tasks: set[asyncio.Task] = set()
    while True:
        job: Optional[Dict[str, Any]] = await dequeue_job(timeout_sec=10, queue_name=WORKSPACE_IMAGE_QUEUE_NAME)
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
