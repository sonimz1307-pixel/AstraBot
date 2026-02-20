from __future__ import annotations

import asyncio
import os
import socket
import traceback
from typing import Any, Dict

from app.services.mi_tasks import claim_next_task, finish_task
from app.routers.leads import _orchestrate_full_job
from app.routers.admin_top import _enrich_selected_internal


WORKER_ID = os.getenv("WORKER_ID") or socket.gethostname()


async def _run_task(task: Dict[str, Any]) -> None:
    task_id = str(task.get("id"))
    payload = task.get("payload") or {}
    ttype = (task.get("task_type") or "").strip()

    try:
        if ttype == "run_full_job":
            await _orchestrate_full_job(
                job_id=str(payload.get("job_id") or task.get("job_id")),
                tg_user_id=int(payload["tg_user_id"]),
                city=str(payload["city"]),
                niche=str(payload["niche"]),
                limit=payload.get("limit"),
                yandex_max_items=int(payload.get("yandex_max_items") or 6),
                yandex_actor_id=str(payload.get("yandex_actor_id") or payload.get("yandex_actor_id") or ""),
                actor_id_2gis=str(payload.get("actor_id_2gis") or ""),
                actor_input_2gis_override=payload.get("actor_input_2gis_override") or None,
                actor_input_yandex_override=payload.get("actor_input_yandex_override") or None,
                max_places=int(payload.get("max_places") or 0) or None,
                max_seconds=int(payload.get("max_seconds") or 0) or None,
                yandex_retries=int(payload.get("yandex_retries") or 1),
                sleep_ms=int(payload.get("sleep_ms") or 0),
            )
        elif ttype == "enrich_selected":
            await _enrich_selected_internal(
                job_id=str(payload.get("job_id") or task.get("job_id")),
                max_urls_per_place=int(payload.get("max_urls_per_place") or 5),
                timeout_sec=float(payload.get("timeout_sec") or 25.0),
                write_raw=bool(payload.get("write_raw") if payload.get("write_raw") is not None else True),
            )
        else:
            raise RuntimeError(f"Unknown task_type: {ttype}")

        finish_task(task_id=task_id, ok=True)
    except Exception as e:
        finish_task(task_id=task_id, ok=False, error=f"{type(e).__name__}: {e}")
        # Print stack for Render logs
        print("TASK FAILED:", task_id, ttype)
        traceback.print_exc()


async def main() -> None:
    print(f"[worker] started id={WORKER_ID}")
    while True:
        task = claim_next_task(worker_id=WORKER_ID)
        if not task:
            await asyncio.sleep(2.0)
            continue
        await _run_task(task)
        await asyncio.sleep(0.2)


if __name__ == "__main__":
    asyncio.run(main())
