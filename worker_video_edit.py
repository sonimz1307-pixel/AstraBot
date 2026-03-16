import asyncio
import os
from typing import Any, Dict, Optional

from queue_redis import dequeue_job
from app.services.video_editor_service import (
    VIDEO_EDIT_QUEUE_NAME as WORKSPACE_VIDEO_EDIT_QUEUE_NAME,
    process_workspace_video_edit_job,
)

try:
    from app.services.video_editor_v2_service import (
        VIDEO_EDITOR_QUEUE_NAME as VIDEO_EDITOR_V2_QUEUE_NAME,
        process_render_job as process_video_editor_v2_render_job,
    )
except Exception:
    VIDEO_EDITOR_V2_QUEUE_NAME = None
    process_video_editor_v2_render_job = None

MAX_CONCURRENCY = int(os.getenv("VIDEO_EDIT_WORKER_CONCURRENCY", "2"))


def _queue_names() -> list[str]:
    names = [WORKSPACE_VIDEO_EDIT_QUEUE_NAME]
    if VIDEO_EDITOR_V2_QUEUE_NAME and VIDEO_EDITOR_V2_QUEUE_NAME not in names:
        names.append(VIDEO_EDITOR_V2_QUEUE_NAME)
    return names


def _resolve_kind(job: Dict[str, Any]) -> str:
    kind = str(job.get("kind") or "").strip().lower()
    if kind:
        return kind

    queue_name = str(job.get("queue_name") or "").strip().lower()
    if queue_name == str(VIDEO_EDITOR_V2_QUEUE_NAME or "").strip().lower():
        return "video_editor_v2_render"
    return "workspace_video_edit"


async def _handle(job: Dict[str, Any], sem: asyncio.Semaphore) -> None:
    async with sem:
        job_id = str(job.get("job_id") or job.get("id") or "").strip()
        if not job_id:
            print("[video_edit] skipped job without job_id", flush=True)
            return

        kind = _resolve_kind(job)
        try:
            if kind == "video_editor_v2_render":
                if process_video_editor_v2_render_job is None:
                    raise RuntimeError(
                        "video_editor_v2 service is not available; add app/services/video_editor_v2_service.py"
                    )
                await asyncio.to_thread(process_video_editor_v2_render_job, job_id)
                print(f"[video_edit] completed v2 job={job_id}", flush=True)
                return

            await asyncio.to_thread(process_workspace_video_edit_job, job_id)
            print(f"[video_edit] completed v1 job={job_id}", flush=True)
        except Exception as exc:
            print(f"[video_edit] failed kind={kind or 'unknown'} job={job_id} error={exc}", flush=True)


async def main() -> None:
    queue_names = _queue_names()
    print(
        f"[video_edit] worker started queues={queue_names} concurrency={MAX_CONCURRENCY}",
        flush=True,
    )
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    tasks: set[asyncio.Task] = set()

    while True:
        job: Optional[Dict[str, Any]] = await dequeue_job(
            timeout_sec=10,
            queue_names=queue_names,
        )
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
