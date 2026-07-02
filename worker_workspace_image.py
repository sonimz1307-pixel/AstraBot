import asyncio
import base64
import json
import os
import time
from io import BytesIO
from uuid import uuid4
from typing import Any, Dict, List, Optional, Tuple

import httpx

from billing_db import add_tokens
from gpt_image_2_kie import handle_gpt_image_2_kie
from nano_banana_2_lite_kie import handle_nano_banana_2_lite
from kling_flow import upload_bytes_to_supabase
from queue_redis import dequeue_job
from app.services.legnext_midjourney import (
    LegnextMidjourneyError,
    create_midjourney_diffusion,
    create_midjourney_reroll,
    create_midjourney_variation,
    get_midjourney_job,
)
from app.services.workspace_worker_jobs import process_workspace_image_job

WORKSPACE_IMAGE_QUEUE_NAME = (os.getenv("WORKSPACE_IMAGE_QUEUE_NAME", "workspace_image") or "workspace_image").strip() or "workspace_image"
WORKSPACE_IMAGE_CONCURRENCY = int(os.getenv("WORKSPACE_IMAGE_CONCURRENCY", "3") or "3")
image_sem = asyncio.Semaphore(WORKSPACE_IMAGE_CONCURRENCY)

WORKSPACE_NB2LITE_QUEUE_NAME = (os.getenv("WORKSPACE_NB2LITE_QUEUE_NAME", "workspace_nb2lite") or "workspace_nb2lite").strip() or "workspace_nb2lite"
WORKSPACE_NB2LITE_CONCURRENCY = int(os.getenv("WORKSPACE_NB2LITE_CONCURRENCY", "2") or "2")
nb2lite_sem = asyncio.Semaphore(WORKSPACE_NB2LITE_CONCURRENCY)

MIDJOURNEY_TG_QUEUE_NAME = (os.getenv("MIDJOURNEY_TG_QUEUE_NAME", "telegram_midjourney") or "telegram_midjourney").strip() or "telegram_midjourney"
MIDJOURNEY_TG_CONCURRENCY = int(os.getenv("MIDJOURNEY_TG_CONCURRENCY", "1") or "1")
midjourney_tg_sem = asyncio.Semaphore(MIDJOURNEY_TG_CONCURRENCY)

TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else ""

MAIN_INTERNAL_URL = (os.getenv("MAIN_INTERNAL_URL") or "").strip().rstrip("/")
INTERNAL_API_KEY = (os.getenv("INTERNAL_API_KEY") or "").strip()
PROGRESS_STEP_SEC = float(os.getenv("GPT_IMAGE_2_PROGRESS_STEP_SEC", "4") or "4")
PROGRESS_SEQUENCE = (os.getenv("GPT_IMAGE_2_PROGRESS_SEQ") or "10,25,45,65,85,95").strip()
MIDJOURNEY_PROGRESS_STEP_SEC = float(os.getenv("MIDJOURNEY_TG_PROGRESS_STEP_SEC", "7") or "7")


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


def _upload_midjourney_tg_asset(*, user_id: int, payload: bytes, ext: str = "jpg", prefix: str = "result") -> str:
    if not payload:
        raise RuntimeError("empty Midjourney asset")
    safe_ext = str(ext or "jpg").strip().lower().lstrip(".") or "jpg"
    path = f"midjourney_tg/{int(user_id)}/{prefix}_{int(time.time())}_{uuid4().hex[:12]}.{safe_ext}"
    return str(upload_bytes_to_supabase(path, payload, _image_mime_type(safe_ext)) or "").strip()


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
    res = str(resolution or "").strip().upper()
    if res == "4K":
        label = "⬇️ Скачать оригинал 4К"
    elif res == "1K":
        label = "⬇️ Скачать оригинал 1К"
    else:
        label = "⬇️ Скачать оригинал 2К"
    return {"inline_keyboard": [[{"text": label, "callback_data": f"dl2k:{token}"}]]}


async def _download_bytes(url: str, *, timeout: float = 180.0) -> bytes:
    safe_url = str(url or "").strip()
    if not safe_url:
        raise RuntimeError("empty download url")
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        r = await client.get(safe_url)
        r.raise_for_status()
        return r.content


