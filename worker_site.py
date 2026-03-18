from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, Optional

import httpx

from app.services.site_builder_service import process_site_job
from queue_redis import dequeue_job

SITE_QUEUE_NAME = (os.getenv("SITE_QUEUE_NAME", "site") or "site").strip() or "site"
MAX_CONCURRENCY = int(os.getenv("SITE_WORKER_CONCURRENCY", "2") or 2)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else ""


def _project_ready_keyboard(project_id: str, version_number: int) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "✏️ Редактировать сайт", "callback_data": f"site:edit:start:{project_id}"}],
            [{"text": f"📦 Скачать v{int(version_number)}", "callback_data": f"site:download:{project_id}:{int(version_number)}"}],
            [{"text": "🗂 Мои сайты", "callback_data": "site:projects"}],
        ]
    }


async def tg_send_message(chat_id: int, text: str, *, reply_markup: Optional[dict] = None) -> None:
    if not TG_API:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
    payload: Dict[str, Any] = {"chat_id": int(chat_id), "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    async with httpx.AsyncClient(timeout=30.0) as client:
        await client.post(f"{TG_API}/sendMessage", json=payload)


async def tg_send_document_bytes(chat_id: int, doc_bytes: bytes, *, filename: str, caption: Optional[str] = None, reply_markup: Optional[dict] = None) -> None:
    if not TG_API:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    files = {"document": (filename, doc_bytes, "application/zip")}
    async with httpx.AsyncClient(timeout=180.0) as client:
        await client.post(f"{TG_API}/sendDocument", data=data, files=files)


async def _handle(job: Dict[str, Any], sem: asyncio.Semaphore) -> None:
    async with sem:
        job_id = str(job.get("job_id") or job.get("id") or "").strip()
        if not job_id:
            print("[site] skipped job without job_id", flush=True)
            return
        try:
            result = await process_site_job(job_id)
            project = result["project"]
            version = result["version"]
            zip_bytes = result["zip_bytes"]
            chat_id = int(project["telegram_user_id"])
            version_number = int(version.get("version_number") or 1)
            await tg_send_document_bytes(
                chat_id,
                zip_bytes,
                filename=f"site-v{version_number}.zip",
                caption=(
                    f"✅ Ваш сайт готов. Это версия v{version_number}.\n"
                    "Внутри ZIP: index.html, styles.css, script.js и README.txt."
                ),
                reply_markup=_project_ready_keyboard(str(project["id"]), version_number),
            )
            if version_number == 1:
                await tg_send_message(
                    chat_id,
                    "В стоимость уже включен 1 бесплатный пакет правок. Если нужно что-то изменить, нажмите «Редактировать сайт».",
                )
            print(f"[site] completed job={job_id} project={project['id']} version=v{version_number}", flush=True)
        except Exception as exc:
            user_id = int((job.get("telegram_user_id") or 0) or 0)
            try:
                if user_id > 0:
                    await tg_send_message(
                        user_id,
                        "❌ Не удалось завершить создание или правку сайта. Токены возвращены автоматически, если были списаны. Попробуйте ещё раз или уточните бриф.",
                    )
            except Exception:
                pass
            print(f"[site] failed job={job_id} error={exc}", flush=True)


async def main() -> None:
    print(f"[site] worker started queue={SITE_QUEUE_NAME} concurrency={MAX_CONCURRENCY}", flush=True)
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    tasks: set[asyncio.Task] = set()

    while True:
        job: Optional[Dict[str, Any]] = await dequeue_job(timeout_sec=10, queue_name=SITE_QUEUE_NAME)
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
