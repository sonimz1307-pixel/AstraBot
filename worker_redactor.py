from __future__ import annotations

import asyncio
import json
import os
import time
from uuid import uuid4
from typing import Any, Dict, Optional

import httpx

from app.services.site_builder_service import process_site_job
from billing_db import add_tokens
from free_plan_limits import FEATURE_CHAT, release_free_usage
from kling3_kie_runner import run_kling3_kie_task_and_wait
from queue_redis import dequeue_job, enqueue_job

SITE_QUEUE_NAME = (os.getenv("SITE_QUEUE_NAME", "site") or "site").strip() or "site"
KLING3_KIE_QUEUE_NAME = (os.getenv("KLING3_KIE_QUEUE_NAME", "kling3_kie") or "kling3_kie").strip() or "kling3_kie"
TG_STT_QUEUE_NAME = (os.getenv("TG_STT_QUEUE_NAME", "redactor_tg_stt") or "redactor_tg_stt").strip() or "redactor_tg_stt"
TG_CHAT_OPENAI_QUEUE_NAME = (os.getenv("TG_CHAT_OPENAI_QUEUE_NAME", "tg_chat_openai") or "tg_chat_openai").strip() or "tg_chat_openai"
TG_CHAT_CLAUDE_QUEUE_NAME = (os.getenv("TG_CHAT_CLAUDE_QUEUE_NAME", "tg_chat_claude") or "tg_chat_claude").strip() or "tg_chat_claude"
TG_CHAT_FABLE_QUEUE_NAME = (os.getenv("TG_CHAT_FABLE_QUEUE_NAME", "tg_chat_fable") or "tg_chat_fable").strip() or "tg_chat_fable"
SITE_WORKER_CONCURRENCY = max(1, int(os.getenv("SITE_WORKER_CONCURRENCY", "1") or "1"))
KLING3_KIE_WORKER_CONCURRENCY = max(1, int(os.getenv("KLING3_KIE_WORKER_CONCURRENCY", "3") or "3"))
TG_STT_CONCURRENCY = max(1, int(os.getenv("TG_STT_CONCURRENCY", "2") or "2"))
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
        f"tg_chat_queues={TG_CHAT_OPENAI_QUEUE_NAME},{TG_CHAT_CLAUDE_QUEUE_NAME},{TG_CHAT_FABLE_QUEUE_NAME}",
        flush=True,
    )
    await asyncio.gather(_site_loop(), _kling_loop(), _tg_stt_loop())


if __name__ == "__main__":
    asyncio.run(main())
