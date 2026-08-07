from __future__ import annotations

import asyncio
import json
import os
import time
from uuid import uuid4, uuid5, NAMESPACE_URL
from typing import Any, Dict, Optional

import httpx

from app.services.site_builder_service import process_site_job
from billing_db import add_tokens, supabase as billing_supabase
from seedance25_billing import refund_seedance25_once
from free_plan_limits import FEATURE_CHAT, release_free_usage
from kling3_kie_runner import run_kling3_kie_task_and_wait
from queue_redis import (
    dequeue_job,
    enqueue_job,
    dequeue_reliable_job,
    ack_reliable_job,
    requeue_reliable_job,
    touch_reliable_job,
    get_reliable_job_state,
    set_reliable_job_state,
    delete_reliable_job_state,
)
from app.services.workspace_worker_jobs import process_workspace_video_job
from seedance_25_kie import (
    Seedance25TaskFailedError,
    Seedance25TaskPendingError,
    run_seedance25_omni_reference_urls,
    run_seedance25_text_to_video,
    upload_seedance25_reference_bytes,
    wait_seedance25_task,
)

SITE_QUEUE_NAME = (os.getenv("SITE_QUEUE_NAME", "site") or "site").strip() or "site"
KLING3_KIE_QUEUE_NAME = (os.getenv("KLING3_KIE_QUEUE_NAME", "kling3_kie") or "kling3_kie").strip() or "kling3_kie"
TG_STT_QUEUE_NAME = (os.getenv("TG_STT_QUEUE_NAME", "redactor_tg_stt") or "redactor_tg_stt").strip() or "redactor_tg_stt"
TG_CHAT_OPENAI_QUEUE_NAME = (os.getenv("TG_CHAT_OPENAI_QUEUE_NAME", "tg_chat_openai") or "tg_chat_openai").strip() or "tg_chat_openai"
TG_CHAT_CLAUDE_QUEUE_NAME = (os.getenv("TG_CHAT_CLAUDE_QUEUE_NAME", "tg_chat_claude") or "tg_chat_claude").strip() or "tg_chat_claude"
TG_CHAT_FABLE_QUEUE_NAME = (os.getenv("TG_CHAT_FABLE_QUEUE_NAME", "tg_chat_fable") or "tg_chat_fable").strip() or "tg_chat_fable"
SEEDANCE25_QUEUE_NAME = (os.getenv("SEEDANCE25_QUEUE_NAME", "seedance25") or "seedance25").strip() or "seedance25"
SITE_WORKER_CONCURRENCY = max(1, int(os.getenv("SITE_WORKER_CONCURRENCY", "1") or "1"))
KLING3_KIE_WORKER_CONCURRENCY = max(1, int(os.getenv("KLING3_KIE_WORKER_CONCURRENCY", "3") or "3"))
TG_STT_CONCURRENCY = max(1, int(os.getenv("TG_STT_CONCURRENCY", "2") or "2"))
SEEDANCE25_CONCURRENCY = max(1, int(os.getenv("SEEDANCE25_CONCURRENCY", "4") or "4"))
SEEDANCE25_RELIABLE_STALE_SEC = max(120, int(os.getenv("SEEDANCE25_RELIABLE_STALE_SEC", "300") or "300"))
SEEDANCE25_RELIABLE_TOUCH_SEC = max(30, min(SEEDANCE25_RELIABLE_STALE_SEC // 2, int(os.getenv("SEEDANCE25_RELIABLE_TOUCH_SEC", "60") or "60")))
SEEDANCE25_DELIVERY_MAX_ATTEMPTS = max(1, int(os.getenv("SEEDANCE25_DELIVERY_MAX_ATTEMPTS", "6") or "6"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else ""
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe").strip() or "gpt-4o-mini-transcribe"
AI_CHAT_VOICE_LANGUAGE = os.getenv("AI_CHAT_VOICE_LANGUAGE", "ru").strip()
try:
    AI_CHAT_VOICE_MAX_BYTES = int(os.getenv("AI_CHAT_VOICE_MAX_BYTES", str(20 * 1024 * 1024)) or (20 * 1024 * 1024))
except Exception:
    AI_CHAT_VOICE_MAX_BYTES = 20 * 1024 * 1024



def _project_ready_keyboard(project_id: str, version_number: int) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "✏️ Редактировать сайт", "callback_data": f"site:edit:start:{project_id}"}],
            [{"text": f"📦 Скачать v{int(version_number)}", "callback_data": f"site:download:{project_id}:{int(version_number)}"}],
            [{"text": "🗂 Мои сайты", "callback_data": "site:projects"}],
        ]
    }


