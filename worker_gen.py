import os
import asyncio
import json
from typing import Any, Dict, Optional

import httpx

from queue_redis import dequeue_job

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")  # matches your Render env
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else None

# Concurrency inside ONE worker instance (you can scale instances on Render too)
MAX_CONCURRENCY = int(os.getenv("GEN_WORKER_CONCURRENCY", "5"))


async def tg_send_message(chat_id: int, text: str) -> None:
    if not TG_API:
        print("TELEGRAM_BOT_TOKEN not set; cannot send message")
        return
    async with httpx.AsyncClient(timeout=20.0) as client:
        await client.post(f"{TG_API}/sendMessage", json={"chat_id": chat_id, "text": text})


async def handle_job(job: Dict[str, Any]) -> None:
    """
    Минимальный обработчик: пока просто подтверждает, что очередь работает.
    Дальше мы добавим реальные типы job (nano/kling/photosession).
    """
    job_type = job.get("type") or job.get("job_type") or "unknown"
    chat_id = job.get("chat_id")
    user_id = job.get("user_id")

    print("JOB:", json.dumps(job, ensure_ascii=False))

    if chat_id:
        await tg_send_message(int(chat_id), f"✅ Воркер получил задачу: {job_type}\njob_id={job.get('job_id')}")


async def worker_loop() -> None:
    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async def _run_one(job: Dict[str, Any]) -> None:
        async with sem:
            try:
                await handle_job(job)
            except Exception as e:
                print("Job failed:", e)

    while True:
        job = await dequeue_job(timeout_sec=10)
        if not job:
            continue
        asyncio.create_task(_run_one(job))


def main() -> None:
    print("Gen worker started. concurrency =", MAX_CONCURRENCY)
    asyncio.run(worker_loop())


if __name__ == "__main__":
    main()
