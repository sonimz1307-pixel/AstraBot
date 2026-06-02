import asyncio
import base64
import json
import os
from typing import Any, Dict, Optional

import httpx

from billing_db import add_tokens
from gpt_image_2_kie import handle_gpt_image_2_kie
from queue_redis import dequeue_job
from app.services.workspace_worker_jobs import process_workspace_image_job

WORKSPACE_IMAGE_QUEUE_NAME = (os.getenv("WORKSPACE_IMAGE_QUEUE_NAME", "workspace_image") or "workspace_image").strip() or "workspace_image"
WORKSPACE_IMAGE_CONCURRENCY = int(os.getenv("WORKSPACE_IMAGE_CONCURRENCY", "3") or "3")
image_sem = asyncio.Semaphore(WORKSPACE_IMAGE_CONCURRENCY)

TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else ""

MAIN_INTERNAL_URL = (os.getenv("MAIN_INTERNAL_URL") or "").strip().rstrip("/")
INTERNAL_API_KEY = (os.getenv("INTERNAL_API_KEY") or "").strip()
PROGRESS_STEP_SEC = float(os.getenv("GPT_IMAGE_2_PROGRESS_STEP_SEC", "4") or "4")
PROGRESS_SEQUENCE = (os.getenv("GPT_IMAGE_2_PROGRESS_SEQ") or "10,25,45,65,85,95").strip()


def _parse_progress_seq() -> list[int]:
    values: list[int] = []
    for raw in PROGRESS_SEQUENCE.split(","):
        try:
            value = int(str(raw).strip())
        except Exception:
            continue
        if 0 <= value <= 99:
            values.append(value)
    return values or [10, 25, 45, 65, 85, 95]


