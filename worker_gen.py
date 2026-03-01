import os
import asyncio
import json
from typing import Any, Dict, Optional

import httpx

from queue_redis import dequeue_job

# --- Telegram ---
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")  # must be set in Render env
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else None
TG_FILE = f"https://api.telegram.org/file/bot{BOT_TOKEN}" if BOT_TOKEN else None

# --- Concurrency inside ONE worker instance (scale instances on Render too) ---
MAX_CONCURRENCY = int(os.getenv("GEN_WORKER_CONCURRENCY", "5"))

# progress behavior (Telegram edits)
PROGRESS_STEP_SEC = float(os.getenv("PHOTOSESSION_PROGRESS_STEP_SEC", "3"))
PROGRESS_SEQUENCE = os.getenv("PHOTOSESSION_PROGRESS_SEQ", "10,25,45,65,85,95").strip()


async def tg_send_message(chat_id: int, text: str) -> Optional[int]:
    """Send a message and return Telegram message_id (or None)."""
    if not TG_API:
        print("TELEGRAM_BOT_TOKEN not set; cannot send message")
        return None
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(f"{TG_API}/sendMessage", json={"chat_id": chat_id, "text": text})
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
            json={"chat_id": chat_id, "message_id": int(message_id), "text": text},
        )


async def tg_send_photo_bytes(chat_id: int, photo_bytes: bytes, *, caption: Optional[str] = None) -> None:
    if not TG_API:
        print("TELEGRAM_BOT_TOKEN not set; cannot send photo")
        return
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
    files = {"photo": ("result.jpg", photo_bytes, "image/jpeg")}
    async with httpx.AsyncClient(timeout=60.0) as client:
        await client.post(f"{TG_API}/sendPhoto", data=data, files=files)


async def tg_get_file_path(file_id: str) -> Optional[str]:
    """Telegram getFile -> file_path"""
    if not TG_API:
        return None
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(f"{TG_API}/getFile", params={"file_id": file_id})
        r.raise_for_status()
        j = r.json()
        if not j.get("ok"):
            return None
        return (j.get("result") or {}).get("file_path")


async def tg_file_url_by_id(file_id: str) -> str:
    """Build a downloadable Telegram file URL from file_id (for ModelArk JSON mode)."""
    if not TG_FILE:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
    file_path = await tg_get_file_path(file_id)
    if not file_path:
        raise RuntimeError("Telegram getFile returned no file_path")
    return f"{TG_FILE}/{file_path}"


def _parse_progress_seq() -> list[int]:
    seq: list[int] = []
    for part in (PROGRESS_SEQUENCE or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            v = int(part)
            if 1 <= v <= 99:
                seq.append(v)
        except Exception:
            pass
    return seq or [10, 25, 45, 65, 85, 95]


async def handle_job(job: Dict[str, Any]) -> None:
    """
    Реальные типы job добавляем по одному. Сейчас делаем: type="photosession".
    Ожидаемые поля job:
      - job_id
      - type="photosession"
      - chat_id (int)
      - user_id (int)
      - photo_file_id (str)  # Telegram file_id исходного фото
      - prompt (str)
      - size (optional, default "1024x1024")
      - charge_ref_id (optional)  # для refund при ошибке
    """
    job_type = job.get("type") or job.get("job_type") or "unknown"
    chat_id = int(job.get("chat_id") or 0)
    user_id = int(job.get("user_id") or 0)

    print("JOB:", json.dumps(job, ensure_ascii=False))

    # --- PHOTOSESSION ---
    if job_type == "photosession":
        if not chat_id or not user_id:
            raise RuntimeError("photosession job missing chat_id/user_id")
        photo_file_id = (job.get("photo_file_id") or "").strip()
        prompt = (job.get("prompt") or "").strip()
        size = (job.get("size") or "1024x1024").strip()
        charge_ref_id = (job.get("charge_ref_id") or "").strip()

        if not photo_file_id:
            raise RuntimeError("photosession job missing photo_file_id")
        if not prompt:
            raise RuntimeError("photosession job missing prompt")

        # progress message (edits)
        msg_id = await tg_send_message(chat_id, "⏳ Нейро‑фотосессия: начинаю обработку…")

        stop = asyncio.Event()
        prog_task: Optional[asyncio.Task] = None

        async def _progress_loop() -> None:
            if not msg_id:
                return
            seq = _parse_progress_seq()
            i = 0
            while not stop.is_set():
                pct = seq[min(i, len(seq) - 1)]
                i += 1
                try:
                    await tg_edit_message_text(chat_id, msg_id, f"⏳ Нейро‑фотосессия: обработка… {pct}%")
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(stop.wait(), timeout=PROGRESS_STEP_SEC)
                except asyncio.TimeoutError:
                    continue

        prog_task = asyncio.create_task(_progress_loop())

        try:
            # ModelArk in your setup expects JSON body with image URLs.
            source_url = await tg_file_url_by_id(photo_file_id)

            # ark_edit_image is defined in main.py
            from main import ark_edit_image  # local import to keep startup light

            out_bytes = await ark_edit_image(
                source_image_bytes=b"",  # unused when source_image_url is provided
                prompt=prompt,
                size=size,
                source_image_url=source_url,
            )

            stop.set()
            if prog_task:
                try:
                    await prog_task
                except Exception:
                    pass

            if msg_id:
                try:
                    await tg_edit_message_text(chat_id, msg_id, "✅ Нейро‑фотосессия: готово.")
                except Exception:
                    pass

            await tg_send_photo_bytes(chat_id, out_bytes, caption="✅ Готово")
            return

        except Exception as e:
            err = str(e)[:800]
            print("photosession failed:", err)

            stop.set()
            if prog_task:
                try:
                    await prog_task
                except Exception:
                    pass

            if charge_ref_id:
                try:
                    from billing_db import refund_photosession_generation
                    refund_photosession_generation(user_id, ref_id=charge_ref_id, error=err)
                except Exception as re_err:
                    print("refund failed:", re_err)

            if msg_id:
                try:
                    await tg_edit_message_text(chat_id, msg_id, f"❌ Ошибка нейро‑фотосессии.\n{err}")
                    return
                except Exception:
                    pass
            await tg_send_message(chat_id, f"❌ Ошибка нейро‑фотосессии.\n{err}")
            return

    # --- Default (qtest etc.) ---
    if chat_id:
        await tg_send_message(chat_id, f"✅ Воркер получил задачу: {job_type}\njob_id={job.get('job_id')}")


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