def _safe_public_provider_url(raw: Any) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if not (value.startswith("http://") or value.startswith("https://")):
        return ""
    # Never pass Telegram bot-file URLs to external providers: they contain TELEGRAM_BOT_TOKEN.
    if "/file/bot" in value:
        return ""
    return value


async def _telegram_file_path(file_id: str) -> str:
    clean_file_id = str(file_id or "").strip()
    if not clean_file_id:
        raise RuntimeError("empty Telegram file_id")
    if not TG_API or not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{TG_API}/getFile", params={"file_id": clean_file_id})
    try:
        payload = r.json()
    except Exception:
        payload = {}
    if r.status_code >= 400 or not (isinstance(payload, dict) and payload.get("ok")):
        detail = payload.get("description") if isinstance(payload, dict) else None
        detail = detail or (r.text[:300] if getattr(r, "text", None) else "") or f"HTTP {r.status_code}"
        raise RuntimeError(f"Telegram getFile failed: {detail}")
    file_path = str(((payload.get("result") or {}) if isinstance(payload, dict) else {}).get("file_path") or "").strip()
    if not file_path:
        raise RuntimeError("Telegram getFile returned empty file_path")
    return file_path


async def _download_telegram_file_bytes(file_id: str) -> bytes:
    file_path = await _telegram_file_path(file_id)
    # This tokenized URL is used only internally for download, never sent to KIE.
    url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    return await _download_bytes(url, timeout=180.0)


def _upload_nano_banana_2_lite_tg_ref(*, user_id: int, payload: bytes, idx: int) -> str:
    if not payload:
        raise RuntimeError("empty Nano Banana 2 Lite reference image")
    ext = _detect_image_ext_from_bytes(payload, fallback="jpg")
    if ext == "jpeg":
        ext = "jpg"
    path = f"workspace_refs/{int(user_id)}/nano_banana_2_lite/{int(time.time())}_{uuid4().hex[:12]}_ref_{idx}.{ext}"
    return str(upload_bytes_to_supabase(path, payload, _image_mime_type(ext)) or "").strip()


async def _prepare_nano_banana_2_lite_reference_urls(
    *,
    user_id: int,
    photo_urls: List[str],
    photo_file_ids: List[str],
    max_refs: int = 10,
) -> List[str]:
    urls: List[str] = []
    seen = set()

    def _add_url(raw: Any) -> None:
        value = _safe_public_provider_url(raw)
        if not value or value in seen or len(urls) >= max_refs:
            return
        seen.add(value)
        urls.append(value)

    for raw in photo_urls or []:
        _add_url(raw)

    for idx, file_id in enumerate(photo_file_ids or [], start=1):
        if len(urls) >= max_refs:
            break
        clean_file_id = str(file_id or "").strip()
        if not clean_file_id:
            continue
        payload = await _download_telegram_file_bytes(clean_file_id)
        _add_url(_upload_nano_banana_2_lite_tg_ref(user_id=user_id, payload=payload, idx=idx))

    return urls[:max_refs]


async def _wait_for_midjourney_job(job_id: str, *, poll_interval_sec: float = 3.0, timeout_sec: float = 900.0) -> Dict[str, Any]:
    loop = asyncio.get_running_loop()
    started = loop.time()
    while True:
        payload = await get_midjourney_job(job_id)
        status = str(payload.get("status") or "").strip().lower()
        if status in {"completed", "failed"}:
            return payload
        if (loop.time() - started) >= timeout_sec:
            raise LegnextMidjourneyError(f"Midjourney polling timeout for job {job_id}")
        await asyncio.sleep(poll_interval_sec)


