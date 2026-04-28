from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, Optional

import httpx

from app.services.site_builder_service import process_site_job
from billing_db import add_tokens
from kling3_kie_runner import run_kling3_kie_task_and_wait
from queue_redis import dequeue_job

SITE_QUEUE_NAME = (os.getenv("SITE_QUEUE_NAME", "site") or "site").strip() or "site"
KLING3_KIE_QUEUE_NAME = (os.getenv("KLING3_KIE_QUEUE_NAME", "kling3_kie") or "kling3_kie").strip() or "kling3_kie"
SITE_WORKER_CONCURRENCY = max(1, int(os.getenv("SITE_WORKER_CONCURRENCY", "1") or "1"))
KLING3_KIE_WORKER_CONCURRENCY = max(1, int(os.getenv("KLING3_KIE_WORKER_CONCURRENCY", "3") or "3"))
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


async def tg_send_video_url(chat_id: int, video_url: str, *, caption: Optional[str] = None) -> None:
    if not TG_API:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
    payload: Dict[str, Any] = {"chat_id": int(chat_id), "video": str(video_url)}
    if caption:
        payload["caption"] = caption
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(f"{TG_API}/sendVideo", json=payload)
    try:
        data = resp.json()
    except Exception:
        data = {}
    if resp.status_code >= 400 or (isinstance(data, dict) and data.get("ok") is False):
        await tg_send_message(chat_id, f"✅ Kling 3.0 - New готов. Видео: {video_url}")




def _kling3_kie_download_keyboard(video_url: str) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "⬇️ Скачать 4K видео", "url": str(video_url)}],
        ]
    }


async def tg_send_kling3_kie_4k_link(chat_id: int, video_url: str) -> None:
    await tg_send_message(
        chat_id,
        "✅ Kling 3.0 - New готов.\n\n"
        "Качество: 4K\n"
        "Видео отправляю ссылкой, чтобы Telegram не упёрся в размер файла.\n\n"
        f"Ссылка: {video_url}",
        reply_markup=_kling3_kie_download_keyboard(video_url),
    )


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


async def _handle_site(job: Dict[str, Any], sem: asyncio.Semaphore) -> None:
    async with sem:
        job_id = str(job.get("job_id") or job.get("id") or "").strip()
        if not job_id:
            print("[redactor/site] skipped job without job_id", flush=True)
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
                caption=f"✅ Ваш сайт готов. Это версия v{version_number}.\nВнутри ZIP: index.html, styles.css, script.js и README.txt.",
                reply_markup=_project_ready_keyboard(str(project["id"]), version_number),
            )
            if version_number == 1:
                await tg_send_message(chat_id, "В стоимость уже включен 1 бесплатный пакет правок. Если нужно что-то изменить, нажмите «Редактировать сайт».")
            print(f"[redactor/site] completed job={job_id}", flush=True)
        except Exception as exc:
            user_id = int((job.get("telegram_user_id") or 0) or 0)
            try:
                if user_id > 0:
                    await tg_send_message(user_id, "❌ Не удалось завершить создание или правку сайта. Токены возвращены автоматически, если были списаны.")
            except Exception:
                pass
            print(f"[redactor/site] failed job={job_id} error={exc}", flush=True)


