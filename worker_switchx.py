from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Any, Dict, Optional

import httpx

from billing_db import add_tokens
from queue_redis import dequeue_job
from switchx_service import SwitchXClient, SwitchXError, guess_content_type
from switchx_types import JOB_TYPE_SWITCHX, RESOLUTION_720, RESOLUTION_1080

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else None
TG_FILE = f"https://api.telegram.org/file/bot{BOT_TOKEN}" if BOT_TOKEN else None
MAX_CONCURRENCY = int(os.getenv("SWITCHX_WORKER_CONCURRENCY", "2"))
SWITCHX_TIMEOUT_SEC = int(os.getenv("SWITCHX_TIMEOUT_SEC", "3600"))
SWITCHX_POLL_SEC = float(os.getenv("SWITCHX_POLL_SEC", "8"))


async def tg_send_message(chat_id: int, text: str, reply_markup: Optional[dict] = None) -> Optional[int]:
    if not TG_API:
        return None
    payload: dict[str, Any] = {"chat_id": int(chat_id), "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(f"{TG_API}/sendMessage", json=payload)
    try:
        j = r.json()
        if j.get("ok"):
            return int((j.get("result") or {}).get("message_id") or 0) or None
    except Exception:
        pass
    return None


async def tg_edit_message_text(chat_id: int, message_id: int, text: str) -> None:
    if not TG_API:
        return
    async with httpx.AsyncClient(timeout=20.0) as client:
        await client.post(
            f"{TG_API}/editMessageText",
            json={"chat_id": int(chat_id), "message_id": int(message_id), "text": text},
        )


async def tg_get_file_path(file_id: str) -> Optional[str]:
    if not TG_API:
        return None
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(f"{TG_API}/getFile", params={"file_id": file_id})
    r.raise_for_status()
    j = r.json()
    if not j.get("ok"):
        return None
    return (j.get("result") or {}).get("file_path")


async def http_get_bytes(url: str, *, timeout: float = 120.0) -> bytes:
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        r = await client.get(url)
    r.raise_for_status()
    return r.content


async def tg_download_file_bytes(file_id: str) -> tuple[bytes, str]:
    if not TG_FILE:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
    file_path = await tg_get_file_path(file_id)
    if not file_path:
        raise RuntimeError("Telegram getFile returned no file_path")
    url = f"{TG_FILE}/{file_path}"
    data = await http_get_bytes(url, timeout=180.0)
    fp = file_path.lower()
    ext = "bin"
    for e in ("mp4", "mov", "jpg", "jpeg", "png", "webp"):
        if fp.endswith("." + e):
            ext = e
            break
    return data, ext


async def tg_send_video_bytes(chat_id: int, video_bytes: bytes, *, filename: str = "switchx.mp4", caption: Optional[str] = None) -> None:
    if not TG_API:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
    files = {"video": (filename, video_bytes, "video/mp4")}
    async with httpx.AsyncClient(timeout=300.0) as client:
        r = await client.post(f"{TG_API}/sendVideo", data=data, files=files)
    try:
        j = r.json()
        if j.get("ok"):
            return
        raise RuntimeError(j.get("description") or "Telegram sendVideo failed")
    except Exception:
        if r.status_code >= 300:
            raise RuntimeError(f"Telegram sendVideo failed: {r.status_code} {r.text[:500]}")


def _progress_seq() -> list[int]:
    raw = (os.getenv("SWITCHX_PROGRESS_SEQ", "10,25,45,65,85,95") or "").strip()
    out: list[int] = []
    for p in raw.split(","):
        p = p.strip()
        if not p:
            continue
        try:
            v = int(p)
        except Exception:
            continue
        if 1 <= v <= 99:
            out.append(v)
    return out or [10, 25, 45, 65, 85, 95]


async def handle_switchx_job(job: Dict[str, Any]) -> None:
    chat_id = int(job.get("chat_id") or 0)
    user_id = int(job.get("user_id") or 0)
    video_file_id = str(job.get("video_file_id") or "").strip()
    reference_file_id = str(job.get("reference_file_id") or "").strip()
    prompt = str(job.get("prompt") or "").strip()
    max_resolution = int(job.get("max_resolution") or 1080)
    alpha_mode = str(job.get("alpha_mode") or "auto").strip() or "auto"
    charge_tokens = int(job.get("charge_tokens") or 0)

    if not chat_id or not user_id:
        raise RuntimeError("switchx job missing chat_id/user_id")
    if not video_file_id:
        raise RuntimeError("switchx job missing video_file_id")
    if not reference_file_id:
        raise RuntimeError("switchx job missing reference_file_id")
    if not prompt:
        raise RuntimeError("switchx job missing prompt")
    if max_resolution not in (RESOLUTION_720, RESOLUTION_1080):
        raise RuntimeError(f"switchx invalid max_resolution: {max_resolution}")

    msg_id = await tg_send_message(chat_id, "⏳ SwitchX: готовлю файлы и отправляю задачу…")
    stop = asyncio.Event()

    async def _progress_loop() -> None:
        if not msg_id:
            return
        seq = _progress_seq()
        i = 0
        while not stop.is_set():
            pct = seq[min(i, len(seq) - 1)]
            i += 1
            try:
                await tg_edit_message_text(chat_id, msg_id, f"⏳ SwitchX: в очереди/генерация… {pct}%")
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(3.0, SWITCHX_POLL_SEC))
            except asyncio.TimeoutError:
                continue

    prog_task = asyncio.create_task(_progress_loop())

    try:
        video_bytes, video_ext = await tg_download_file_bytes(video_file_id)
        ref_bytes, ref_ext = await tg_download_file_bytes(reference_file_id)

        client = SwitchXClient()
        source_upload = await client.create_and_upload(
            filename=f"source_{uuid.uuid4().hex}.{video_ext or 'mp4'}",
            file_bytes=video_bytes,
            content_type=guess_content_type(f"x.{video_ext or 'mp4'}"),
        )
        ref_upload = await client.create_and_upload(
            filename=f"reference_{uuid.uuid4().hex}.{ref_ext or 'jpg'}",
            file_bytes=ref_bytes,
            content_type=guess_content_type(f"x.{ref_ext or 'jpg'}"),
        )

        created = await client.start_generation(
            source_uri=source_upload.beeble_uri,
            reference_image_uri=ref_upload.beeble_uri,
            prompt=prompt,
            alpha_mode=alpha_mode,
            max_resolution=max_resolution,
            idempotency_key=str(job.get("job_id") or uuid.uuid4().hex),
        )
        if not created.id:
            raise SwitchXError("SwitchX did not return job id")

        result = await client.wait_until_done(created.id, timeout_sec=SWITCHX_TIMEOUT_SEC, poll_sec=SWITCHX_POLL_SEC)

        stop.set()
        try:
            await prog_task
        except Exception:
            pass

        if result.status.lower().strip() == "failed":
            raise SwitchXError(result.error or "SwitchX generation failed")
        if not result.render_url:
            raise SwitchXError("SwitchX completed but render URL is missing")

        if msg_id:
            try:
                await tg_edit_message_text(chat_id, msg_id, "✅ SwitchX: готово. Отправляю видео…")
            except Exception:
                pass

        out_bytes = await http_get_bytes(result.render_url, timeout=300.0)
        await tg_send_video_bytes(
            chat_id,
            out_bytes,
            filename="switchx.mp4",
            caption=f"🎬 SwitchX готово • {max_resolution}p",
        )

    except Exception as e:
        stop.set()
        try:
            await prog_task
        except Exception:
            pass
        if charge_tokens > 0:
            try:
                add_tokens(user_id, int(charge_tokens), reason="switchx_video_refund", meta={"error": str(e)[:300]})
            except TypeError:
                try:
                    add_tokens(user_id, int(charge_tokens), reason="switchx_video_refund")
                except Exception:
                    pass
        await tg_send_message(chat_id, f"❌ SwitchX: ошибка генерации.\n{e}")


async def handle_job(job: Dict[str, Any]) -> None:
    job_type = str(job.get("type") or job.get("job_type") or "").strip()
    if job_type != JOB_TYPE_SWITCHX:
        return
    await handle_switchx_job(job)


async def worker_loop() -> None:
    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async def _run_one(job: Dict[str, Any]) -> None:
        async with sem:
            try:
                await handle_job(job)
            except Exception as e:
                print("SwitchX job failed:", e)

    while True:
        job = await dequeue_job(timeout_sec=10)
        if not job:
            continue
        if str(job.get("type") or job.get("job_type") or "").strip() != JOB_TYPE_SWITCHX:
            continue
        asyncio.create_task(_run_one(job))


def main() -> None:
    print("SwitchX worker started. concurrency =", MAX_CONCURRENCY)
    asyncio.run(worker_loop())


if __name__ == "__main__":
    main()