def _make_midjourney_grid(image_items: List[Tuple[bytes, str]]) -> bytes:
    """Create a 2x2 preview grid for the Telegram gallery."""
    from PIL import Image, ImageDraw  # type: ignore

    if not image_items:
        raise RuntimeError("no images for grid")

    thumbs = []
    cell = 768
    gap = 10
    bg = (18, 18, 18)

    for idx, (payload, _ext) in enumerate(image_items[:4], start=1):
        img = Image.open(BytesIO(payload)).convert("RGB")
        img.thumbnail((cell, cell), Image.LANCZOS)
        canvas = Image.new("RGB", (cell, cell), bg)
        x = (cell - img.width) // 2
        y = (cell - img.height) // 2
        canvas.paste(img, (x, y))
        draw = ImageDraw.Draw(canvas)
        badge = f"#{idx}"
        draw.rounded_rectangle((16, 16, 92, 62), radius=12, fill=(0, 0, 0))
        draw.text((30, 27), badge, fill=(255, 255, 255))
        thumbs.append(canvas)

    while len(thumbs) < 4:
        thumbs.append(Image.new("RGB", (cell, cell), bg))

    w = cell * 2 + gap
    h = cell * 2 + gap
    grid = Image.new("RGB", (w, h), bg)
    positions = [(0, 0), (cell + gap, 0), (0, cell + gap), (cell + gap, cell + gap)]
    for img, pos in zip(thumbs[:4], positions):
        grid.paste(img, pos)

    out = BytesIO()
    grid.save(out, format="JPEG", quality=92, optimize=True)
    return out.getvalue()


def _midjourney_grid_keyboard(session_token: str) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "1️⃣ Открыть #1", "callback_data": f"mjr:{session_token}:open:0"},
                {"text": "2️⃣ Открыть #2", "callback_data": f"mjr:{session_token}:open:1"},
            ],
            [
                {"text": "3️⃣ Открыть #3", "callback_data": f"mjr:{session_token}:open:2"},
                {"text": "4️⃣ Открыть #4", "callback_data": f"mjr:{session_token}:open:3"},
            ],
            [
                {"text": "🔄 Reroll все 4", "callback_data": f"mjr:{session_token}:reroll"},
                {"text": "📄 Prompt", "callback_data": f"mjr:{session_token}:prompt"},
            ],
            [{"text": "⬇️ Скачать все", "callback_data": f"mjr:{session_token}:download_all"}],
            [{"text": "🆕 Новый prompt", "callback_data": f"mjr:{session_token}:new"}],
        ]
    }


async def register_midjourney_session(payload: Dict[str, Any]) -> Optional[str]:
    if not MAIN_INTERNAL_URL:
        return None
    headers: Dict[str, str] = {}
    if INTERNAL_API_KEY:
        headers["x-internal-key"] = INTERNAL_API_KEY
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(f"{MAIN_INTERNAL_URL}/internal/midjourney/register", json=payload, headers=headers)
        if r.status_code != 200:
            print(f"[workspace_image] Midjourney register failed HTTP {r.status_code}: {r.text[:300]}", flush=True)
            return None
        data = r.json()
        if isinstance(data, dict) and data.get("ok") and data.get("token"):
            return str(data["token"])
    except Exception as exc:
        print(f"[workspace_image] register_midjourney_session failed: {exc}", flush=True)
    return None