async def _handle_kling3_kie(job: Dict[str, Any], sem: asyncio.Semaphore) -> None:
    async with sem:
        job_id = str(job.get("job_id") or "").strip()
        origin = str(job.get("origin") or "").strip().lower()
        charge_tokens = int(job.get("charge_tokens") or 0)
        user_id = int(job.get("user_id") or 0)
        charge_ref_id = str(job.get("charge_ref_id") or "")
        refund_reason = str(job.get("refund_reason") or "kling3_kie_refund")
        try:
            task_id, raw_task, video_url = await run_kling3_kie_task_and_wait(
                prompt=str(job.get("prompt") or ""),
                duration=int(job.get("duration") or 5),
                mode=str(job.get("kie_mode") or job.get("mode_quality") or "pro"),
                enable_audio=bool(job.get("enable_audio")),
                aspect_ratio=str(job.get("aspect_ratio") or "16:9"),
                generation_mode=str(job.get("mode") or "text_to_video"),
                start_image_url=str(job.get("start_image_url") or job.get("start_frame_url") or "").strip() or None,
                end_image_url=str(job.get("end_image_url") or job.get("end_frame_url") or job.get("last_frame_url") or "").strip() or None,
                multi_shots=job.get("multi_shots") or [],
                kling_elements=job.get("kling_elements") or [],
                poll_interval_sec=float(os.getenv("KLING3_KIE_POLL_INTERVAL_SEC", "5") or "5"),
                timeout_sec=int(os.getenv("KLING3_KIE_TIMEOUT_SEC", "1800") or "1800"),
            )
            if not video_url:
                raise RuntimeError(f"KIE completed without video url. taskId={task_id}")

            if origin == "workspace":
                from app.routers import web_workspace_api as ww

                generation_id = str(job.get("generation_id") or "").strip()
                if not generation_id:
                    raise RuntimeError("workspace kling3_kie job missing generation_id")
                ww._update_workspace_generation(generation_id, {"task_id": task_id, "provider_video_url": video_url, "status": "processing"})
                await ww._finalize_workspace_generation_from_url(generation_id=generation_id, user_id=user_id, provider_video_url=video_url)
            else:
                chat_id = int(job.get("chat_id") or 0)
                if chat_id:
                    kie_mode = str(job.get("kie_mode") or job.get("mode_quality") or "").strip().lower()
                    if kie_mode == "4k":
                        await tg_send_kling3_kie_4k_link(chat_id, video_url)
                    else:
                        await tg_send_video_url(chat_id, video_url, caption="✅ Kling 3.0 - New готов")
            print(f"[redactor/kling3_kie] completed job={job_id} taskId={task_id}", flush=True)
        except Exception as exc:
            if charge_tokens > 0 and user_id > 0:
                try:
                    add_tokens(user_id, charge_tokens, reason=refund_reason, ref_id=charge_ref_id or None, meta={"origin": origin or "kling3_kie", "job_id": job_id, "error": str(exc)[:300]})
                except TypeError:
                    add_tokens(user_id, charge_tokens, reason=refund_reason)
                except Exception:
                    pass
            if origin == "workspace":
                try:
                    from app.routers import web_workspace_api as ww
                    generation_id = str(job.get("generation_id") or "").strip()
                    if generation_id:
                        ww._mark_workspace_generation_failed(generation_id, str(exc), error_code="provider_error")
                except Exception:
                    pass
            else:
                try:
                    chat_id = int(job.get("chat_id") or 0)
                    if chat_id:
                        await tg_send_message(chat_id, f"❌ Kling 3.0 - New: ошибка генерации. Токены возвращены.\n{str(exc)[:800]}")
                except Exception:
                    pass
            print(f"[redactor/kling3_kie] failed job={job_id} error={exc}", flush=True)


async def _site_loop() -> None:
    sem = asyncio.Semaphore(SITE_WORKER_CONCURRENCY)
    tasks: set[asyncio.Task] = set()
    while True:
        job = await dequeue_job(timeout_sec=10, queue_name=SITE_QUEUE_NAME)
        if job:
            tasks.add(asyncio.create_task(_handle_site(job, sem)))
        done = {t for t in tasks if t.done()}
        tasks -= done


async def _kling_loop() -> None:
    sem = asyncio.Semaphore(KLING3_KIE_WORKER_CONCURRENCY)
    tasks: set[asyncio.Task] = set()
    while True:
        job = await dequeue_job(timeout_sec=10, queue_name=KLING3_KIE_QUEUE_NAME)
        if job:
            tasks.add(asyncio.create_task(_handle_kling3_kie(job, sem)))
        done = {t for t in tasks if t.done()}
        tasks -= done


async def main() -> None:
    print(
        f"[redactor] worker started site_queue={SITE_QUEUE_NAME} site_concurrency={SITE_WORKER_CONCURRENCY} "
        f"kling_queue={KLING3_KIE_QUEUE_NAME} kling_concurrency={KLING3_KIE_WORKER_CONCURRENCY}",
        flush=True,
    )
    await asyncio.gather(_site_loop(), _kling_loop())


if __name__ == "__main__":
    asyncio.run(main())