async def tg_send_message(chat_id: int, text: str, *, reply_markup: Optional[dict] = None) -> Optional[int]:
    if not TG_API:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
    payload: Dict[str, Any] = {"chat_id": int(chat_id), "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(f"{TG_API}/sendMessage", json=payload)
    try:
        data = response.json()
        if isinstance(data, dict) and data.get("ok"):
            return int((data.get("result") or {}).get("message_id") or 0) or None
    except Exception:
        pass
    return None


async def tg_delete_message(chat_id: int, message_id: Optional[int]) -> None:
    if not TG_API or not message_id:
        return
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            await client.post(f"{TG_API}/deleteMessage", json={"chat_id": int(chat_id), "message_id": int(message_id)})
    except Exception:
        pass


async def tg_send_chat_action(chat_id: int, action: str = "typing") -> None:
    if not TG_API:
        return
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            await client.post(f"{TG_API}/sendChatAction", json={"chat_id": int(chat_id), "action": action})
    except Exception:
        pass


async def tg_get_file_path(file_id: str) -> str:
    if not TG_API:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(f"{TG_API}/getFile", params={"file_id": file_id})
    response.raise_for_status()
    payload = response.json()
    return str((payload.get("result") or {}).get("file_path") or "").strip()


async def tg_download_file_bytes(file_path: str) -> bytes:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
    if not file_path:
        raise RuntimeError("Telegram не вернул file_path для голосового.")
    url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.get(url)
    response.raise_for_status()
    return response.content


async def _ffmpeg_convert_audio_to_mp3(audio_bytes: bytes) -> bytes:
    """Convert Telegram voice audio to compact MP3 for OpenAI STT."""
    if not audio_bytes:
        raise RuntimeError("Пустой аудиофайл.")

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-b:a",
        "48k",
        "-f",
        "mp3",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(audio_bytes), timeout=45)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        raise RuntimeError("ffmpeg не успел подготовить голосовое сообщение.")

    if proc.returncode != 0 or not stdout:
        err = (stderr or b"").decode("utf-8", "ignore")[:700]
        raise RuntimeError(f"ffmpeg не смог подготовить голосовое сообщение: {err or 'unknown error'}")
    return stdout


async def openai_transcribe_audio_bytes(
    audio_bytes: bytes,
    *,
    filename: str = "voice.mp3",
    mime_type: str = "audio/mpeg",
) -> str:
    """Speech-to-text for Telegram AI-chat voice messages."""
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY не задан в переменных окружения.")
    if not audio_bytes:
        raise RuntimeError("Пустой аудиофайл.")

    data = {
        "model": OPENAI_TRANSCRIBE_MODEL,
        "response_format": "json",
    }
    if AI_CHAT_VOICE_LANGUAGE:
        data["language"] = AI_CHAT_VOICE_LANGUAGE

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    files = {"file": (filename, audio_bytes, mime_type)}

    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers=headers,
            data=data,
            files=files,
        )

    if response.status_code >= 300:
        raise RuntimeError(f"OpenAI STT error {response.status_code}: {response.text[:1200]}")

    try:
        payload = response.json()
    except Exception:
        payload = {}
    text = str((payload or {}).get("text") or "").strip()
    if not text:
        raise RuntimeError("OpenAI STT вернул пустой текст.")
    return text


async def transcribe_tg_voice_to_text(file_id: str) -> str:
    """Download Telegram voice by file_id, convert it, and return recognized text."""
    file_path = await tg_get_file_path(file_id)
    raw_audio = await tg_download_file_bytes(file_path)
    if len(raw_audio) > AI_CHAT_VOICE_MAX_BYTES:
        mb = max(1, AI_CHAT_VOICE_MAX_BYTES // (1024 * 1024))
        raise RuntimeError(f"Голосовое слишком большое. Лимит: до {mb} МБ.")

    try:
        mp3_audio = await _ffmpeg_convert_audio_to_mp3(raw_audio)
        return await openai_transcribe_audio_bytes(mp3_audio, filename="voice.mp3", mime_type="audio/mpeg")
    except Exception as convert_error:
        # Fallback: try original Telegram file. Useful if ffmpeg is temporarily unavailable.
        try:
            return await openai_transcribe_audio_bytes(raw_audio, filename="voice.ogg", mime_type="audio/ogg")
        except Exception:
            raise convert_error


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or default)
    except Exception:
        return int(default)


def _seedance25_load_task_id_from_ledger(charge_ref_id: str) -> str:
    """Durable Telegram recovery fallback using the existing charge ledger row.

    No schema migration is needed: the provider task id is merged into the JSONB
    ``meta`` of the original ``seedance25_video`` charge identified by its UUID
    ``ref_id``.  Redis remains the fast state store; Supabase is an independent
    durable copy so a Render restart cannot normally cause a second createTask.
    """
    ref = str(charge_ref_id or "").strip()
    if not ref or billing_supabase is None:
        return ""
    try:
        response = (
            billing_supabase.table("bot_balance_ledger")
            .select("id,meta")
            .eq("reason", "seedance25_video")
            .eq("ref_id", ref)
            .limit(1)
            .execute()
        )
        rows = list(getattr(response, "data", None) or [])
        if not rows:
            return ""
        meta = (rows[0] or {}).get("meta")
        if not isinstance(meta, dict):
            return ""
        return str(meta.get("provider_task_id") or "").strip()
    except Exception as exc:
        print(f"[redactor/seedance25] ledger taskId read failed ref={ref}: {exc}", flush=True)
        return ""