async def process_telegram_midjourney_job(job: Dict[str, Any]) -> None:
    chat_id = int(job.get("chat_id") or 0)
    user_id = int(job.get("user_id") or 0)
    action = str(job.get("mj_action") or "generate").strip().lower() or "generate"
    run_prompt = str(job.get("run_prompt") or "").strip()
    source_task_id = str(job.get("source_task_id") or "").strip()
    selected_image_no = int(job.get("selected_image_no") or 0)
    variation_type_name = str(job.get("variation_type") or "subtle").strip().lower()
    charge_tokens = int(job.get("charge_tokens") or 0)
    charge_ref_id = str(job.get("charge_ref_id") or "").strip() or None
    refund_reason = str(job.get("refund_reason") or "midjourney_refund").strip() or "midjourney_refund"
    model = str(job.get("model") or "midjourney-v7").strip() or "midjourney-v7"
    aspect_ratio = str(job.get("aspect_ratio") or "1:1").strip() or "1:1"
    speed_mode = str(job.get("speed_mode") or "fast").strip() or "fast"
    prompt = str(job.get("prompt") or "").strip()
    settings = job.get("settings") if isinstance(job.get("settings"), dict) else {}

    status_msg_id = await tg_send_message(
        chat_id,
        f"⏳ Midjourney: задача в обработке…\nМодель: {model.replace('midjourney-', 'Midjourney ').replace('v', 'V')}\nФормат: {aspect_ratio}\nСкорость: {speed_mode.title()}",
    )
    stop = asyncio.Event()

    async def _progress_loop() -> None:
        if not status_msg_id:
            return
        seq = [10, 20, 35, 50, 65, 80, 90, 95]
        i = 0
        while not stop.is_set():
            pct = seq[min(i, len(seq) - 1)]
            i += 1
            try:
                await tg_edit_message_text(chat_id, status_msg_id, f"⏳ Midjourney: обработка… {pct}%\nФормат: {aspect_ratio}\nСкорость: {speed_mode.title()}")
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=MIDJOURNEY_PROGRESS_STEP_SEC)
            except asyncio.TimeoutError:
                continue

    progress_task = asyncio.create_task(_progress_loop())

    try:
        if action == "generate":
            if not run_prompt:
                raise LegnextMidjourneyError("Midjourney prompt is empty")
            created = await create_midjourney_diffusion(text=run_prompt)
        elif action == "reroll":
            if not source_task_id:
                raise LegnextMidjourneyError("Midjourney reroll requires source_task_id")
            created = await create_midjourney_reroll(job_id=source_task_id)
        elif action == "variation":
            if not source_task_id:
                raise LegnextMidjourneyError("Midjourney variation requires source_task_id")
            if selected_image_no < 0 or selected_image_no > 3:
                raise LegnextMidjourneyError("Midjourney variation requires image_no 0..3")
            variation_type = 1 if variation_type_name == "strong" else 0
            created = await create_midjourney_variation(
                job_id=source_task_id,
                image_no=int(selected_image_no),
                variation_type=variation_type,
            )
        else:
            raise LegnextMidjourneyError(f"Unsupported Midjourney action: {action}")

        provider_task_id = str(created.get("job_id") or "").strip()
        if not provider_task_id:
            raise LegnextMidjourneyError("Midjourney API did not return job_id")

        final_payload = await _wait_for_midjourney_job(provider_task_id)
        status = str(final_payload.get("status") or "").strip().lower()
        if status != "completed":
            error = final_payload.get("error") or {}
            message = str(error.get("message") or error.get("raw_message") or final_payload.get("detail") or f"Midjourney task finished with status {status}")
            raise LegnextMidjourneyError(message)

        output = final_payload.get("output") or {}
        image_urls = [str(item or "").strip() for item in (output.get("image_urls") or []) if str(item or "").strip()]
        single_image_url = str(output.get("image_url") or "").strip()
        if not image_urls and single_image_url:
            image_urls = [single_image_url]
        if not image_urls:
            raise LegnextMidjourneyError("Midjourney completed without image URLs")

        image_items: List[Tuple[bytes, str]] = []
        image_entries: List[Dict[str, str]] = []
        for idx, url in enumerate(image_urls[:4], start=1):
            payload = await _download_bytes(url)
            ext = _detect_image_ext_from_bytes(payload, fallback="jpg")
            image_items.append((payload, ext))
            try:
                stored_url = _upload_midjourney_tg_asset(user_id=user_id, payload=payload, ext=ext, prefix=f"image_{idx}")
            except Exception as upload_exc:
                print(f"[workspace_image] Midjourney original upload failed idx={idx}: {upload_exc}", flush=True)
                stored_url = str(url or "").strip()
            image_entries.append({"url": stored_url, "ext": ext})

        if not image_items:
            raise LegnextMidjourneyError("Midjourney images could not be downloaded")

        grid_bytes = _make_midjourney_grid(image_items)
        try:
            grid_url = _upload_midjourney_tg_asset(user_id=user_id, payload=grid_bytes, ext="jpg", prefix="grid")
        except Exception as upload_exc:
            print(f"[workspace_image] Midjourney grid upload failed: {upload_exc}", flush=True)
            grid_url = ""

        session_payload = {
            "chat_id": int(chat_id),
            "user_id": int(user_id),
            "provider_task_id": provider_task_id,
            "source_task_id": source_task_id,
            "prompt": prompt,
            "run_prompt": run_prompt,
            "model": model,
            "action": action,
            "selected_image_no": selected_image_no,
            "variation_type": variation_type_name,
            "settings": settings,
            "aspect_ratio": aspect_ratio,
            "speed_mode": speed_mode,
            "charge_tokens": charge_tokens,
            "images": image_entries,
            "grid_url": grid_url,
        }
        session_token = await register_midjourney_session(session_payload)

        stop.set()
        try:
            await progress_task
        except Exception:
            pass
        if status_msg_id:
            try:
                await tg_edit_message_text(chat_id, status_msg_id, "✅ Midjourney: готово. Отправляю галерею…")
            except Exception:
                pass

        model_title = "Midjourney V8.1" if model == "midjourney-v8.1" else "Midjourney V7"
        action_title = "Готово: 4 варианта"
        if action == "reroll":
            action_title = "Reroll готов: новая подборка из 4 вариантов"
        elif action == "variation":
            action_title = f"Remix готов: 4 варианта по изображению #{int(selected_image_no) + 1}"

        caption = (
            f"{model_title}\n\n"
            f"{action_title}\n"
            f"Формат: {aspect_ratio}\n"
            f"Скорость: {speed_mode.title()}\n"
            "Выбери изображение для просмотра или remix."
        )
        if session_token:
            await tg_send_photo_bytes(
                chat_id,
                grid_bytes,
                caption=caption,
                reply_markup=_midjourney_grid_keyboard(session_token),
                filename="midjourney_grid.jpg",
                mime_type="image/jpeg",
            )
        else:
            await tg_send_photo_bytes(
                chat_id,
                grid_bytes,
                caption=caption + "\n\n⚠️ Интерактивные кнопки недоступны: main server не зарегистрировал галерею. Отправляю 4 оригинала файлами.",
                reply_markup=None,
                filename="midjourney_grid.jpg",
                mime_type="image/jpeg",
            )
            for idx, (payload, ext) in enumerate(image_items[:4], start=1):
                try:
                    await tg_send_document_bytes(chat_id, payload, filename=f"midjourney_{idx}.{ext or 'jpg'}", caption=f"⬇️ Midjourney оригинал #{idx}")
                except Exception as send_exc:
                    print(f"[workspace_image] Midjourney fallback original send failed idx={idx}: {send_exc}", flush=True)
        if status_msg_id:
            try:
                await tg_edit_message_text(chat_id, status_msg_id, "✅ Midjourney: готово")
            except Exception:
                pass
        return

    except Exception as exc:
        err = str(exc)[:800]
        print(f"[workspace_image] Midjourney job failed job={job.get('job_id')}: {err}", flush=True)
        stop.set()
        try:
            await progress_task
        except Exception:
            pass
        refund_status = ""
        if charge_tokens and charge_ref_id and user_id:
            try:
                add_tokens(user_id, int(charge_tokens), reason=refund_reason, ref_id=charge_ref_id, meta={"stage": "worker_failed", "provider": "midjourney", "action": action, "error": err})
                refund_status = "Токены возвращены."
            except Exception as refund_exc:
                print(f"[workspace_image] Midjourney refund failed: {refund_exc}", flush=True)
                refund_status = "Автовозврат токенов не удалось подтвердить. Если баланс не восстановится автоматически, напиши в поддержку."
        text = f"❌ Midjourney: ошибка генерации." + (f"\n{refund_status}" if refund_status else "") + f"\n{err}"
        if status_msg_id:
            try:
                await tg_edit_message_text(chat_id, status_msg_id, text)
                return
            except Exception:
                pass
        await tg_send_message(chat_id, text)


