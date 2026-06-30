import os
import time
import traceback
from typing import Any, Dict
from uuid import uuid4

from fastapi import FastAPI, Request, Response

from tg_update_queue import enqueue_tg_update_job, get_tg_update_queue_stats


app = FastAPI(title="AstraBot Telegram Webhook")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
TG_UPDATE_QUEUE_NAME = (os.getenv("TG_UPDATE_QUEUE_NAME", "tg_update") or "tg_update").strip()


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"ok": True, "service": "tg_webhook", "queue": TG_UPDATE_QUEUE_NAME}


@app.get("/queue/health")
async def queue_health() -> Dict[str, Any]:
    """Operational endpoint for Render/manual checks."""
    try:
        return await get_tg_update_queue_stats(queue_name=TG_UPDATE_QUEUE_NAME)
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "queue_name": TG_UPDATE_QUEUE_NAME}


@app.post("/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    """Fast Telegram entrypoint: accept update, enqueue to Redis, return quickly."""
    if secret != WEBHOOK_SECRET:
        return Response(status_code=403)

    try:
        update = await request.json()
    except Exception:
        return Response(status_code=400)

    if not isinstance(update, dict):
        return Response(status_code=400)

    update_id = update.get("update_id")
    job_id = f"tg_update:{update_id}" if update_id is not None else f"tg_update:{uuid4()}"

    job = {
        "job_id": job_id,
        "kind": "tg_update",
        "update_id": update_id,
        "update": update,
        "received_ts": time.time(),
        "source": "tg_webhook.py",
    }

    try:
        await enqueue_tg_update_job(job, queue_name=TG_UPDATE_QUEUE_NAME)
    except Exception as exc:
        print(f"[tg_webhook] Redis enqueue failed job_id={job_id}: {exc!r}", flush=True)
        traceback.print_exc()
        # Telegram will retry when webhook returns a 5xx response.
        return Response(status_code=503)

    return {"ok": True, "queued": True, "job_id": job_id}