def _seedance25_persist_task_id_to_ledger(charge_ref_id: str, task_id: str, job_id: str) -> bool:
    """Persist KIE taskId into the already-existing charge ledger row."""
    ref = str(charge_ref_id or "").strip()
    task = str(task_id or "").strip()
    if not ref or not task or billing_supabase is None:
        return False
    try:
        response = (
            billing_supabase.table("bot_balance_ledger")
            .select("id,meta")
            .eq("reason", "seedance25_video")
            .eq("ref_id", ref)
            .limit(1)
            .execute()
        )
        rows = list(getattr(response, "data", None) or [])
        if not rows:
            return False
        row = rows[0] or {}
        row_id = str(row.get("id") or "").strip()
        if not row_id:
            return False
        meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
        merged = dict(meta)
        merged.update({
            "provider": "kie",
            "provider_task_id": task,
            "seedance25_job_id": str(job_id or ""),
            "provider_task_saved_at": int(time.time()),
        })
        billing_supabase.table("bot_balance_ledger").update({"meta": merged}).eq("id", row_id).execute()
        return _seedance25_load_task_id_from_ledger(ref) == task
    except Exception as exc:
        print(f"[redactor/seedance25] ledger taskId persist failed ref={ref}: {exc}", flush=True)
        return False


async def _seedance25_persist_tg_task_id_reliably(*, job: Dict[str, Any], task_id: str, charge_ref_id: str) -> None:
    """Fail closed until taskId reaches Redis or Supabase.

    The KIE task already exists at this point, so proceeding without any durable
    copy is unsafe.  We keep retrying (and remain cancellable on shutdown) until
    at least one independent store has the id.  Normally both are written.
    """
    task = str(task_id or "").strip()
    job_id = str(job.get("job_id") or "").strip()
    if not task:
        raise RuntimeError("Seedance 2.5 KIE returned an empty taskId")
    job["provider_task_id"] = task
    attempt = 0
    while True:
        attempt += 1
        db_ok = await asyncio.to_thread(
            _seedance25_persist_task_id_to_ledger,
            charge_ref_id,
            task,
            job_id,
        )
        redis_ok = False
        if job_id:
            try:
                await set_reliable_job_state(
                    queue_name=SEEDANCE25_QUEUE_NAME,
                    job_id=job_id,
                    updates={"task_id": task, "phase": "provider_running"},
                )
                redis_ok = True
            except Exception as exc:
                print(
                    f"[redactor/seedance25] Redis taskId persist retry job={job_id} attempt={attempt}: {exc}",
                    flush=True,
                )
        if db_ok or redis_ok:
            if not (db_ok and redis_ok):
                print(
                    f"[redactor/seedance25] taskId persisted with degraded redundancy "
                    f"job={job_id} redis={redis_ok} supabase={db_ok}",
                    flush=True,
                )
            return
        if attempt == 1 or attempt % 12 == 0:
            print(
                f"[redactor/seedance25] CRITICAL: taskId not durable yet; retrying "
                f"job={job_id} taskId={task} attempt={attempt}",
                flush=True,
            )
        await asyncio.sleep(min(5.0, 1.0 + attempt * 0.25))


def _tg_chat_queue_for_model(model_key: Any) -> str:
    key = str(model_key or "claude").strip().lower()
    if key in {"openai", "chatgpt", "gpt"}:
        return TG_CHAT_OPENAI_QUEUE_NAME
    if key in {"fable", "claude_fable", "claude-fable", "claude-fable-5", "fable-5"}:
        return TG_CHAT_FABLE_QUEUE_NAME
    return TG_CHAT_CLAUDE_QUEUE_NAME


def _release_or_refund_tg_stt_job(job: Dict[str, Any], *, stage: str, error: str = "") -> None:
    user_id = _safe_int(job.get("user_id"))
    charge_tokens = _safe_int(job.get("charge_tokens"))
    charge_ref_id = str(job.get("charge_ref_id") or "").strip()
    refund_reason = str(job.get("refund_reason") or "claude_fable_chat_refund").strip() or "claude_fable_chat_refund"
    if user_id <= 0:
        return
    if charge_tokens > 0 and charge_ref_id:
        try:
            add_tokens(
                user_id,
                charge_tokens,
                reason=refund_reason,
                ref_id=charge_ref_id,
                meta={"stage": stage, "source": "telegram_voice_stt", "job_id": str(job.get("job_id") or ""), "error": error[:300]},
            )
        except TypeError:
            try:
                add_tokens(user_id, charge_tokens, reason=refund_reason)
            except Exception:
                pass
        except Exception:
            pass
        return

    if bool(job.get("free_chat_consumed")):
        try:
            release_free_usage(user_id, FEATURE_CHAT)
        except Exception:
            pass