async def process_telegram_nano_banana_2_lite_job(job: Dict[str, Any]) -> None:
    chat_id = int(job.get("chat_id") or 0)
    user_id = int(job.get("user_id") or 0)
    prompt = str(job.get("prompt") or "").strip()
    aspect_ratio = str(job.get("aspect_ratio") or "auto").strip() or "auto"
    photo_file_ids = [str(item or "").strip() for item in (job.get("photo_file_ids") or []) if str(item or "").strip()]
    photo_urls = [str(item or "").strip() for item in (job.get("photo_urls") or []) if str(item or "").strip()]
    charge_tokens = int(job.get("charge_tokens") or 0)
    charge_ref_id = str(job.get("charge_ref_id") or "").strip() or None
    refund_reason = str(job.get("refund_reason") or "nano_banana_2_lite_refund").strip() or "nano_banana_2_lite_refund"

    status_msg_id = await tg_send_message(chat_id, f"⏳ Nano Banana 2 Lite: задача в обработке…\nКачество: 1K\nФормат: {aspect_ratio}")
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
                await tg_edit_message_text(chat_id, status_msg_id, f"⏳ Nano Banana 2 Lite: обработка… {pct}%\nКачество: 1K\nФормат: {aspect_ratio}")
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=PROGRESS_STEP_SEC)
            except asyncio.TimeoutError:
                continue

    progress_task = asyncio.create_task(_progress_loop())

    try:
        ref_urls = await _prepare_nano_banana_2_lite_reference_urls(
            user_id=user_id,
            photo_urls=photo_urls[:10],
            photo_file_ids=photo_file_ids[:10],
            max_refs=10,
        )
        out_bytes, ext = await handle_nano_banana_2_lite(
            prompt,
            source_image_urls=ref_urls,
            aspect_ratio=aspect_ratio,
            require_source_image=bool(photo_urls or photo_file_ids),
        )
        stop.set()
        try:
            await progress_task
        except Exception:
            pass
        if status_msg_id:
            try:
                await tg_edit_message_text(chat_id, status_msg_id, "✅ Nano Banana 2 Lite: готово. Отправляю файл…")
            except Exception:
                pass

        token = await register_dl2k_slot(chat_id, user_id, out_bytes)
        reply_markup = _download_keyboard(token, "1K")
        await tg_send_photo_bytes(
            chat_id,
            out_bytes,
            caption="✅ Готово (Nano Banana 2 Lite • 1K)",
            reply_markup=reply_markup,
            filename=f"nano_banana_2_lite.{ext or 'jpg'}",
            mime_type=_image_mime_type(ext or "jpg"),
        )
        if status_msg_id:
            try:
                await tg_edit_message_text(chat_id, status_msg_id, "✅ Nano Banana 2 Lite: готово")
            except Exception:
                pass
        return
    except Exception as exc:
        err = str(exc)[:800]
        print(f"[workspace_image] Nano Banana 2 Lite job failed job={job.get('job_id')}: {err}", flush=True)
        stop.set()
        try:
            await progress_task
        except Exception:
            pass
        refund_status = ""
        if charge_tokens and charge_ref_id and user_id:
            try:
                add_tokens(user_id, int(charge_tokens), reason=refund_reason, ref_id=charge_ref_id, meta={"stage": "worker_failed", "provider": "nano_banana_2_lite", "error": err})
                refund_status = "Токены возвращены."
            except Exception as refund_exc:
                print(f"[workspace_image] Nano Banana 2 Lite refund failed: {refund_exc}", flush=True)
                refund_status = "Автовозврат токенов не удалось подтвердить. Если баланс не восстановится автоматически, напиши в поддержку."
        text = f"❌ Nano Banana 2 Lite: ошибка генерации." + (f"\n{refund_status}" if refund_status else "") + f"\n{err}"
        if status_msg_id:
            try:
                await tg_edit_message_text(chat_id, status_msg_id, text)
                return
            except Exception:
                pass
        await tg_send_message(chat_id, text)


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