async def tg_send_message(chat_id: int, text: str, reply_markup: Optional[dict] = None) -> Optional[int]:
    if not TG_API:
        print("[workspace_image] TELEGRAM_BOT_TOKEN is not set; cannot send message", flush=True)
        return None
    payload: Dict[str, Any] = {"chat_id": int(chat_id), "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    async with httpx.AsyncClient(timeout=25.0) as client:
        r = await client.post(f"{TG_API}/sendMessage", json=payload)
    try:
        data = r.json()
        if isinstance(data, dict) and data.get("ok"):
            return int(((data.get("result") or {}) if isinstance(data.get("result"), dict) else {}).get("message_id") or 0) or None
    except Exception:
        pass
    return None


async def tg_edit_message_text(chat_id: int, message_id: int, text: str) -> None:
    if not TG_API or not message_id:
        return
    async with httpx.AsyncClient(timeout=25.0) as client:
        r = await client.post(
            f"{TG_API}/editMessageText",
            json={"chat_id": int(chat_id), "message_id": int(message_id), "text": text},
        )
        if r.status_code == 400:
            return


async def _tg_post_multipart(method: str, *, data: dict, files: dict, timeout: float = 90.0) -> dict:
    if not TG_API:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(f"{TG_API}/{method}", data=data, files=files)
    try:
        payload = r.json()
    except Exception:
        payload = {}
    if r.status_code >= 400 or not (isinstance(payload, dict) and payload.get("ok")):
        detail = payload.get("description") if isinstance(payload, dict) else None
        detail = detail or (r.text[:400] if getattr(r, "text", None) else "") or f"Telegram {method} failed with HTTP {r.status_code}"
        raise RuntimeError(detail)
    return payload


def _detect_image_ext_from_bytes(payload: bytes, fallback: str = "jpg") -> str:
    head = bytes(payload[:16] if payload else b"")
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return (str(fallback or "jpg").strip().lower().lstrip(".") or "jpg")


def _image_mime_type(ext: str) -> str:
    raw = str(ext or "jpg").strip().lower().lstrip(".") or "jpg"
    if raw == "jpeg":
        raw = "jpg"
    if raw == "png":
        return "image/png"
    if raw == "webp":
        return "image/webp"
    return "image/jpeg"


async def tg_send_document_bytes(
    chat_id: int,
    doc_bytes: bytes,
    *,
    filename: str = "file.jpg",
    caption: Optional[str] = None,
    reply_markup: Optional[dict] = None,
) -> Optional[int]:
    data: Dict[str, Any] = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    files = {"document": (filename, doc_bytes, "application/octet-stream")}
    payload = await _tg_post_multipart("sendDocument", data=data, files=files, timeout=120.0)
    return int(((payload.get("result") or {}) if isinstance(payload.get("result"), dict) else {}).get("message_id") or 0) or None


async def tg_send_photo_bytes(
    chat_id: int,
    photo_bytes: bytes,
    *,
    caption: Optional[str] = None,
    reply_markup: Optional[dict] = None,
    filename: Optional[str] = None,
    mime_type: Optional[str] = None,
) -> Optional[int]:
    ext = _detect_image_ext_from_bytes(photo_bytes, fallback="jpg")
    if ext == "jpeg":
        ext = "jpg"
    safe_filename = str(filename or f"result.{ext}").strip() or f"result.{ext}"
    safe_mime = str(mime_type or _image_mime_type(ext)).strip() or _image_mime_type(ext)

    if ext != "jpg" or len(photo_bytes or b"") > 9_500_000:
        return await tg_send_document_bytes(chat_id, photo_bytes, filename=safe_filename, caption=caption, reply_markup=reply_markup)

    data: Dict[str, Any] = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    files = {"photo": (safe_filename, photo_bytes, safe_mime)}
    try:
        payload = await _tg_post_multipart("sendPhoto", data=data, files=files, timeout=120.0)
        return int(((payload.get("result") or {}) if isinstance(payload.get("result"), dict) else {}).get("message_id") or 0) or None
    except Exception as exc:
        print(f"[workspace_image] sendPhoto failed, fallback to document: {exc}", flush=True)
        return await tg_send_document_bytes(chat_id, photo_bytes, filename=safe_filename, caption=caption, reply_markup=reply_markup)


async def register_dl2k_slot(chat_id: int, user_id: int, image_bytes: bytes) -> Optional[str]:
    if not MAIN_INTERNAL_URL or not image_bytes:
        return None
    headers: Dict[str, str] = {}
    if INTERNAL_API_KEY:
        headers["x-internal-key"] = INTERNAL_API_KEY
    payload = {
        "chat_id": int(chat_id),
        "user_id": int(user_id),
        "bytes_b64": base64.b64encode(image_bytes).decode("ascii"),
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(f"{MAIN_INTERNAL_URL}/internal/dl2k", json=payload, headers=headers)
        if r.status_code != 200:
            return None
        data = r.json()
        if isinstance(data, dict) and data.get("ok") and data.get("token"):
            return str(data["token"])
    except Exception as exc:
        print(f"[workspace_image] register_dl2k_slot failed: {exc}", flush=True)
    return None


def _download_keyboard(token: Optional[str], resolution: str) -> Optional[dict]:
    if not token:
        return None
    label = "⬇️ Скачать оригинал 4К" if str(resolution).upper() == "4K" else "⬇️ Скачать оригинал 2К"
    return {"inline_keyboard": [[{"text": label, "callback_data": f"dl2k:{token}"}]]}


async def process_telegram_gpt_image_2_kie_job(job: Dict[str, Any]) -> None:
    chat_id = int(job.get("chat_id") or 0)
    user_id = int(job.get("user_id") or 0)
    prompt = str(job.get("prompt") or "").strip()
    mode = str(job.get("mode") or "text_to_image").strip().lower()
    resolution = str(job.get("resolution") or "2K").strip().upper() or "2K"
    aspect_ratio = str(job.get("aspect_ratio") or "auto").strip() or "auto"
    photo_file_ids = [str(item or "").strip() for item in (job.get("photo_file_ids") or []) if str(item or "").strip()]
    photo_urls = [str(item or "").strip() for item in (job.get("photo_urls") or []) if str(item or "").strip()]
    charge_tokens = int(job.get("charge_tokens") or 0)
    charge_ref_id = str(job.get("charge_ref_id") or "").strip() or None
    refund_reason = str(job.get("refund_reason") or "gpt_image_2_refund").strip() or "gpt_image_2_refund"

    status_msg_id = await tg_send_message(chat_id, f"⏳ Gpt Image 2: задача в обработке…\nКачество: {resolution}\nФормат: {aspect_ratio}")
    stop = asyncio.Event()

    async def _progress_loop() -> None:
        if not status_msg_id:
            return
        seq = _parse_progress_seq()
        i = 0
        while not stop.is_set():
            pct = seq[min(i, len(seq) - 1)]
            i += 1
            try:
                await tg_edit_message_text(chat_id, status_msg_id, f"⏳ Gpt Image 2: обработка… {pct}%\nКачество: {resolution}\nФормат: {aspect_ratio}")
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=PROGRESS_STEP_SEC)
            except asyncio.TimeoutError:
                continue

    progress_task = asyncio.create_task(_progress_loop())

    try:
        out_bytes, ext = await handle_gpt_image_2_kie(
            prompt,
            mode=mode,
            source_image_urls=photo_urls[:16],
            telegram_file_ids=photo_file_ids[:16],
            resolution=resolution,
            aspect_ratio=aspect_ratio,
        )
        stop.set()
        try:
            await progress_task
        except Exception:
            pass
        if status_msg_id:
            try:
                await tg_edit_message_text(chat_id, status_msg_id, "✅ Gpt Image 2: готово. Отправляю файл…")
            except Exception:
                pass

        token = await register_dl2k_slot(chat_id, user_id, out_bytes)
        reply_markup = _download_keyboard(token, resolution)
        await tg_send_photo_bytes(
            chat_id,
            out_bytes,
            caption=f"✅ Готово (Gpt Image 2 • {resolution})",
            reply_markup=reply_markup,
            filename=f"gpt_image_2.{ext or 'jpg'}",
            mime_type=_image_mime_type(ext or "jpg"),
        )
        if status_msg_id:
            try:
                await tg_edit_message_text(chat_id, status_msg_id, "✅ Gpt Image 2: готово")
            except Exception:
                pass
        return
    except Exception as exc:
        err = str(exc)[:800]
        print(f"[workspace_image] Gpt Image 2 job failed job={job.get('job_id')}: {err}", flush=True)
        stop.set()
        try:
            await progress_task
        except Exception:
            pass
        refund_status = ""
        if charge_tokens and charge_ref_id and user_id:
            try:
                add_tokens(user_id, int(charge_tokens), reason=refund_reason, ref_id=charge_ref_id, meta={"stage": "worker_failed", "provider": "gpt_image_2_kie", "error": err})
                refund_status = "Токены возвращены."
            except Exception as refund_exc:
                print(f"[workspace_image] Gpt Image 2 refund failed: {refund_exc}", flush=True)
                refund_status = "Автовозврат токенов не удалось подтвердить. Если баланс не восстановится автоматически, напиши в поддержку."
        text = f"❌ Gpt Image 2: ошибка генерации." + (f"\n{refund_status}" if refund_status else "") + f"\n{err}"
        if status_msg_id:
            try:
                await tg_edit_message_text(chat_id, status_msg_id, text)
                return
            except Exception:
                pass
        await tg_send_message(chat_id, text)


async def _handle(job: Dict[str, Any]) -> None:
    kind = str(job.get("kind") or "").strip().lower()
    async with image_sem:
        if kind == "workspace_image_run":
            await process_workspace_image_job(job)
            print(f"[workspace_image] completed workspace image job={job.get('job_id')}", flush=True)
            return
        if kind == "telegram_gpt_image_2_kie_run":
            await process_telegram_gpt_image_2_kie_job(job)
            print(f"[workspace_image] completed telegram Gpt Image 2 job={job.get('job_id')}", flush=True)
            return
        print(f"[workspace_image] skipped unsupported kind={kind} job={job.get('job_id')}", flush=True)


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