async def _enqueue_recognized_voice_to_chat(job: Dict[str, Any], recognized_text: str) -> str:
    chat_id = _safe_int(job.get("chat_id"))
    user_id = _safe_int(job.get("user_id"))
    if chat_id <= 0 or user_id <= 0:
        raise RuntimeError("tg_stt job missing chat_id/user_id")

    model_key = str(job.get("model_key") or "claude").strip() or "claude"
    reply_markup = job.get("reply_markup") if isinstance(job.get("reply_markup"), dict) else None
    status_message_id: Optional[int] = None
    try:
        status_message_id = await tg_send_message(chat_id, "⏳ Думаю...")
    except Exception:
        status_message_id = None

    chat_job: Dict[str, Any] = {
        "job_id": f"tg_ai_chat_stt_{uuid4().hex}",
        "kind": "tg_ai_chat",
        "chat_id": int(chat_id),
        "user_id": int(user_id),
        "text": str(recognized_text or ""),
        "model_key": model_key,
        "model": str(job.get("model") or "").strip(),
        "system_prompt": str(job.get("system_prompt") or "").strip(),
        "thinking": bool(job.get("thinking", True)),
        "charge_tokens": _safe_int(job.get("charge_tokens")),
        "charge_ref_id": str(job.get("charge_ref_id") or "").strip(),
        "refund_reason": str(job.get("refund_reason") or "").strip(),
        "reply_markup": reply_markup,
        "source": "worker_redactor.py:tg_stt",
        "stt_source_job_id": str(job.get("job_id") or ""),
    }
    if status_message_id:
        chat_job["status_message_id"] = int(status_message_id)

    try:
        queue_name = _tg_chat_queue_for_model(model_key)
        await enqueue_job(chat_job, queue_name=queue_name)
        return str(chat_job["job_id"])
    except Exception:
        await tg_delete_message(chat_id, status_message_id)
        raise


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
        # Use the caller's model-specific caption. The old hard-coded Kling text
        # caused Seedance 2.5 fallback messages to be mislabeled.
        ready_text = str(caption or "✅ Видео готово").strip()
        await tg_send_message(chat_id, f"{ready_text}. Видео: {video_url}")




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


async def _download_tg_file_id(file_id: str) -> bytes:
    file_path = await tg_get_file_path(str(file_id or ""))
    return await tg_download_file_bytes(file_path)


async def _normalize_seedance25_audio(raw: bytes) -> bytes:
    """KIE Seedance 2.5 accepts MP3/WAV refs; normalize Telegram voice/audio to MP3."""
    if not raw:
        return raw
    if raw.startswith(b"ID3") or (len(raw) > 2 and raw[0] == 0xFF and (raw[1] & 0xE0) == 0xE0):
        return raw
    if raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
        return raw
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
        "-vn", "-ac", "2", "-ar", "44100", "-b:a", "128k", "-f", "mp3", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(raw), timeout=90)
    if proc.returncode != 0 or not stdout:
        raise RuntimeError(f"Не удалось подготовить audio reference для Seedance 2.5: {(stderr or b'').decode('utf-8','ignore')[:500]}")
    return stdout


async def _seedance25_refund_once(user_id: int, charge_tokens: int, refund_reason: str, charge_ref_id: str, meta: Dict[str, Any]) -> bool:
    if int(charge_tokens or 0) <= 0 or int(user_id or 0) <= 0:
        return False
    ref_id = str(charge_ref_id or "").strip()
    if not ref_id:
        stable_job_id = str((meta or {}).get("job_id") or "").strip()
        if stable_job_id:
            ref_id = str(uuid5(NAMESPACE_URL, f"nabex:seedance25:refund:{stable_job_id}"))
    if not ref_id:
        raise RuntimeError("Seedance 2.5 refund requires stable charge_ref_id/job_id")
    return await asyncio.to_thread(
        refund_seedance25_once,
        int(user_id),
        int(charge_tokens),
        reason=str(refund_reason),
        ref_id=ref_id,
        meta=dict(meta or {}),
    )


class _Seedance25DeliveryPending(RuntimeError):
    """Provider succeeded, but Telegram delivery still needs retrying."""