async def _process_workspace_image_queue_job(job: Dict[str, Any]) -> None:
    kind = str(job.get("kind") or "").strip().lower()
    if kind == "workspace_image_run":
        await process_workspace_image_job(job)
        print(f"[workspace_image] completed workspace image job={job.get('job_id')}", flush=True)
        return
    if kind == "telegram_nano_banana_2_lite_run":
        await process_telegram_nano_banana_2_lite_job(job)
        print(f"[workspace_image] completed telegram Nano Banana 2 Lite job={job.get('job_id')}", flush=True)
        return
    if kind == "telegram_gpt_image_2_kie_run":
        await process_telegram_gpt_image_2_kie_job(job)
        print(f"[workspace_image] completed telegram Gpt Image 2 job={job.get('job_id')}", flush=True)
        return
    print(f"[workspace_image] skipped unsupported workspace-image kind={kind} job={job.get('job_id')}", flush=True)


async def _process_midjourney_tg_queue_job(job: Dict[str, Any]) -> None:
    kind = str(job.get("kind") or "").strip().lower()
    if kind == "telegram_midjourney_run":
        await process_telegram_midjourney_job(job)
        print(f"[workspace_image] completed telegram Midjourney job={job.get('job_id')}", flush=True)
        return
    print(f"[workspace_image] skipped unsupported Midjourney kind={kind} job={job.get('job_id')}", flush=True)


