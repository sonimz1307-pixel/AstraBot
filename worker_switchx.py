from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
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

SWITCHX_CONCURRENCY = int(os.getenv("SWITCHX_CONCURRENCY", os.getenv("SWITCHX_WORKER_CONCURRENCY", "2")))
MUSIC_CONCURRENCY = int(os.getenv("MUSIC_CONCURRENCY", "3"))
SORA_CONCURRENCY = int(os.getenv("SORA_CONCURRENCY", "2"))
SWITCHX_TIMEOUT_SEC = int(os.getenv("SWITCHX_TIMEOUT_SEC", "3600"))
SORA_TIMEOUT_SEC = int(os.getenv("SORA_TIMEOUT_SEC", "1800"))
SORA_POLL_SEC = float(os.getenv("SORA_POLL_SEC", "10"))
SWITCHX_POLL_SEC = float(os.getenv("SWITCHX_POLL_SEC", "8"))
SUNOAPI_POLL_TIMEOUT_SEC = int(os.getenv("SUNOAPI_POLL_TIMEOUT_SEC", "600"))
PIAPI_POLL_TIMEOUT_SEC = int(os.getenv("PIAPI_POLL_TIMEOUT_SEC", "300"))

MUSIC_QUEUE_NAME = os.getenv("MUSIC_QUEUE_NAME", "music").strip() or "music"
SWITCHX_QUEUE_NAME = os.getenv("SWITCHX_QUEUE_NAME", "switchx").strip() or "switchx"
SORA_QUEUE_NAME = os.getenv("SORA_QUEUE_NAME", "sora").strip() or "sora"

switchx_sem = asyncio.Semaphore(SWITCHX_CONCURRENCY)
music_sem = asyncio.Semaphore(MUSIC_CONCURRENCY)
sora_sem = asyncio.Semaphore(SORA_CONCURRENCY)

MUSIC_JOB_TYPES = {"music", "music_piapi", "music_suno"}
SORA_JOB_TYPES = {"sora_video"}
SUPPORTED_JOB_TYPES = {JOB_TYPE_SWITCHX, *MUSIC_JOB_TYPES, *SORA_JOB_TYPES}

PIAPI_API_KEY = os.getenv("PIAPI_API_KEY", "").strip()
PIAPI_BASE_URL = os.getenv("PIAPI_BASE_URL", "https://api.piapi.ai").rstrip("/")

SUNOAPI_API_KEY = os.getenv("SUNOAPI_API_KEY", "").strip()
SUNOAPI_BASE_URL = os.getenv("SUNOAPI_BASE_URL", "https://api.sunoapi.org/api/v1").rstrip("/")
if SUNOAPI_BASE_URL.rstrip("/") == "https://api.sunoapi.org":
    SUNOAPI_BASE_URL = "https://api.sunoapi.org/api/v1"