async def _handle_seedance25(job: Dict[str, Any]) -> None:
    job_id = str(job.get("job_id") or "").strip()
    user_id = _safe_int(job.get("user_id"))
    charge_tokens = _safe_int(job.get("charge_tokens"))
    charge_ref_id = str(job.get("charge_ref_id") or "").strip()
    refund_reason = str(job.get("refund_reason") or "seedance25_video_refund").strip() or "seedance25_video_refund"
    state = await get_reliable_job_state(queue_name=SEEDANCE25_QUEUE_NAME, job_id=job_id) if job_id else {}
    if str(state.get("terminal") or "").lower() in {"completed", "failed"}:
        print(f"[redactor/seedance25] skip terminal replay job={job_id} terminal={state.get('terminal')}", flush=True)
        return

    # If a previous attempt reached the refund stage but died before ACK, finish
    # the refund idempotently instead of starting a free second generation.
    if str(state.get("phase") or "").lower() == "failing":
        await _seedance25_refund_once(user_id, charge_tokens, refund_reason, charge_ref_id, {
            "origin": "seedance25", "job_id": job_id, "stage": "recovered_failing"
        })
        if job_id:
            await set_reliable_job_state(queue_name=SEEDANCE25_QUEUE_NAME, job_id=job_id, updates={"terminal": "failed", "phase": "failed"})
        return

    existing_task_id = str(state.get("task_id") or job.get("provider_task_id") or "").strip()
    if not existing_task_id and charge_ref_id:
        existing_task_id = await asyncio.to_thread(_seedance25_load_task_id_from_ledger, charge_ref_id)
        if existing_task_id:
            job["provider_task_id"] = existing_task_id
            if job_id:
                try:
                    await set_reliable_job_state(
                        queue_name=SEEDANCE25_QUEUE_NAME,
                        job_id=job_id,
                        updates={"task_id": existing_task_id, "phase": "provider_running"},
                    )
                except Exception:
                    pass
            print(f"[redactor/seedance25] recovered taskId from billing ledger job={job_id}", flush=True)

    if str(job.get("kind") or "").strip().lower() == "workspace_video_run":
        async def _workspace_task_id_cb(task_id: str) -> None:
            job["provider_task_id"] = str(task_id)
            if job_id:
                await set_reliable_job_state(queue_name=SEEDANCE25_QUEUE_NAME, job_id=job_id, updates={"task_id": str(task_id), "phase": "provider_running"})

        if existing_task_id:
            job["resume_task_id"] = existing_task_id
        try:
            ok = await process_workspace_video_job(job, on_provider_task_id=_workspace_task_id_cb)
            if not ok:
                if job_id:
                    await set_reliable_job_state(queue_name=SEEDANCE25_QUEUE_NAME, job_id=job_id, updates={"phase": "failing"})
                await _seedance25_refund_once(user_id, charge_tokens, refund_reason, charge_ref_id, {
                    "origin": "workspace_seedance25", "job_id": job_id, "stage": "workspace_result_failed"
                })
            if job_id:
                await set_reliable_job_state(
                    queue_name=SEEDANCE25_QUEUE_NAME,
                    job_id=job_id,
                    updates={"terminal": "completed" if ok else "failed", "phase": "completed" if ok else "failed"},
                )
            print(f"[redactor/seedance25] completed workspace job={job_id} ok={ok}", flush=True)
        except Seedance25TaskPendingError as exc:
            # KIE task already exists or provider already returned a result. Keep
            # Stream entry pending; stale reclaim will resume the same taskId.
            if job_id:
                try:
                    await set_reliable_job_state(
                        queue_name=SEEDANCE25_QUEUE_NAME,
                        job_id=job_id,
                        updates={
                            "phase": "provider_running",
                            "task_id": str(job.get("provider_task_id") or existing_task_id or ""),
                            "error": str(exc)[:500],
                        },
                    )
                except Exception:
                    pass
            print(f"[redactor/seedance25] workspace task pending; no refund job={job_id}: {exc}", flush=True)
            raise
        except Exception as exc:
            # Defensive fail-closed rule: if any layer knows a taskId already
            # exists, an unexpected local error cannot prove provider failure.
            known_task_id = str(job.get("provider_task_id") or existing_task_id or "").strip()
            if known_task_id:
                if job_id:
                    try:
                        await set_reliable_job_state(
                            queue_name=SEEDANCE25_QUEUE_NAME,
                            job_id=job_id,
                            updates={"phase": "provider_running", "task_id": known_task_id, "error": str(exc)[:500]},
                        )
                    except Exception:
                        pass
                print(f"[redactor/seedance25] workspace post-task error; no refund job={job_id}: {exc}", flush=True)
                raise Seedance25TaskPendingError(str(exc)) from exc
            try:
                from app.routers import web_workspace_api as ww
                generation_id = str(job.get("generation_id") or "").strip()
                if generation_id:
                    ww._mark_workspace_generation_failed(generation_id, str(exc), error_code="worker_error")
            except Exception:
                pass
            if job_id:
                await set_reliable_job_state(queue_name=SEEDANCE25_QUEUE_NAME, job_id=job_id, updates={"phase": "failing", "error": str(exc)[:500]})
            try:
                await _seedance25_refund_once(user_id, charge_tokens, refund_reason, charge_ref_id, {
                    "origin": "workspace_seedance25", "job_id": job_id, "stage": "worker_download", "error": str(exc)[:300]
                })
            except Exception as refund_exc:
                print(f"[redactor/seedance25] workspace refund pending job={job_id} error={refund_exc}", flush=True)
                raise
            if job_id:
                await set_reliable_job_state(queue_name=SEEDANCE25_QUEUE_NAME, job_id=job_id, updates={"terminal": "failed", "phase": "failed"})
            print(f"[redactor/seedance25] failed workspace job={job_id} error={exc}", flush=True)
        return

    chat_id = _safe_int(job.get("chat_id"))
    mode = str(job.get("mode") or "text_to_video").strip().lower()
    prompt = str(job.get("prompt") or "").strip()
    model = str(job.get("seedance_model") or job.get("model") or "seedance25-720p").strip()
    duration = _safe_int(job.get("duration"), 5)
    aspect_ratio = str(job.get("aspect_ratio") or "adaptive").strip()
    video_url = str(state.get("video_url") or "").strip()
    provider_completed = bool(video_url)

    async def _task_id_cb(task_id: str) -> None:
        print(f"[redactor/seedance25] job={job_id} taskId={task_id}", flush=True)
        await _seedance25_persist_tg_task_id_reliably(
            job=job,
            task_id=str(task_id),
            charge_ref_id=charge_ref_id,
        )

    # Provider stage. Only failures before a final KIE video URL exists are
    # refundable. Delivery/storage errors after success must never create a free
    # generation, because KIE has already charged us.
    if not provider_completed:
        try:
            if existing_task_id:
                video_url = await wait_seedance25_task(existing_task_id)
            elif mode == "omni_reference":
                image_urls = []
                video_urls = []
                audio_urls = []

                for idx, file_id in enumerate((job.get("image_file_ids") or []), start=1):
                    if not str(file_id or "").strip():
                        continue
                    raw = await _download_tg_file_id(file_id)
                    try:
                        image_urls.append(await asyncio.to_thread(upload_seedance25_reference_bytes, user_id, "image", idx, raw))
                    finally:
                        del raw

                for idx, file_id in enumerate((job.get("video_file_ids") or []), start=1):
                    if not str(file_id or "").strip():
                        continue
                    raw = await _download_tg_file_id(file_id)
                    try:
                        video_urls.append(await asyncio.to_thread(upload_seedance25_reference_bytes, user_id, "video", idx, raw))
                    finally:
                        del raw

                for idx, file_id in enumerate((job.get("audio_file_ids") or []), start=1):
                    if not str(file_id or "").strip():
                        continue
                    raw = await _download_tg_file_id(file_id)
                    normalized = None
                    try:
                        normalized = await _normalize_seedance25_audio(raw)
                        audio_urls.append(await asyncio.to_thread(upload_seedance25_reference_bytes, user_id, "audio", idx, normalized))
                    finally:
                        if normalized is not None:
                            del normalized
                        del raw

                video_url = await run_seedance25_omni_reference_urls(
                    user_id=user_id,
                    model=model,
                    prompt=prompt,
                    duration=duration,
                    aspect_ratio=aspect_ratio,
                    reference_image_urls=image_urls,
                    reference_video_urls=video_urls,
                    reference_audio_urls=audio_urls,
                    on_task_id=_task_id_cb,
                )
            else:
                video_url = await run_seedance25_text_to_video(
                    model=model,
                    prompt=prompt,
                    duration=duration,
                    aspect_ratio=aspect_ratio,
                    on_task_id=_task_id_cb,
                )
            provider_completed = bool(str(video_url or "").strip())
            if not provider_completed:
                raise RuntimeError("Seedance 2.5 provider completed without a video URL")
        except Seedance25TaskPendingError as exc:
            known_task_id = str(job.get("provider_task_id") or existing_task_id or "").strip()
            if job_id:
                try:
                    await set_reliable_job_state(
                        queue_name=SEEDANCE25_QUEUE_NAME,
                        job_id=job_id,
                        updates={"phase": "provider_running", "task_id": known_task_id, "error": str(exc)[:500]},
                    )
                except Exception:
                    pass
            print(f"[redactor/seedance25] provider polling pending; no refund job={job_id}: {exc}", flush=True)
            raise
        except Seedance25TaskFailedError as exc:
            if job_id:
                await set_reliable_job_state(
                    queue_name=SEEDANCE25_QUEUE_NAME,
                    job_id=job_id,
                    updates={"phase": "failing", "error": str(exc)[:500]},
                )
            try:
                await _seedance25_refund_once(
                    user_id, charge_tokens, refund_reason, charge_ref_id,
                    {"origin": "seedance25", "job_id": job_id, "stage": "provider_terminal_fail", "error": str(exc)[:300]},
                )
            except Exception as refund_exc:
                print(f"[redactor/seedance25] refund pending job={job_id} error={refund_exc}", flush=True)
                raise
            if job_id:
                await set_reliable_job_state(
                    queue_name=SEEDANCE25_QUEUE_NAME, job_id=job_id,
                    updates={"terminal": "failed", "phase": "failed"},
                )
            if chat_id:
                try:
                    await tg_send_message(chat_id, f"❌ Seedance 2.5: ошибка генерации. Токены возвращены.\n{str(exc)[:700]}")
                except Exception:
                    pass
            print(f"[redactor/seedance25] terminal provider failure job={job_id} error={exc}", flush=True)
            return
        except Exception as exc:
            known_task_id = str(job.get("provider_task_id") or existing_task_id or "").strip()
            if known_task_id:
                if job_id:
                    try:
                        await set_reliable_job_state(
                            queue_name=SEEDANCE25_QUEUE_NAME,
                            job_id=job_id,
                            updates={"phase": "provider_running", "task_id": known_task_id, "error": str(exc)[:500]},
                        )
                    except Exception:
                        pass
                print(f"[redactor/seedance25] unexpected post-task error; no refund job={job_id}: {exc}", flush=True)
                raise Seedance25TaskPendingError(str(exc)) from exc
            if job_id:
                await set_reliable_job_state(
                    queue_name=SEEDANCE25_QUEUE_NAME,
                    job_id=job_id,
                    updates={"phase": "failing", "error": str(exc)[:500]},
                )
            try:
                await _seedance25_refund_once(
                    user_id,
                    charge_tokens,
                    refund_reason,
                    charge_ref_id,
                    {"origin": "seedance25", "job_id": job_id, "error": str(exc)[:300]},
                )
            except Exception as refund_exc:
                print(f"[redactor/seedance25] refund pending job={job_id} error={refund_exc}", flush=True)
                raise
            if job_id:
                await set_reliable_job_state(
                    queue_name=SEEDANCE25_QUEUE_NAME,
                    job_id=job_id,
                    updates={"terminal": "failed", "phase": "failed"},
                )
            if chat_id:
                try:
                    await tg_send_message(chat_id, f"❌ Seedance 2.5: ошибка генерации. Токены возвращены.\n{str(exc)[:700]}")
                except Exception:
                    pass
            print(f"[redactor/seedance25] failed provider job={job_id} error={exc}", flush=True)
            return

    # KIE succeeded. From this point forward there is deliberately NO refund.
    # Persist the result before trying Telegram so a reclaimed job can retry only
    # delivery instead of calling createTask again.
    video_url = str(video_url or "").strip()
    delivery_attempts = _safe_int(state.get("delivery_attempts"), 0)
    if job_id:
        try:
            await set_reliable_job_state(
                queue_name=SEEDANCE25_QUEUE_NAME,
                job_id=job_id,
                updates={
                    "phase": "delivery_pending",
                    "video_url": video_url[:1500],
                    "delivery_attempts": delivery_attempts,
                },
            )
        except Exception as state_exc:
            print(f"[redactor/seedance25] result state persist warning job={job_id}: {state_exc}", flush=True)

    if chat_id:
        try:
            await tg_send_video_url(chat_id, video_url, caption="✅ Seedance 2.5 готов")
        except Exception as delivery_exc:
            delivery_attempts += 1
            if job_id:
                try:
                    await set_reliable_job_state(
                        queue_name=SEEDANCE25_QUEUE_NAME,
                        job_id=job_id,
                        updates={
                            "phase": "delivery_pending",
                            "video_url": video_url[:1500],
                            "delivery_attempts": delivery_attempts,
                            "delivery_error": str(delivery_exc)[:500],
                        },
                    )
                except Exception:
                    pass
            if delivery_attempts >= SEEDANCE25_DELIVERY_MAX_ATTEMPTS:
                if job_id:
                    try:
                        await set_reliable_job_state(
                            queue_name=SEEDANCE25_QUEUE_NAME,
                            job_id=job_id,
                            updates={
                                "terminal": "completed",
                                "phase": "delivery_failed",
                                "video_url": video_url[:1500],
                                "delivery_attempts": delivery_attempts,
                                "delivery_error": str(delivery_exc)[:500],
                            },
                        )
                    except Exception:
                        pass
                print(
                    f"[redactor/seedance25] provider completed but Telegram delivery exhausted "
                    f"job={job_id} attempts={delivery_attempts} url={video_url[:300]} error={delivery_exc}",
                    flush=True,
                )
                return
            print(
                f"[redactor/seedance25] Telegram delivery pending job={job_id} "
                f"attempt={delivery_attempts}/{SEEDANCE25_DELIVERY_MAX_ATTEMPTS}: {delivery_exc}",
                flush=True,
            )
            raise _Seedance25DeliveryPending(str(delivery_exc)) from delivery_exc

    if job_id:
        try:
            await set_reliable_job_state(
                queue_name=SEEDANCE25_QUEUE_NAME,
                job_id=job_id,
                updates={
                    "terminal": "completed",
                    "phase": "completed",
                    "video_url": video_url[:1500],
                    "delivery_attempts": delivery_attempts,
                },
            )
        except Exception as state_exc:
            # Delivery already succeeded, so never retry/refund just because the
            # auxiliary Redis state write failed. The Stream ACK is enough.
            print(f"[redactor/seedance25] completed state write warning job={job_id}: {state_exc}", flush=True)
    print(f"[redactor/seedance25] completed job={job_id}", flush=True)


