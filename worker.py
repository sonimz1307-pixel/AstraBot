from __future__ import annotations

import asyncio
import gc
import logging
import os
import socket
import traceback
from typing import Any, Dict

# -----------------------------------------------------------------------------
# Worker for MI queue (mi_tasks)
# Goals:
#   - Stable idle behavior (low traffic, low CPU)
#   - No noisy httpx/httpcore logs
#   - Avoid memory creep during long idle periods
# -----------------------------------------------------------------------------

# Silence httpx/httpcore logs ASAP (Render log spam)
for _name in ("httpx", "httpcore", "httpx._client", "httpcore._sync", "httpcore._async"):
    lg = logging.getLogger(_name)
    lg.setLevel(logging.CRITICAL)
    lg.propagate = False
    lg.disabled = True

WORKER_ID = os.getenv("WORKER_ID") or socket.gethostname()

# These imports come after log silencing
from app.services.mi_tasks import claim_next_task, finish_task
from app.routers.leads import _orchestrate_full_job
from app.routers.admin_top import _enrich_selected_internal


def _silence_httpx_again() -> None:
    # Some libs can re-enable loggers after import; force-disable again.
    for name in ("httpx", "httpcore", "httpx._client", "httpcore._sync", "httpcore._async"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.CRITICAL)
        lg.propagate = False
        lg.disabled = True


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
                yandex_actor_id=str(payload.get("yandex_actor_id") or ""),
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
        # Keep error string short and safe
        err = f"{type(e).__name__}: {e}"
        finish_task(task_id=task_id, ok=False, error=err)
        print("TASK FAILED:", task_id, ttype)
        traceback.print_exc()

    finally:
        # Best-effort memory cleanup between tasks
        gc.collect()


async def main() -> None:
    _silence_httpx_again()
    print(f"[worker] started id={WORKER_ID}")

    # Idle backoff settings (tuneable via env)
    base_sleep_s = float(os.getenv("MI_IDLE_SLEEP_BASE", "5"))
    max_sleep_s = float(os.getenv("MI_IDLE_SLEEP_MAX", "30"))

    sleep_s = base_sleep_s
    idle_loops = 0

    while True:
        _silence_httpx_again()

        task = claim_next_task(worker_id=WORKER_ID)

        if not task:
            idle_loops += 1
            await asyncio.sleep(sleep_s)

            # Exponential backoff (fast) to reduce DB polling when idle
            sleep_s = min(max_sleep_s, sleep_s * 2)

            # Periodic GC while idle to prevent slow memory creep
            if idle_loops % 10 == 0:
                gc.collect()

            continue

        # Got a task: reset backoff
        idle_loops = 0
        sleep_s = base_sleep_s

        await _run_task(task)

        # Small pause to avoid hammering DB in tight loops
        await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