SUNOAPI_CALLBACK_URL = os.getenv("SUNOAPI_CALLBACK_URL", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_API_BASE = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1").rstrip("/")


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
        raise RuntimeError(j.get("description") or f"Telegram sendMessage failed: {j}")
    except Exception:
        if r.status_code >= 300:
            raise RuntimeError(f"Telegram sendMessage failed: {r.status_code} {r.text[:500]}")
    return None


async def tg_edit_message_text(chat_id: int, message_id: int, text: str) -> None:
    if not TG_API:
        return
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(
            f"{TG_API}/editMessageText",
            json={"chat_id": int(chat_id), "message_id": int(message_id), "text": text},
        )
    try:
        j = r.json()
        if isinstance(j, dict) and not j.get("ok", False):
            raise RuntimeError(j.get("description") or f"Telegram editMessageText failed: {j}")
    except Exception:
        if r.status_code >= 300:
            raise RuntimeError(f"Telegram editMessageText failed: {r.status_code} {r.text[:500]}")


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


async def tg_send_audio_bytes(
    chat_id: int,
    audio_bytes: bytes,
    *,
    filename: str = "track.mp3",
    caption: Optional[str] = None,
    reply_markup: Optional[dict] = None,
) -> None:
    if not TG_API:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
    if not audio_bytes:
        raise RuntimeError("Empty audio bytes for sendAudio")
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    files = {"audio": (filename, audio_bytes, "audio/mpeg")}
    async with httpx.AsyncClient(timeout=240.0) as client:
        r = await client.post(f"{TG_API}/sendAudio", data=data, files=files)
    try:
        j = r.json()
        if isinstance(j, dict) and j.get("ok", False):
            return
        raise RuntimeError(j.get("description") or f"Telegram sendAudio error: {j}")
    except Exception:
        if r.status_code >= 300:
            raise RuntimeError(f"Telegram sendAudio failed: {r.status_code} {r.text[:500]}")


async def tg_send_document_bytes(
    chat_id: int,
    file_bytes: bytes,
    *,
    filename: str,
    mime: str = "application/octet-stream",
    caption: Optional[str] = None,
    reply_markup: Optional[dict] = None,
) -> None:
    if not TG_API:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    files = {"document": (filename, file_bytes, mime)}
    async with httpx.AsyncClient(timeout=240.0) as client:
        r = await client.post(f"{TG_API}/sendDocument", data=data, files=files)
    try:
        j = r.json()
        if isinstance(j, dict) and j.get("ok", False):
            return
        raise RuntimeError(j.get("description") or f"Telegram sendDocument error: {j}")
    except Exception:
        if r.status_code >= 300:
            raise RuntimeError(f"Telegram sendDocument failed: {r.status_code} {r.text[:500]}")


async def tg_send_audio_from_url(
    chat_id: int,
    url: str,
    *,
    caption: Optional[str] = None,
    reply_markup: Optional[dict] = None,
) -> None:
    try:
        content = await http_get_bytes(url, timeout=180.0)
        if len(content) > 48 * 1024 * 1024:
            await tg_send_message(chat_id, f"🎧 MP3: {url}", reply_markup=reply_markup)
            return
        try:
            await tg_send_audio_bytes(chat_id, content, filename="track.mp3", caption=caption, reply_markup=reply_markup)
        except Exception:
            await tg_send_document_bytes(chat_id, content, filename="track.mp3", mime="audio/mpeg", caption=caption, reply_markup=reply_markup)
    except Exception:
        await tg_send_message(chat_id, f"🎧 MP3: {url}", reply_markup=reply_markup)


def _sora_headers() -> dict[str, str]:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set")
    return {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
    }


def _sora_size_from_aspect(aspect_ratio: str) -> str:
    ar = str(aspect_ratio or "16:9").strip()
    if ar == "9:16":
        return "720x1280"
    return "1280x720"


async def sora_create_video(*, prompt: str, duration: int, aspect_ratio: str, model: str = "sora-2") -> dict:
    url = f"{OPENAI_API_BASE}/videos"
    files = [
        ("model", (None, str(model or "sora-2"))),
        ("prompt", (None, str(prompt or "").strip())),
        ("seconds", (None, str(int(duration)))),
        ("size", (None, _sora_size_from_aspect(aspect_ratio))),
    ]
    headers = _sora_headers()
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(url, headers=headers, files=files)
    if r.status_code >= 300:
        try:
            j = r.json()
            raise RuntimeError(((j.get("error") or {}).get("message")) or r.text[:800])
        except Exception:
            raise RuntimeError(f"OpenAI create video failed: {r.status_code} {r.text[:800]}")
    return r.json()


async def sora_retrieve_video(video_id: str) -> dict:
    url = f"{OPENAI_API_BASE}/videos/{video_id}"
    headers = _sora_headers()
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(url, headers=headers)
    if r.status_code >= 300:
        try:
            j = r.json()
            raise RuntimeError(((j.get("error") or {}).get("message")) or r.text[:800])
        except Exception:
            raise RuntimeError(f"OpenAI retrieve video failed: {r.status_code} {r.text[:800]}")
    return r.json()


async def sora_download_video(video_id: str) -> bytes:
    url = f"{OPENAI_API_BASE}/videos/{video_id}/content"
    headers = _sora_headers()
    async with httpx.AsyncClient(timeout=300.0) as client:
        r = await client.get(url, headers=headers, params={"variant": "video"})
    if r.status_code >= 300:
        try:
            j = r.json()
            raise RuntimeError(((j.get("error") or {}).get("message")) or r.text[:800])
        except Exception:
            raise RuntimeError(f"OpenAI download video failed: {r.status_code} {r.text[:800]}")
    return r.content


async def sora_poll_video(video_id: str, *, timeout_sec: int = SORA_TIMEOUT_SEC, sleep_sec: float = SORA_POLL_SEC) -> dict:
    t0 = time.time()
    last: dict[str, Any] = {}
    while True:
        last = await sora_retrieve_video(video_id)
        status = str(last.get("status") or "").strip().lower()
        if status in ("completed", "failed"):
            return last
        if time.time() - t0 > timeout_sec:
            raise TimeoutError(f"Sora timeout after {timeout_sec}s (video_id={video_id}, status={status})")
        await asyncio.sleep(max(2.0, float(sleep_sec)))


# ---------------- PiAPI music helpers ----------------
async def piapi_create_task(payload: dict) -> dict:
    if not PIAPI_API_KEY:
        raise RuntimeError("PIAPI_API_KEY is empty. Set it in Render env vars.")
    url = f"{PIAPI_BASE_URL}/api/v1/task"
    headers = {"X-API-Key": PIAPI_API_KEY, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(url, headers=headers, json=payload)
    r.raise_for_status()
    return r.json()


async def piapi_get_task(task_id: str) -> dict:
    if not PIAPI_API_KEY:
        raise RuntimeError("PIAPI_API_KEY is empty. Set it in Render env vars.")
    url = f"{PIAPI_BASE_URL}/api/v1/task/{task_id}"
    headers = {"X-API-Key": PIAPI_API_KEY}
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(url, headers=headers)
    r.raise_for_status()
    return r.json()


async def piapi_poll_task(task_id: str, *, timeout_sec: int = 240, sleep_sec: float = 2.0) -> dict:
    t0 = time.time()
    last = None
    while True:
        last = await piapi_get_task(task_id)
        status = ((last.get("data") or {}).get("status") or "").lower()
        if status in ("completed", "failed"):
            return last
        if time.time() - t0 > timeout_sec:
            raise TimeoutError(f"PiAPI task timeout after {timeout_sec}s (task_id={task_id}, status={status})")
        await asyncio.sleep(sleep_sec)


def _suno_sig(uid: int, chat_id: int) -> str:
    secret = (WEBHOOK_SECRET or "change_me").encode("utf-8")
    msg = f"{int(uid)}:{int(chat_id)}".encode("utf-8")
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def _build_suno_callback_url(user_id: int, chat_id: int) -> str:
    base = (PUBLIC_BASE_URL or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("PUBLIC_BASE_URL is not set (needed for SunoAPI callBackUrl)")
    sig = _suno_sig(int(user_id), int(chat_id))
    return f"{base}/api/suno/callback?uid={int(user_id)}&chat={int(chat_id)}&sig={sig}"


# ---------------- SunoAPI.org helpers ----------------
async def sunoapi_generate_task(
    *,
    prompt: str,
    custom_mode: bool,
    instrumental: bool,
    model: str,
    user_id: int,
    chat_id: int,
    title: str = "",
    style: str = "",
) -> str:
    if not SUNOAPI_API_KEY:
        raise RuntimeError("SUNOAPI_API_KEY is empty. Set it in Render env vars.")
    url = f"{SUNOAPI_BASE_URL}/generate"
    payload = {
        "prompt": prompt,
        "customMode": bool(custom_mode),
        "instrumental": bool(instrumental),
        "model": model,
    }
    if title:
        payload["title"] = title
    if style:
        payload["style"] = style
    payload["callBackUrl"] = SUNOAPI_CALLBACK_URL or _build_suno_callback_url(int(user_id), int(chat_id))
    headers = {"Authorization": f"Bearer {SUNOAPI_API_KEY}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(url, headers=headers, json=payload)
    r.raise_for_status()
    js = r.json()
    if js.get("code") != 200:
        raise RuntimeError(f"SunoAPI generate failed: {js}")
    task_id = (((js.get("data") or {}).get("taskId")) or "").strip()
    if not task_id:
        raise RuntimeError(f"SunoAPI did not return taskId: {js}")
    return task_id


async def sunoapi_get_task(task_id: str) -> dict:
    if not SUNOAPI_API_KEY:
        raise RuntimeError("SUNOAPI_API_KEY is empty. Set it in Render env vars.")
    url = f"{SUNOAPI_BASE_URL}/generate/record-info"
    headers = {"Authorization": f"Bearer {SUNOAPI_API_KEY}"}
    params = {"taskId": task_id}
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(url, headers=headers, params=params)
    r.raise_for_status()
    return r.json()


async def sunoapi_poll_task(task_id: str, *, timeout_sec: Optional[int] = None, sleep_sec: float = 2.0) -> dict:
    if timeout_sec is None:
        timeout_sec = SUNOAPI_POLL_TIMEOUT_SEC
    t0 = time.time()
    last = None
    while True:
        last = await sunoapi_get_task(task_id)
        data = last.get("data") or {}
        status = str(data.get("status") or "").upper().strip()
        if status in ("SUCCESS", "FAILED", "ERROR"):
            return last
        if time.time() - t0 > timeout_sec:
            raise TimeoutError(f"SunoAPI task timeout after {timeout_sec}s (taskId={task_id}, status={status})")
        await asyncio.sleep(sleep_sec)


def _sunoapi_extract_tracks(task_json: dict) -> list[dict]:
    data = task_json.get("data") or {}
    resp = data.get("response") or {}
    resp_data = resp.get("data") or []
    if isinstance(resp_data, list):
        return [x for x in resp_data if isinstance(x, dict)]
    return []


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


def _pick_first_url(val: Any) -> str:
    if not val:
        return ""
    if isinstance(val, str):
        s = val.strip()
        return s if s.startswith(("http://", "https://")) else ""
    if isinstance(val, dict):
        for k in (
            "url", "audio_url", "audioUrl", "song_url", "songUrl", "song_path", "songPath",
            "mp3", "mp3_url", "file_url", "fileUrl", "download_url", "downloadUrl",
            "source_stream_audio_url", "sourceStreamAudioUrl", "video_url", "videoUrl",
            "image_url", "imageUrl",
        ):
            v = val.get(k)
            if isinstance(v, str) and v.strip().startswith(("http://", "https://")):
                return v.strip()
        for v in val.values():
            u = _pick_first_url(v)
            if u:
                return u
    if isinstance(val, list):
        for x in val:
            u = _pick_first_url(x)
            if u:
                return u
    return ""


def _extract_audio_url(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    for k in (
        "audio_url", "audioUrl", "song_url", "songUrl", "song_path", "songPath",
        "mp3_url", "mp3", "file_url", "fileUrl", "url", "source_stream_audio_url", "sourceStreamAudioUrl",
    ):
        v = item.get(k)
        if isinstance(v, str) and v.strip().startswith(("http://", "https://")):
            return v.strip()
    u = _pick_first_url(item.get("audio"))
    if u:
        return u
    for k in ("audio_urls", "audios", "urls", "songs"):
        u = _pick_first_url(item.get(k))
        if u:
            return u
    return ""


def _music_provider_from_job(job: Dict[str, Any], settings: Dict[str, Any], ai_choice: str, job_type: str) -> str:
    provider = str(
        job.get("provider")
        or settings.get("provider")
        or settings.get("api")
        or settings.get("ai_provider")
        or settings.get("aiProvider")
        or ""
    ).lower().strip()
    if provider in ("suno-api", "suno_api", "suno api"):
        provider = "sunoapi"
    if ai_choice == "udio":
        return "piapi"
    if provider in ("piapi", "sunoapi", "auto"):
        return provider
    if job_type == "music_suno":
        return "sunoapi"
    if job_type == "music_piapi":
        return "piapi"
    return os.getenv("MUSIC_PROVIDER_DEFAULT", "piapi").lower().strip() or "piapi"


def _build_piapi_payload(settings: Dict[str, Any], ai_choice: str) -> dict:
    if ai_choice == "udio":
        udio_prompt = (
            str(settings.get("gpt_description_prompt") or "").strip()
            or str(settings.get("prompt") or "").strip()
            or "Modern atmospheric music with emotional melody"
        )
        return {
            "model": "music-u",
            "task_type": "generate_music",
            "input": {
                "gpt_description_prompt": udio_prompt,
                "lyrics_type": "instrumental" if settings.get("make_instrumental") else "generate",
            },
            "config": {"service_mode": str(settings.get("service_mode") or "public")},
        }

    input_block: Dict[str, Any] = {
        "make_instrumental": bool(settings.get("make_instrumental")),
    }
    title = str(settings.get("title") or "").strip()
    tags = str(settings.get("tags") or "").strip()
    if title:
        input_block["title"] = title
    if tags:
        input_block["tags"] = tags

    music_mode = str(settings.get("music_mode") or "prompt").strip().lower()
    if music_mode == "prompt":
        input_block["gpt_description_prompt"] = str(settings.get("gpt_description_prompt") or "").strip()
    else:
        input_block["prompt"] = str(settings.get("prompt") or "").strip()

    return {
        "model": "suno",
        "task_type": "music",
        "input": input_block,
        "config": {"service_mode": str(settings.get("service_mode") or "public")},
    }


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


async def handle_sora_job(job: Dict[str, Any]) -> None:
    chat_id = int(job.get("chat_id") or 0)
    user_id = int(job.get("user_id") or 0)
    prompt = str(job.get("prompt") or "").strip()
    model = str(job.get("model") or "sora-2").strip() or "sora-2"
    duration = int(job.get("duration") or 4)
    aspect_ratio = str(job.get("aspect_ratio") or "16:9").strip()
    charge_tokens = int(job.get("charge_tokens") or 0)

    if not chat_id or not user_id:
        raise RuntimeError("sora job missing chat_id/user_id")
    if not prompt:
        raise RuntimeError("sora job missing prompt")
    if duration not in (4, 8, 12):
        raise RuntimeError(f"sora invalid duration: {duration}")
    if aspect_ratio not in ("16:9", "9:16"):
        raise RuntimeError(f"sora invalid aspect_ratio: {aspect_ratio}")

    msg_id = await tg_send_message(chat_id, f"⏳ Sora 2: отправляю задачу…\n{duration} сек • {aspect_ratio}")
    stop = asyncio.Event()

    async def _progress_loop() -> None:
        if not msg_id:
            return
        seq = [8, 16, 24, 32, 45, 58, 71, 84, 92, 97]
        i = 0
        while not stop.is_set():
            pct = seq[min(i, len(seq) - 1)]
            i += 1
            try:
                await tg_edit_message_text(chat_id, msg_id, f"⏳ Sora 2: генерация… {pct}%")
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(4.0, SORA_POLL_SEC))
            except asyncio.TimeoutError:
                continue

    prog_task = asyncio.create_task(_progress_loop())

    try:
        created = await sora_create_video(
            prompt=prompt,
            duration=duration,
            aspect_ratio=aspect_ratio,
            model=model,
        )
        video_id = str(created.get("id") or "").strip()
        if not video_id:
            raise RuntimeError(f"OpenAI did not return video id: {created}")

        done = await sora_poll_video(video_id)

        stop.set()
        try:
            await prog_task
        except Exception:
            pass

        status = str(done.get("status") or "").strip().lower()
        if status == "failed":
            err = ((done.get("error") or {}).get("message")) or "Sora generation failed"
            raise RuntimeError(err)
        if status != "completed":
            raise RuntimeError(f"Unexpected Sora status: {status}")

        if msg_id:
            try:
                await tg_edit_message_text(chat_id, msg_id, "✅ Sora 2: готово. Отправляю видео…")
            except Exception:
                pass

        video_bytes = await sora_download_video(video_id)
        await tg_send_video_bytes(
            chat_id,
            video_bytes,
            filename="sora2.mp4",
            caption=f"🎬 Sora 2 готово • {duration} сек • {aspect_ratio}",
        )

    except Exception as e:
        stop.set()
        try:
            await prog_task
        except Exception:
            pass
        if charge_tokens > 0:
            try:
                add_tokens(user_id, int(charge_tokens), reason="sora_video_refund", meta={"error": str(e)[:300]})
            except TypeError:
                try:
                    add_tokens(user_id, int(charge_tokens), reason="sora_video_refund")
                except Exception:
                    pass
        await tg_send_message(chat_id, f"❌ Sora 2: ошибка генерации.\n{e}")


async def handle_music_job(job: Dict[str, Any]) -> None:
    job_type = str(job.get("type") or job.get("job_type") or "").strip().lower()
    chat_id = int(job.get("chat_id") or 0)
    user_id = int(job.get("user_id") or 0)
    settings = dict(job.get("settings") or {})
    ai_choice = str(job.get("ai") or settings.get("ai") or "suno").lower().strip()
    if ai_choice not in ("suno", "udio"):
        ai_choice = "suno"

    if not chat_id or not user_id:
        raise RuntimeError("music job missing chat_id/user_id")

    provider = _music_provider_from_job(job, settings, ai_choice, job_type)
    if provider not in ("piapi", "sunoapi", "auto"):
        provider = "piapi"

    charge_tokens = int(job.get("charge_tokens") or 0)
    refund_reason = str(job.get("refund_reason") or "music_refund").strip() or "music_refund"
    menu_markup = job.get("reply_markup") if isinstance(job.get("reply_markup"), dict) else None

    msg_id = await tg_send_message(chat_id, f"⏳ Запускаю генерацию музыки…\nПровайдер: {provider}")
    stop = asyncio.Event()

    async def _progress_loop() -> None:
        if not msg_id:
            return
        seq = [10, 20, 35, 50, 65, 80, 92, 97]
        i = 0
        while not stop.is_set():
            pct = seq[min(i, len(seq) - 1)]
            i += 1
            try:
                await tg_edit_message_text(chat_id, msg_id, f"⏳ Музыка: генерация… {pct}%")
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=6.0)
            except asyncio.TimeoutError:
                continue

    prog_task = asyncio.create_task(_progress_loop())

    async def _run_piapi() -> tuple[str, dict]:
        payload_api = dict(job.get("payload_api") or {})
        if not payload_api:
            payload_api = _build_piapi_payload(settings, ai_choice)
        created_local = await piapi_create_task(payload_api)
        task_id_local = ((created_local.get("data") or {}).get("task_id")) or ""
        if not task_id_local:
            raise RuntimeError(f"PiAPI did not return task_id: {created_local}")
        done_local = await piapi_poll_task(task_id_local, timeout_sec=PIAPI_POLL_TIMEOUT_SEC, sleep_sec=2.0)
        return ("piapi", done_local)

    async def _run_sunoapi() -> tuple[str, dict]:
        prompt_text = (
            str(settings.get("gpt_description_prompt") or "").strip()
            if str(settings.get("music_mode") or "prompt").strip().lower() == "prompt"
            else str(settings.get("prompt") or "").strip()
        )
        if not prompt_text:
            prompt_text = "A modern catchy song with clear structure and strong hook"
        mv_local = str(settings.get("mv") or "").lower().strip()
        model_enum = "V4_5ALL"
        if "v5" in mv_local:
            model_enum = "V5"
        elif any(x in mv_local for x in ("v4_5", "v4.5", "v4-5")):
            model_enum = "V4_5ALL"
        elif "v4" in mv_local:
            model_enum = "V4"
        custom_mode = bool(str(settings.get("music_mode") or "prompt").strip().lower() != "prompt")
        instrumental = bool(settings.get("make_instrumental"))
        title_local = str(settings.get("title") or "").strip()
        style_local = str(settings.get("tags") or "").strip()
        task_id_local = await sunoapi_generate_task(
            prompt=prompt_text,
            custom_mode=custom_mode,
            instrumental=instrumental,
            model=model_enum,
            user_id=user_id,
            chat_id=chat_id,
            title=title_local,
            style=style_local,
        )
        done_local = await sunoapi_poll_task(task_id_local, timeout_sec=SUNOAPI_POLL_TIMEOUT_SEC, sleep_sec=2.0)
        return ("sunoapi", done_local)

    provider_norm = provider if provider in ("piapi", "sunoapi", "auto") else "auto"
    default_primary = os.getenv("MUSIC_PROVIDER_DEFAULT", "piapi").lower().strip()
    if default_primary not in ("piapi", "sunoapi"):
        default_primary = "piapi"
    primary = default_primary if provider_norm == "auto" else provider_norm
    secondary = "sunoapi" if primary == "piapi" else "piapi"

    try:
        try:
            if primary == "sunoapi":
                source, done = await _run_sunoapi()
            else:
                source, done = await _run_piapi()
        except Exception as e_primary:
            if provider_norm != "auto":
                raise RuntimeError(f"Провайдер {primary} вернул ошибку: {e_primary}")
            can_fallback = (secondary == "sunoapi" and bool(SUNOAPI_API_KEY)) or (secondary == "piapi" and bool(PIAPI_API_KEY))
            if not can_fallback:
                raise RuntimeError(f"Провайдер {primary} упал, а запасной {secondary} недоступен: {e_primary}")
            await tg_send_message(chat_id, f"⚠️ Основной провайдер ({primary}) упал: {e_primary}\nПробую запасной ({secondary})…")
            if secondary == "sunoapi":
                source, done = await _run_sunoapi()
            else:
                source, done = await _run_piapi()

        if source == "sunoapi":
            data = done.get("data") or {}
            status = str(data.get("status") or "").upper().strip()
            if status != "SUCCESS":
                raise RuntimeError(f"Музыка не сгенерировалась (SunoAPI). Статус: {status}. {done.get('msg') or 'unknown error'}")
            out = _sunoapi_extract_tracks(done)
            if not out:
                raise RuntimeError("SunoAPI завершил задачу, но не вернул ссылки на треки")
        else:
            data = done.get("data") or {}
            status = str(data.get("status") or "")
            if status.lower() != "completed":
                err = (data.get("error") or {}).get("message") or "unknown error"
                raise RuntimeError(f"Музыка не сгенерировалась. Статус: {status}. {err}")
            out = data.get("output") or []
            if isinstance(out, dict):
                out = [out]
            if not out:
                raise RuntimeError("PiAPI завершил задачу, но не вернул output")

        stop.set()
        try:
            await prog_task
        except Exception:
            pass

        if msg_id:
            try:
                await tg_edit_message_text(chat_id, msg_id, "✅ Музыка готова. Отправляю треки…")
            except Exception:
                pass
        else:
            await tg_send_message(chat_id, "✅ Музыка готова.")

        for i, item in enumerate(out[:2], start=1):
            audio_url = _extract_audio_url(item)
            video_url = _pick_first_url(item.get("video_url") or item.get("video") or item.get("mp4") or item.get("videoUrl"))
            if audio_url:
                await tg_send_audio_from_url(
                    chat_id,
                    audio_url,
                    caption=f"🎵 Трек #{i}",
                    reply_markup=menu_markup if i == 1 else None,
                )
            else:
                keys = ", ".join(list(item.keys())[:15]) if isinstance(item, dict) else str(type(item))
                await tg_send_message(chat_id, f"⚠️ Трек #{i}: не удалось найти ссылку на MP3. Поля: {keys}", reply_markup=menu_markup if i == 1 else None)
            if video_url:
                await tg_send_message(chat_id, f"🎬 MP4: {video_url}")

    except Exception as e:
        stop.set()
        try:
            await prog_task
        except Exception:
            pass
        if charge_tokens > 0:
            try:
                add_tokens(user_id, int(charge_tokens), reason=refund_reason, meta={"error": str(e)[:300], "job_type": job_type})
            except TypeError:
                try:
                    add_tokens(user_id, int(charge_tokens), reason=refund_reason)
                except Exception:
                    pass
        await tg_send_message(chat_id, f"❌ Ошибка music worker: {e}", reply_markup=menu_markup)


async def handle_job(job: Dict[str, Any]) -> None:
    job_type = str(job.get("type") or job.get("job_type") or "").strip()

    if job_type == JOB_TYPE_SWITCHX:
        async with switchx_sem:
            await handle_switchx_job(job)
        return

    if job_type in MUSIC_JOB_TYPES:
        async with music_sem:
            await handle_music_job(job)
        return

    if job_type in SORA_JOB_TYPES:
        async with sora_sem:
            await handle_sora_job(job)
        return

    print("Unknown job type:", job_type)


async def worker_loop() -> None:
    async def _run_one(job: Dict[str, Any]) -> None:
        try:
            await handle_job(job)
        except Exception as e:
            print("Generation job failed:", e)

    queue_names = [MUSIC_QUEUE_NAME, SWITCHX_QUEUE_NAME, SORA_QUEUE_NAME]

    while True:
        job = await dequeue_job(timeout_sec=10, queue_names=queue_names)
        if not job:
            continue

        job_type = str(job.get("type") or job.get("job_type") or "").strip()
        if job_type not in SUPPORTED_JOB_TYPES:
            print("Skipped unsupported job type from combined worker:", job_type)
            continue

        asyncio.create_task(_run_one(job))


def main() -> None:
    if not os.getenv("REDIS_URL", "").strip():
        raise RuntimeError("REDIS_URL is not set")
    print(
        "Generation worker started. "
        f"switchx_concurrency={SWITCHX_CONCURRENCY} "
        f"music_concurrency={MUSIC_CONCURRENCY} "
        f"sora_concurrency={SORA_CONCURRENCY} "
        f"queues={[MUSIC_QUEUE_NAME, SWITCHX_QUEUE_NAME, SORA_QUEUE_NAME]}"
    )
    asyncio.run(worker_loop())


if __name__ == "__main__":
    main()