async def _handle_tg_stt(job: Dict[str, Any], sem: asyncio.Semaphore) -> None:
    async with sem:
        job_id = str(job.get("job_id") or "").strip()
        chat_id = _safe_int(job.get("chat_id"))
        file_id = str(job.get("file_id") or "").strip()

        try:
            if not file_id:
                raise RuntimeError("tg_stt job missing file_id")
            if chat_id > 0:
                await tg_send_chat_action(chat_id, "typing")

            recognized_text = await transcribe_tg_voice_to_text(file_id)
            if not recognized_text:
                raise RuntimeError("Не смог распознать текст в голосовом.")
        except Exception as exc:
            _release_or_refund_tg_stt_job(job, stage="tg_stt_failed", error=str(exc))
            try:
                if chat_id > 0:
                    await tg_send_message(chat_id, f"❌ Не смог распознать голосовое: {str(exc)[:800]}")
            except Exception:
                pass
            print(f"[redactor/tg_stt] failed job={job_id} error={exc}", flush=True)
            return

        try:
            chat_job_id = await _enqueue_recognized_voice_to_chat(job, recognized_text)
        except Exception as exc:
            _release_or_refund_tg_stt_job(job, stage="tg_stt_chat_enqueue_failed", error=str(exc))
            try:
                if chat_id > 0:
                    await tg_send_message(chat_id, f"❌ Голосовое распознал, но не смог поставить ИИ-чат в очередь. Проверь REDIS_URL и worker_chat.py.\n{str(exc)[:800]}")
            except Exception:
                pass
            print(f"[redactor/tg_stt] chat enqueue failed job={job_id} error={exc}", flush=True)
            return

        print(
            f"[redactor/tg_stt] completed job={job_id} text_len={len(recognized_text)} chat_job={chat_job_id}",
            flush=True,
        )