async def _run_acquired_job(
    *,
    job: Dict[str, Any],
    sem: asyncio.Semaphore,
    processor,
    queue_label: str,
) -> None:
    try:
        await processor(job)
    except Exception as exc:
        print(f"[workspace_image] unhandled job error queue={queue_label} job={job.get('job_id')}: {exc}", flush=True)
    finally:
        sem.release()


async def _consume_queue(
    *,
    queue_name: str,
    sem: asyncio.Semaphore,
    processor,
    queue_label: str,
) -> None:
    tasks: set[asyncio.Task] = set()
    while True:
        await sem.acquire()
        try:
            job: Optional[Dict[str, Any]] = await dequeue_job(timeout_sec=10, queue_names=[queue_name])
        except Exception as exc:
            sem.release()
            print(f"[workspace_image] dequeue crashed queue={queue_label}: {exc}", flush=True)
            await asyncio.sleep(2)
            continue

        if not job:
            sem.release()
            done = {t for t in tasks if t.done()}
            for task in done:
                try:
                    task.result()
                except Exception as exc:
                    print(f"[workspace_image] task finished with error queue={queue_label}: {exc}", flush=True)
            tasks -= done
            continue

        task = asyncio.create_task(
            _run_acquired_job(job=job, sem=sem, processor=processor, queue_label=queue_label)
        )
        tasks.add(task)
        done = {t for t in tasks if t.done()}
        for done_task in done:
            try:
                done_task.result()
            except Exception as exc:
                print(f"[workspace_image] task finished with error queue={queue_label}: {exc}", flush=True)
        tasks -= done


async def main() -> None:
    if MIDJOURNEY_TG_QUEUE_NAME == WORKSPACE_IMAGE_QUEUE_NAME:
        raise RuntimeError("MIDJOURNEY_TG_QUEUE_NAME must be different from WORKSPACE_IMAGE_QUEUE_NAME")
    if WORKSPACE_NB2LITE_QUEUE_NAME == MIDJOURNEY_TG_QUEUE_NAME:
        raise RuntimeError("WORKSPACE_NB2LITE_QUEUE_NAME must be different from MIDJOURNEY_TG_QUEUE_NAME")

    same_image_queue = WORKSPACE_NB2LITE_QUEUE_NAME == WORKSPACE_IMAGE_QUEUE_NAME
    if same_image_queue:
        print(
            "[workspace_image] WARNING: WORKSPACE_NB2LITE_QUEUE_NAME equals WORKSPACE_IMAGE_QUEUE_NAME; "
            "Nano Banana 2 Lite will share the common image queue.",
            flush=True,
        )

    print(
        f"[workspace_image] worker started "
        f"workspace_queue={WORKSPACE_IMAGE_QUEUE_NAME} image_concurrency={WORKSPACE_IMAGE_CONCURRENCY} "
        f"nb2lite_queue={WORKSPACE_NB2LITE_QUEUE_NAME} nb2lite_concurrency={WORKSPACE_NB2LITE_CONCURRENCY} "
        f"midjourney_queue={MIDJOURNEY_TG_QUEUE_NAME} midjourney_tg_concurrency={MIDJOURNEY_TG_CONCURRENCY}",
        flush=True,
    )

    consumers = [
        _consume_queue(
            queue_name=WORKSPACE_IMAGE_QUEUE_NAME,
            sem=image_sem,
            processor=_process_workspace_image_queue_job,
            queue_label="workspace_image",
        ),
        _consume_queue(
            queue_name=MIDJOURNEY_TG_QUEUE_NAME,
            sem=midjourney_tg_sem,
            processor=_process_midjourney_tg_queue_job,
            queue_label="midjourney_tg",
        ),
    ]
    if not same_image_queue:
        consumers.append(
            _consume_queue(
                queue_name=WORKSPACE_NB2LITE_QUEUE_NAME,
                sem=nb2lite_sem,
                processor=_process_workspace_image_queue_job,
                queue_label="nb2lite",
            )
        )

    await asyncio.gather(*consumers)


if __name__ == "__main__":
    asyncio.run(main())
