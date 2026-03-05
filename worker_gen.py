import os
import asyncio
import json
import base64
import uuid
import time
import mimetypes
from typing import Any, Dict, Optional

import httpx

from queue_redis import dequeue_job

from nano_banana_pro import handle_nano_banana_pro
from billing_db import add_tokens

# --- Telegram ---
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")  # must be set in Render env
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else None
TG_FILE = f"https://api.telegram.org/file/bot{BOT_TOKEN}" if BOT_TOKEN else None

# --- Main service (for download button "Скачать оригинал 2К") ---
MAIN_INTERNAL_URL = os.getenv("MAIN_INTERNAL_URL", "").strip().rstrip("/")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "").strip()

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



async def register_dl2k_slot(chat_id: int, user_id: int, image_bytes: bytes) -> Optional[str]:
    """
    Ask main service to register a temporary download slot for inline callback button.
    Returns token for callback_data "dl2k:<token>".
    """
    if not MAIN_INTERNAL_URL:
        return None
    if not image_bytes:
        return None

    headers = {}
    if INTERNAL_API_KEY:
        headers["x-internal-key"] = INTERNAL_API_KEY

    payload = {
        "chat_id": int(chat_id),
        "user_id": int(user_id),
        "bytes_b64": base64.b64encode(image_bytes).decode("ascii"),
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(f"{MAIN_INTERNAL_URL}/internal/dl2k", json=payload, headers=headers)
            if r.status_code != 200:
                return None
            j = r.json()
            if j.get("ok") and j.get("token"):
                return str(j["token"])
    except Exception:
        return None
    return None

async def tg_send_photo_bytes(chat_id: int, photo_bytes: bytes, *, caption: Optional[str] = None, reply_markup: Optional[dict] = None) -> None:
    if not TG_API:
        print("TELEGRAM_BOT_TOKEN not set; cannot send photo")
        return
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    files = {"photo": ("result.jpg", photo_bytes, "image/jpeg")}
    async with httpx.AsyncClient(timeout=60.0) as client:
        await client.post(f"{TG_API}/sendPhoto", data=data, files=files)

async def tg_send_document_bytes(chat_id: int, doc_bytes: bytes, *, filename: str = "file.jpg", caption: Optional[str] = None, reply_markup: Optional[dict] = None) -> None:
    """Send a document (keeps original quality, unlike photo)."""
    if not TG_API:
        print("TELEGRAM_BOT_TOKEN not set; cannot send document")
        return
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    files = {"document": (filename, doc_bytes)}
    async with httpx.AsyncClient(timeout=60.0) as client:
        await client.post(f"{TG_API}/sendDocument", data=data, files=files)



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

# --- Supabase Storage (for PUBLIC reference URLs, so PiAPI can fetch reliably) ---
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
SEEDANCE_REF_BUCKET = os.getenv("SEEDANCE_REF_BUCKET", "seedance-refs").strip() or "seedance-refs"

def _sb_public_url(bucket: str, object_path: str) -> str:
    return f"{SUPABASE_URL}/storage/v1/object/public/{bucket}/{object_path}"

async def sb_upload_public_bytes(bucket: str, object_path: str, data: bytes, *, content_type: str = "application/octet-stream") -> str:
    """Upload bytes to Supabase Storage and return PUBLIC URL.
    Bucket must be PUBLIC or have a public policy for object reads.
    Uses service key (recommended on server-side only).
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise RuntimeError("Supabase Storage not configured (SUPABASE_URL/SUPABASE_SERVICE_KEY)")
    url = f"{SUPABASE_URL}/storage/v1/object/{bucket}/{object_path}"
    headers = {
        "authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "apikey": SUPABASE_SERVICE_KEY,
        "content-type": content_type,
        "x-upsert": "true",
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(url, headers=headers, content=data)
        if r.status_code >= 300:
            raise RuntimeError(f"Supabase upload failed: {r.status_code} {r.text[:300]}")
    return _sb_public_url(bucket, object_path)

async def tg_download_file_bytes(file_id: str) -> tuple[bytes, str]:
    """Download Telegram file by file_id and return (bytes, content_type)."""
    file_url = await tg_file_url_by_id(file_id)
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        r = await client.get(file_url)
        r.raise_for_status()
        ct = r.headers.get("content-type") or "application/octet-stream"
        return (r.content, ct)

async def seedance_ref_public_url_from_tg_file_id(file_id: str) -> str:
    """Make a PUBLIC URL for Seedance reference image from a Telegram file_id.

    Preferred: upload to Supabase public storage (stable, no bot token in URL).
    Fallback: Telegram direct URL (can be ignored by PiAPI in some networks).
    """
    # Try Supabase
    try:
        b, ct = await tg_download_file_bytes(file_id)
        ext = mimetypes.guess_extension(ct.split(";")[0].strip()) or ".jpg"
        obj = f"tg_refs/{int(time.time())}_{uuid.uuid4().hex}{ext}"
        return await sb_upload_public_bytes(SEEDANCE_REF_BUCKET, obj, b, content_type=ct)
    except Exception as e:
        print("Seedance: Supabase ref upload failed, fallback to Telegram URL:", e)
        return await tg_file_url_by_id(file_id)



# --- PiAPI Seedance 2.0 (Text/Image -> Video) ---
PIAPI_BASE_URL = os.getenv("PIAPI_BASE_URL", "https://api.piapi.ai").strip().rstrip("/")
PIAPI_API_KEY = os.getenv("PIAPI_API_KEY", "").strip()

SEEDANCE_TIMEOUT_SEC = int(os.getenv("SEEDANCE_TIMEOUT_SEC", "7200"))  # up to 2h (queue may be long)
SEEDANCE_POLL_SEC = float(os.getenv("SEEDANCE_POLL_SEC", "6"))


async def _piapi_seedance_create_task(*, task_type: str, prompt: Optional[str] = None,
                                     duration: Optional[int] = None,
                                     aspect_ratio: Optional[str] = None,
                                     image_urls: Optional[list[str]] = None,
                                     parent_task_id: Optional[str] = None,
                                     service_mode: str = "public") -> dict:
    if not PIAPI_API_KEY:
        raise RuntimeError("PIAPI_API_KEY not set")
    if not PIAPI_BASE_URL:
        raise RuntimeError("PIAPI_BASE_URL not set")
    body: dict = {"model": "seedance", "task_type": task_type, "input": {}}
    if prompt is not None:
        body["input"]["prompt"] = prompt
    if duration is not None:
        body["input"]["duration"] = int(duration)
    if aspect_ratio is not None:
        body["input"]["aspect_ratio"] = aspect_ratio
    if image_urls:
        body["input"]["image_urls"] = image_urls
    if parent_task_id:
        body["input"]["parent_task_id"] = parent_task_id
    body["config"] = {"service_mode": service_mode}
    headers = {"X-API-Key": PIAPI_API_KEY}
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{PIAPI_BASE_URL}/api/v1/task", headers=headers, json=body)
        try:
            j = r.json()
        except Exception:
            j = {}
        if r.status_code >= 300:
            raise RuntimeError(f"PiAPI seedance create failed: {r.status_code} {r.text[:600]}")
        if not isinstance(j, dict):
            raise RuntimeError("PiAPI seedance create: bad JSON")
        return j


async def _piapi_seedance_get_task(task_id: str) -> dict:
    if not PIAPI_API_KEY:
        raise RuntimeError("PIAPI_API_KEY not set")
    headers = {"X-API-Key": PIAPI_API_KEY}
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(f"{PIAPI_BASE_URL}/api/v1/task/{task_id}", headers=headers)
        try:
            j = r.json()
        except Exception:
            j = {}
        if r.status_code >= 300:
            raise RuntimeError(f"PiAPI seedance get failed: {r.status_code} {r.text[:600]}")
        if not isinstance(j, dict):
            raise RuntimeError("PiAPI seedance get: bad JSON")
        return j


def _seedance_status_lower(resp: dict) -> str:
    """Seedance task status, supports both {data:{...}} and flat payload."""
    if not isinstance(resp, dict):
        return ""
    d = resp.get("data") if isinstance(resp.get("data"), dict) else resp
    return str((d or {}).get("status") or "").lower().strip()


def _first_http_url(*vals: Any) -> Optional[str]:
    for v in vals:
        if isinstance(v, str) and v.startswith("http"):
            return v
    return None


def _seedance_extract_output_url(resp: dict) -> Optional[str]:
    """Extract resulting video URL from PiAPI Seedance task response.

    Supports both formats:
    1) {"data": {"output": {"video": "https://..."}}}
    2) {"output": {"video": "https://..."}}   (flat)
    """
    if not isinstance(resp, dict):
        return None
    d = resp.get("data") if isinstance(resp.get("data"), dict) else resp
    out = (d.get("output") or {}) if isinstance(d, dict) else {}
    if isinstance(out, dict):
        u = _first_http_url(
            out.get("video"),  # IMPORTANT: PiAPI returns output.video
            out.get("video_url"), out.get("videoUrl"),
            out.get("url"), out.get("mp4_url"), out.get("mp4Url"),
            out.get("file_url"), out.get("fileUrl"),
            out.get("image_url"),  # fallback (some gateways misuse this field)
        )
        if u:
            return u
        urls = out.get("video_urls") or out.get("videoUrls") or out.get("image_urls") or out.get("imageUrls")
        if isinstance(urls, list):
            for x in urls:
                u2 = _first_http_url(x)
                if u2:
                    return u2
    return None


async def _piapi_seedance_wait(task_id: str, *, timeout_s: int, poll_s: float) -> dict:
    t0 = asyncio.get_event_loop().time()
    last = None
    while True:
        last = await _piapi_seedance_get_task(task_id)
        st = _seedance_status_lower(last)
        if st in ("completed", "failed"):
            return last
        if (asyncio.get_event_loop().time() - t0) > float(timeout_s):
            raise TimeoutError("Seedance: timeout while waiting")
        await asyncio.sleep(poll_s)


async def tg_send_video_bytes(chat_id: int, video_bytes: bytes, *, filename: str = "video.mp4", caption: str = "") -> None:
    """Send video as multipart bytes (most reliable)."""
    if not TG_API:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
    files = {"video": (filename, video_bytes, "video/mp4")}
    async with httpx.AsyncClient(timeout=180.0) as client:
        r = await client.post(f"{TG_API}/sendVideo", data=data, files=files)
        j = {}
        try:
            j = r.json()
        except Exception:
            pass
        if (r.status_code >= 300) or (isinstance(j, dict) and not j.get("ok", True)):
            raise RuntimeError(f"Telegram sendVideo(bytes) failed: {r.status_code} {str(j)[:300]}")

async def http_download_bytes(url: str, *, timeout: float = 180.0) -> bytes:
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.content

async def tg_send_video_from_url(chat_id: int, video_url: str, caption: str = "") -> None:
    """Try to send video by URL; if Telegram rejects, fallback to bytes upload."""
    if not TG_API:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{TG_API}/sendVideo", json={"chat_id": chat_id, "video": video_url, "caption": caption})
        j = {}
        try:
            j = r.json()
        except Exception:
            pass
        # Telegram can return HTTP 200 but ok=false
        if (r.status_code >= 300) or (isinstance(j, dict) and not j.get("ok", True)):
            # fallback: download and upload bytes
            vb = await http_download_bytes(video_url, timeout=240.0)
            await tg_send_video_bytes(chat_id, vb, filename="seedance.mp4", caption=caption)



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

            token = await register_dl2k_slot(chat_id, user_id, out_bytes)

            reply_markup = None
            if token:
                reply_markup = {
                    "inline_keyboard": [[
                        {"text": "⬇️ Скачать оригинал 2К", "callback_data": f"dl2k:{token}"}
                    ]]
                }

            await tg_send_photo_bytes(chat_id, out_bytes, caption="✅ Готово", reply_markup=reply_markup)
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
    # --- NANO BANANA PRO (queue worker) ---
    elif job_type == "nano_banana_pro":
        prompt = str(job.get("prompt") or "").strip()
        photo_file_id = str(job.get("photo_file_id") or "").strip()
        resolution = str(job.get("resolution") or "2K").strip()
        output_format = str(job.get("output_format") or "jpg").strip()
        aspect_ratio = str(job.get("aspect_ratio") or "").strip() or None
        safety_level = str(job.get("safety_level") or "high").strip()
        cost = int(job.get("cost") or 2)

        if not chat_id or not user_id:
            raise RuntimeError("nano_banana_pro job missing chat_id/user_id")
        if not prompt:
            raise RuntimeError("nano_banana_pro job missing prompt")

        # progress message (edits)
        msg_id = await tg_send_message(chat_id, "⏳ Nano Banana Pro: начинаю обработку…")

        stop = asyncio.Event()

        async def _progress_loop_nano() -> None:
            if not msg_id:
                return
            seq = _parse_progress_seq()
            i = 0
            while not stop.is_set():
                pct = seq[min(i, len(seq) - 1)]
                i += 1
                try:
                    await tg_edit_message_text(chat_id, msg_id, f"⏳ Nano Banana Pro: обработка… {pct}%")
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(stop.wait(), timeout=PROGRESS_STEP_SEC)
                except asyncio.TimeoutError:
                    continue

        prog_task = asyncio.create_task(_progress_loop_nano())

        try:
            # If photo_file_id is present -> Image→Image. Else -> Text→Image.
            if photo_file_id:
                out_bytes, ext = await handle_nano_banana_pro(
                    b"x",  # non-empty -> i2i branch
                    prompt,
                    resolution=resolution,
                    output_format=output_format,
                    aspect_ratio=aspect_ratio,
                    safety_level=safety_level,
                    telegram_file_id=photo_file_id,
                )
            else:
                out_bytes, ext = await handle_nano_banana_pro(
                    None,
                    prompt,
                    resolution=resolution,
                    output_format=output_format,
                    aspect_ratio=aspect_ratio,
                    safety_level=safety_level,
                )

            stop.set()
            try:
                await prog_task
            except Exception:
                pass

            if msg_id:
                try:
                    await tg_edit_message_text(chat_id, msg_id, "✅ Nano Banana Pro: готово. Отправляю файл…")
                except Exception:
                    pass

            # Register download slot in main to keep old UX: photo + inline button "Скачать оригинал 2К"
            token = await register_dl2k_slot(chat_id, user_id, out_bytes)
            reply_markup = None
            if token:
                reply_markup = {"inline_keyboard": [[{"text": "⬇️ Скачать оригинал 2К", "callback_data": f"dl2k:{token}"}]]}

            if msg_id:
                try:
                    await tg_edit_message_text(chat_id, msg_id, "✅ Nano Banana Pro: готово")
                except Exception:
                    pass

            await tg_send_photo_bytes(chat_id, out_bytes, caption="🍌 Nano Banana Pro — готово", reply_markup=reply_markup)
            return

        except Exception as e:
            err = str(e)[:800]
            print("nano_banana_pro failed:", err)

            stop.set()
            try:
                await prog_task
            except Exception:
                pass

            # refund: return tokens back (simple add_tokens)
            try:
                add_tokens(user_id, cost, reason="nano_banana_pro_refund")
            except Exception:
                pass

            if msg_id:
                try:
                    await tg_edit_message_text(chat_id, msg_id, f"❌ Ошибка Nano Banana Pro.\n{err}")
                except Exception:
                    pass
            await tg_send_message(chat_id, f"❌ Ошибка Nano Banana Pro.\n{err}")
            return


# --- SEEDANCE 2 (PiAPI) --- 
    elif job_type == "seedance_video":
        prompt = str(job.get("prompt") or "").strip()
        task_type = str(job.get("task_type") or "seedance-2-preview").strip()
        duration = int(job.get("duration") or 5)
        aspect_ratio = str(job.get("aspect_ratio") or "16:9").strip()
        image_file_ids = job.get("image_file_ids") or []
        parent_task_id = str(job.get("parent_task_id") or "").strip() or None
        service_mode = str(job.get("service_mode") or "public").strip() or "public"

        charge_tokens = int(job.get("charge_tokens") or 0)

        if not chat_id or not user_id:
            raise RuntimeError("seedance_video job missing chat_id/user_id")
        if not parent_task_id and not prompt:
            raise RuntimeError("seedance_video job missing prompt")

        msg_id = await tg_send_message(chat_id, "⏳ Seedance: отправляю задачу…")

        stop = asyncio.Event()

        async def _progress_loop_seedance() -> None:
            if not msg_id:
                return
            seq = _parse_progress_seq()
            i = 0
            while not stop.is_set():
                pct = seq[min(i, len(seq) - 1)]
                i += 1
                try:
                    await tg_edit_message_text(chat_id, msg_id, f"⏳ Seedance: в очереди/генерация… {pct}%")
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(stop.wait(), timeout=max(2.0, SEEDANCE_POLL_SEC))
                except asyncio.TimeoutError:
                    continue

        prog_task = asyncio.create_task(_progress_loop_seedance())

        try:
            # Convert reference images to PUBLIC URLs (preferred: Supabase public; fallback: Telegram file URL)
            image_urls: Optional[list[str]] = None
            if isinstance(image_file_ids, list) and image_file_ids:
                image_urls = []
                for fid in image_file_ids[:9]:
                    if not isinstance(fid, str) or not fid.strip():
                        continue
                    try:
                        u = await seedance_ref_public_url_from_tg_file_id(fid.strip())
                        image_urls.append(u)
                    except Exception as e:
                        print("Seedance: failed to build public ref url:", e)

            created = await _piapi_seedance_create_task(
                task_type=task_type,
                prompt=prompt if not parent_task_id else (job.get("prompt") if job.get("prompt") is not None else None),
                duration=duration if not parent_task_id else (job.get("duration") if job.get("duration") is not None else None),
                aspect_ratio=aspect_ratio if not parent_task_id else (job.get("aspect_ratio") if job.get("aspect_ratio") is not None else None),
                image_urls=image_urls,
                parent_task_id=parent_task_id,
                service_mode=service_mode,
            )

            task_id = (((created.get("data") or {}) if isinstance(created.get("data"), dict) else created) or {}).get("task_id") if isinstance(created, dict) else None
            if not task_id:
                raise RuntimeError(f"Seedance: PiAPI didn't return task_id: {json.dumps(created, ensure_ascii=False)[:800]}")

            done = await _piapi_seedance_wait(task_id, timeout_s=SEEDANCE_TIMEOUT_SEC, poll_s=SEEDANCE_POLL_SEC)

            stop.set()
            try:
                await prog_task
            except Exception:
                pass

            st = _seedance_status_lower(done)
            if st == "failed":
                d_done = (done.get("data") if isinstance(done.get("data"), dict) else done) if isinstance(done, dict) else {}
                err = ((d_done or {}).get("error") or {}) if isinstance(d_done, dict) else {}
                msg = ""
                if isinstance(err, dict):
                    msg = str(err.get("message") or "")
                raise RuntimeError(msg or "Seedance task failed")

            url = _seedance_extract_output_url(done)
            if not url:
                raise RuntimeError("Seedance completed but no output url")

            if msg_id:
                try:
                    await tg_edit_message_text(chat_id, msg_id, "✅ Seedance: готово. Отправляю видео…")
                except Exception:
                    pass

            try:
                await tg_send_video_from_url(chat_id, url, caption="🎬 Seedance видео")
            except Exception:
                await tg_send_message(chat_id, f"✅ Seedance готово!\n🎬 {url}")

            return

        except Exception as e:
            stop.set()
            try:
                await prog_task
            except Exception:
                pass

            # refund on failure if charged
            if charge_tokens > 0:
                try:
                    add_tokens(user_id, int(charge_tokens), reason="seedance_video_refund", meta={"error": str(e)[:300]})
                except TypeError:
                    try:
                        add_tokens(user_id, int(charge_tokens), reason="seedance_video_refund")
                    except Exception:
                        pass

            try:
                await tg_send_message(chat_id, f"❌ Seedance: ошибка генерации.\n{e}")
            except Exception:
                pass
            return

    # --- Default (qtest etc.) ---
    else:
        if chat_id:
            await tg_send_message(chat_id, f"✅ Воркер получил задачу: {job_type}\njob_id={job.get('job_id')}")
        return




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