async def _tg_stt_loop() -> None:
    sem = asyncio.Semaphore(TG_STT_CONCURRENCY)
    tasks: set[asyncio.Task] = set()
    while True:
        job = await dequeue_job(timeout_sec=10, queue_name=TG_STT_QUEUE_NAME)
        if job:
            tasks.add(asyncio.create_task(_handle_tg_stt(job, sem)))
        done = {t for t in tasks if t.done()}
        tasks -= done


class _Seedance25LeaseLost(RuntimeError):
    pass


async def _seedance25_lease_heartbeat(job: Dict[str, Any]) -> None:
    while True:
        await asyncio.sleep(SEEDANCE25_RELIABLE_TOUCH_SEC)
        owned = await touch_reliable_job(queue_name=SEEDANCE25_QUEUE_NAME, job=job)
        if owned is False:
            raise _Seedance25LeaseLost(f"Seedance 2.5 reliable lease lost for job={job.get('job_id')}")
        # owned=None means a transient Redis problem. Do not kill a valid provider
        # request immediately; if the outage lasts, the stream entry will be
        # reclaimed after the configured stale interval and this worker will then
        # observe ownership loss on the next successful heartbeat.


async def _seedance25_worker_slot(slot: int) -> None:
    consumer = f"{os.getenv('HOSTNAME', 'worker')}-{os.getpid()}-slot{slot}"
    while True:
        job: Optional[Dict[str, Any]] = None
        heartbeat: Optional[asyncio.Task] = None
        handler_task: Optional[asyncio.Task] = None
        try:
            job = await dequeue_reliable_job(
                queue_name=SEEDANCE25_QUEUE_NAME,
                consumer_name=consumer,
                timeout_sec=10,
                stale_after_sec=SEEDANCE25_RELIABLE_STALE_SEC,
            )
            if not job:
                continue
            heartbeat = asyncio.create_task(_seedance25_lease_heartbeat(job))
            handler_task = asyncio.create_task(_handle_seedance25(job))
            done, _pending = await asyncio.wait({handler_task, heartbeat}, return_when=asyncio.FIRST_COMPLETED)

            if heartbeat in done:
                lease_exc = heartbeat.exception()
                if lease_exc is not None:
                    if handler_task and not handler_task.done():
                        handler_task.cancel()
                        try:
                            await handler_task
                        except asyncio.CancelledError:
                            pass
                    if isinstance(lease_exc, _Seedance25LeaseLost):
                        print(f"[redactor/seedance25] lease lost; stop old worker slot={slot} job={job.get('job_id')}", flush=True)
                        # Do not ACK/requeue: ownership already belongs to another
                        # consumer (or the entry was completed there).
                        job = None
                        heartbeat = None
                        handler_task = None
                        continue
                    raise lease_exc

            # Handler finished first. Propagate a real handler exception before ACK.
            await handler_task
            handler_task = None
            if heartbeat:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass
                heartbeat = None
            acked = await ack_reliable_job(queue_name=SEEDANCE25_QUEUE_NAME, job=job)
            if acked:
                job_id = str(job.get("job_id") or "").strip()
                if job_id:
                    await delete_reliable_job_state(queue_name=SEEDANCE25_QUEUE_NAME, job_id=job_id)
            job = None
        except asyncio.CancelledError:
            if heartbeat:
                heartbeat.cancel()
            if handler_task and not handler_task.done():
                handler_task.cancel()
                try:
                    await handler_task
                except asyncio.CancelledError:
                    pass
            if job:
                try:
                    await asyncio.shield(requeue_reliable_job(queue_name=SEEDANCE25_QUEUE_NAME, job=job))
                    print(f"[redactor/seedance25] requeued active reliable job on shutdown slot={slot} job={job.get('job_id')}", flush=True)
                except Exception as requeue_exc:
                    print(f"[redactor/seedance25] FAILED to requeue shutdown job slot={slot}: {requeue_exc}", flush=True)
            raise
        except Exception as exc:
            if heartbeat:
                heartbeat.cancel()
            if handler_task and not handler_task.done():
                handler_task.cancel()
            print(f"[redactor/seedance25] worker slot={slot} loop error={exc}", flush=True)
            await asyncio.sleep(1.0)


async def _seedance25_loop() -> None:
    await asyncio.gather(*(
        _seedance25_worker_slot(slot) for slot in range(1, SEEDANCE25_CONCURRENCY + 1)
    ))


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
        f"kling_queue={KLING3_KIE_QUEUE_NAME} kling_concurrency={KLING3_KIE_WORKER_CONCURRENCY} "
        f"tg_stt_queue={TG_STT_QUEUE_NAME} tg_stt_concurrency={TG_STT_CONCURRENCY} "
        f"tg_chat_queues={TG_CHAT_OPENAI_QUEUE_NAME},{TG_CHAT_CLAUDE_QUEUE_NAME},{TG_CHAT_FABLE_QUEUE_NAME} "
        f"seedance25_queue={SEEDANCE25_QUEUE_NAME} seedance25_concurrency={SEEDANCE25_CONCURRENCY}",
        flush=True,
    )
    await asyncio.gather(_site_loop(), _kling_loop(), _tg_stt_loop(), _seedance25_loop())


if __name__ == "__main__":
    asyncio.run(main())
