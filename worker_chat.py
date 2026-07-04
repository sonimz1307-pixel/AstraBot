from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import uuid
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ai_chat import openai_chat_answer
from chat_file_text import extract_file_text
from chat_attachment_storage import (
    CHAT_ATTACHMENTS_BUCKET,
    create_signed_url,
    download_chat_attachment_bytes,
    upload_chat_attachment_bytes,
)
from chat_job_store import set_chat_job_status
from chat_memory_redis import (
    AI_CHAT_HISTORY_MAX,
    add_tg_chat_turn,
    maybe_summarize_tg_chat_memory,
)
from kie_claude_chat import (
    KIE_CLAUDE_MODEL_ID,
    KIE_CLAUDE_FABLE_MODEL_ID,
    KIE_CLAUDE_FABLE_MAX_TOKENS,
    is_kie_claude_model,
    kie_claude_answer,
    kie_claude_display_name,
    kie_claude_history_messages_for_model,
    kie_claude_is_fable_model,
    kie_claude_max_tokens_for_model,
    kie_claude_summary_chars_for_model,
    normalize_kie_claude_model,
)
from queue_redis import dequeue_job, get_redis
from app.services.partner_program import apply_topup_event, bind_referral
from app.services.free_usage_events import log_free_usage_event_async
from billing_db import add_tokens
from db_supabase import supabase as sb

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else ""
TG_FILE = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else ""

TG_CHAT_OPENAI_QUEUE_NAME = (os.getenv("TG_CHAT_OPENAI_QUEUE_NAME", "tg_chat_openai") or "tg_chat_openai").strip() or "tg_chat_openai"
TG_CHAT_CLAUDE_QUEUE_NAME = (os.getenv("TG_CHAT_CLAUDE_QUEUE_NAME", "tg_chat_claude") or "tg_chat_claude").strip() or "tg_chat_claude"
TG_CHAT_FABLE_QUEUE_NAME = (os.getenv("TG_CHAT_FABLE_QUEUE_NAME", "tg_chat_fable") or "tg_chat_fable").strip() or "tg_chat_fable"
WORKSPACE_CHAT_OPENAI_QUEUE_NAME = (os.getenv("WORKSPACE_CHAT_OPENAI_QUEUE_NAME", "workspace_chat_openai") or "workspace_chat_openai").strip() or "workspace_chat_openai"
WORKSPACE_CHAT_CLAUDE_QUEUE_NAME = (os.getenv("WORKSPACE_CHAT_CLAUDE_QUEUE_NAME", "workspace_chat_claude") or "workspace_chat_claude").strip() or "workspace_chat_claude"
WORKSPACE_CHAT_FABLE_QUEUE_NAME = (os.getenv("WORKSPACE_CHAT_FABLE_QUEUE_NAME", "workspace_chat_fable") or "workspace_chat_fable").strip() or "workspace_chat_fable"
PARTNER_EVENTS_QUEUE_NAME = (os.getenv("PARTNER_EVENTS_QUEUE_NAME", "partner_events") or "partner_events").strip() or "partner_events"
TG_BROADCAST_QUEUE_NAME = (os.getenv("TG_BROADCAST_QUEUE_NAME", "tg_broadcast") or "tg_broadcast").strip() or "tg_broadcast"
ADMIN_IDS = set(
    int(x)
    for x in (os.getenv("ADMIN_IDS", "") or "").replace(";", ",").split(",")
    if x.strip().isdigit()
)

TG_CHAT_OPENAI_CONCURRENCY = int(os.getenv("TG_CHAT_OPENAI_CONCURRENCY", "3") or "3")
TG_CHAT_CLAUDE_CONCURRENCY = int(os.getenv("TG_CHAT_CLAUDE_CONCURRENCY", "2") or "2")
TG_CHAT_FABLE_CONCURRENCY = int(os.getenv("TG_CHAT_FABLE_CONCURRENCY", "3") or "3")
WORKSPACE_CHAT_OPENAI_CONCURRENCY = int(os.getenv("WORKSPACE_CHAT_OPENAI_CONCURRENCY", "3") or "3")
WORKSPACE_CHAT_CLAUDE_CONCURRENCY = int(os.getenv("WORKSPACE_CHAT_CLAUDE_CONCURRENCY", "2") or "2")
WORKSPACE_CHAT_FABLE_CONCURRENCY = int(os.getenv("WORKSPACE_CHAT_FABLE_CONCURRENCY", "3") or "3")
TG_BROADCAST_CONCURRENCY = int(os.getenv("TG_BROADCAST_CONCURRENCY", "1") or "1")
TG_BROADCAST_BATCH_SIZE = int(os.getenv("TG_BROADCAST_BATCH_SIZE", "1000") or "1000")
TG_BROADCAST_DELAY_SEC = float(os.getenv("TG_BROADCAST_DELAY_SEC", "0.05") or "0.05")
TG_BROADCAST_PROGRESS_EVERY = int(os.getenv("TG_BROADCAST_PROGRESS_EVERY", "500") or "500")

AI_CHAT_FILE_MAX_BYTES = int(os.getenv("AI_CHAT_FILE_MAX_BYTES", str(10 * 1024 * 1024)) or str(10 * 1024 * 1024))
AI_CHAT_FILE_TEXT_MAX_CHARS = int(os.getenv("AI_CHAT_FILE_TEXT_MAX_CHARS", "50000") or "50000")
CHAT_USER_LOCK_TTL_SEC = int(os.getenv("CHAT_USER_LOCK_TTL_SEC", "180") or "180")
CHAT_USER_LOCK_WAIT_SEC = float(os.getenv("CHAT_USER_LOCK_WAIT_SEC", "1.0") or "1.0")
CHAT_USER_LOCK_MAX_WAIT_SEC = float(os.getenv("CHAT_USER_LOCK_MAX_WAIT_SEC", "240") or "240")

TG_LONG_ANSWER_FILE_THRESHOLD = int(os.getenv("TG_LONG_ANSWER_FILE_THRESHOLD", "3500") or "3500")
TG_LONG_ANSWER_PREVIEW_CHARS = int(os.getenv("TG_LONG_ANSWER_PREVIEW_CHARS", "400") or "400")
TG_LONG_ANSWER_SIGNED_TTL_SEC = int(os.getenv("TG_LONG_ANSWER_SIGNED_TTL_SEC", str(7 * 24 * 3600)) or str(7 * 24 * 3600))

DEFAULT_TG_SYSTEM_PROMPT = (
    "Ты Claude Sonnet 5 внутри AstraBot. Отвечай на русском, кратко и по делу. "
    "Рассуждение включено, но не раскрывай внутренние рассуждения — сразу давай готовый ответ. "
    "Интернет выключен. Если нужны актуальные данные, честно скажи, что без интернета их нельзя проверить. "
    "Файлы анализируй только по тексту, который передал backend. Не используй LaTeX/TeX."
)

sem_tg_openai = asyncio.Semaphore(max(1, TG_CHAT_OPENAI_CONCURRENCY))
sem_tg_claude = asyncio.Semaphore(max(1, TG_CHAT_CLAUDE_CONCURRENCY))
sem_tg_fable = asyncio.Semaphore(max(1, TG_CHAT_FABLE_CONCURRENCY))
sem_workspace_openai = asyncio.Semaphore(max(1, WORKSPACE_CHAT_OPENAI_CONCURRENCY))
sem_workspace_claude = asyncio.Semaphore(max(1, WORKSPACE_CHAT_CLAUDE_CONCURRENCY))
sem_workspace_fable = asyncio.Semaphore(max(1, WORKSPACE_CHAT_FABLE_CONCURRENCY))
sem_tg_broadcast = asyncio.Semaphore(max(1, TG_BROADCAST_CONCURRENCY))


def _job_kind(job: Dict[str, Any]) -> str:
    return str(job.get("kind") or "").strip().lower()


def _job_model_key(job: Dict[str, Any]) -> str:
    model_key = str(job.get("model_key") or "").strip().lower()
    if model_key in {"openai", "chatgpt", "gpt"}:
        return "openai"
    if model_key in {"fable", "claude_fable", "claude-fable", "claude-fable-5"}:
        return "claude_fable"
    model_actual = normalize_kie_claude_model(job.get("model") or job.get("model_actual") or "")
    if kie_claude_is_fable_model(model_actual):
        return "claude_fable"
    return "claude"


def _sem_for_job(job: Dict[str, Any]) -> asyncio.Semaphore:
    kind = _job_kind(job)
    model_key = _job_model_key(job)
    if kind == "tg_broadcast":
        return sem_tg_broadcast
    if kind == "tg_ai_chat":
        if model_key == "openai":
            return sem_tg_openai
        if model_key == "claude_fable":
            return sem_tg_fable
        return sem_tg_claude
    if model_key == "openai":
        return sem_workspace_openai
    if model_key == "claude_fable":
        return sem_workspace_fable
    return sem_workspace_claude


async def tg_send_chat_action(chat_id: int, action: str = "typing") -> None:
    if not TG_API:
        return
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            await client.post(f"{TG_API}/sendChatAction", json={"chat_id": int(chat_id), "action": action})
    except Exception:
        pass


async def tg_send_message(chat_id: int, text: str, reply_markup: Optional[dict] = None) -> Optional[int]:
    if not TG_API:
        print("[chat_worker] TELEGRAM_BOT_TOKEN is not set", flush=True)
        return None
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


async def tg_send_document_bytes(
    chat_id: int,
    raw: bytes,
    *,
    filename: str,
    caption: Optional[str] = None,
    reply_markup: Optional[dict] = None,
) -> bool:
    if not TG_API:
        print("[chat_worker] TELEGRAM_BOT_TOKEN is not set", flush=True)
        return False
    data: Dict[str, Any] = {"chat_id": str(int(chat_id))}
    if caption:
        data["caption"] = str(caption)[:1024]
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    files = {"document": (filename, raw, "text/plain; charset=utf-8")}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(f"{TG_API}/sendDocument", data=data, files=files)
        payload = response.json() if response.content else {}
        return bool(isinstance(payload, dict) and payload.get("ok"))
    except Exception as exc:
        print(f"[chat_worker] sendDocument failed: {exc}", flush=True)
        return False


async def _upload_long_answer_text(
    text: str,
    *,
    chat_id: int,
    user_id: int = 0,
    job_id: str = "",
) -> str:
    raw = (text or "").encode("utf-8")
    if not raw:
        return ""
    filename = f"astrabot_answer_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.txt"
    try:
        storage_ref = await upload_chat_attachment_bytes(
            raw,
            filename=filename,
            content_type="text/plain; charset=utf-8",
            user_id=user_id or chat_id,
            origin="telegram-long-answer",
            job_id=job_id or None,
        )
        bucket = str(storage_ref.get("storage_bucket") or CHAT_ATTACHMENTS_BUCKET)
        path = str(storage_ref.get("storage_path") or "")
        signed = await create_signed_url(bucket, path, expires_in=TG_LONG_ANSWER_SIGNED_TTL_SEC) if path else ""
        return signed or str(storage_ref.get("storage_url") or "")
    except Exception as exc:
        print(f"[chat_worker] long answer upload failed job={job_id}: {exc}", flush=True)
        return ""


async def tg_send_long_message(
    chat_id: int,
    text: str,
    reply_markup: Optional[dict] = None,
    *,
    user_id: int = 0,
    job_id: str = "",
) -> None:
    clean = str(text or "").strip() or "Пустой ответ от модели."

    if len(clean) >= max(1, TG_LONG_ANSWER_FILE_THRESHOLD):
        raw = clean.encode("utf-8")
        # Keep the chat compact: by default show only ~400 chars, not a huge preview block.
        preview_limit = max(120, min(TG_LONG_ANSWER_PREVIEW_CHARS, 900))
        preview = clean[:preview_limit].strip()
        if len(clean) > preview_limit:
            preview += "…"

        download_url = await _upload_long_answer_text(clean, chat_id=chat_id, user_id=user_id, job_id=job_id)
        filename = f"astrabot_answer_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.txt"
        sent_file = await tg_send_document_bytes(
            chat_id,
            raw,
            filename=filename,
            caption="Полный ответ AstraBot в TXT",
        )
        if sent_file or download_url:
            notice = "📄 Ответ получился длинным.\n"
            if sent_file:
                notice += "Полный текст прикрепил TXT-файлом.\n"
            elif download_url:
                notice += "Полный текст доступен по кнопке ниже.\n"
            notice += f"\nКраткое начало:\n\n{preview}"

            effective_reply_markup = reply_markup
            if download_url:
                download_button = {"text": "🔗 Скачать TXT", "url": download_url}
                if isinstance(reply_markup, dict) and isinstance(reply_markup.get("inline_keyboard"), list):
                    effective_reply_markup = {
                        **reply_markup,
                        "inline_keyboard": [[download_button], *reply_markup.get("inline_keyboard", [])],
                    }
                else:
                    effective_reply_markup = {"inline_keyboard": [[download_button]]}

            await tg_send_message(chat_id, notice[:3900], reply_markup=effective_reply_markup)
            return

        # If both Storage and Telegram document delivery fail, fall back to safe chunking without a misleading file notice.

    chunks: List[str] = []
    while len(clean) > 3900:
        cut = clean.rfind("\n", 0, 3900)
        if cut < 1000:
            cut = 3900
        chunks.append(clean[:cut].strip())
        clean = clean[cut:].strip()
    chunks.append(clean)
    for index, chunk in enumerate(chunks):
        await tg_send_message(chat_id, chunk, reply_markup=reply_markup if index == len(chunks) - 1 else None)


async def tg_get_file_path(file_id: str) -> str:
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(f"{TG_API}/getFile", params={"file_id": file_id})
    response.raise_for_status()
    data = response.json()
    return str((data.get("result") or {}).get("file_path") or "")


async def tg_download_file_bytes(file_path: str) -> bytes:
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.get(f"{TG_FILE}/{file_path}")
    response.raise_for_status()
    return response.content


async def _typing_heartbeat(chat_id: int, stop: asyncio.Event) -> None:
    while not stop.is_set():
        await tg_send_chat_action(chat_id, "typing")
        try:
            await asyncio.wait_for(stop.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            continue


@asynccontextmanager
async def _redis_lock(lock_name: str):
    r = await get_redis()
    token = str(uuid.uuid4())
    key = f"astrabot:lock:{lock_name}"
    start = time.time()
    acquired = False
    while time.time() - start < CHAT_USER_LOCK_MAX_WAIT_SEC:
        acquired = bool(await r.set(key, token, nx=True, ex=CHAT_USER_LOCK_TTL_SEC))
        if acquired:
            break
        await asyncio.sleep(CHAT_USER_LOCK_WAIT_SEC)
    if not acquired:
        raise TimeoutError(f"Не удалось получить lock для {lock_name}")
    try:
        yield
    finally:
        try:
            current = await r.get(key)
            if current == token:
                await r.delete(key)
        except Exception:
            pass




def _job_charge_tokens(job: Dict[str, Any]) -> int:
    try:
        return max(0, int(job.get("charge_tokens") or 0))
    except Exception:
        return 0


def _job_charge_ref_id(job: Dict[str, Any]) -> str:
    return str(job.get("charge_ref_id") or "").strip()


async def _refund_paid_chat_job(job: Dict[str, Any], *, stage: str, error: str = "") -> None:
    charge_tokens = _job_charge_tokens(job)
    charge_ref_id = _job_charge_ref_id(job)
    if charge_tokens <= 0 or not charge_ref_id:
        return
    try:
        user_id_raw = str(job.get("user_id") or "").strip()
        user_id = int(user_id_raw) if user_id_raw.isdigit() else int(job.get("telegram_user_id") or 0)
        if user_id <= 0:
            return
        reason = str(job.get("refund_reason") or "claude_fable_chat_refund").strip() or "claude_fable_chat_refund"
        await asyncio.to_thread(
            lambda: add_tokens(
                user_id,
                charge_tokens,
                reason=reason,
                ref_id=charge_ref_id,
                meta={
                    "stage": stage,
                    "error": str(error or "")[:500],
                    "job_id": str(job.get("job_id") or ""),
                    "model": str(job.get("model") or job.get("model_actual") or KIE_CLAUDE_FABLE_MODEL_ID),
                },
            )
        )
    except Exception as exc:
        print(f"[chat_worker] paid chat refund failed job={job.get('job_id')}: {exc}", flush=True)

async def _prepare_tg_file_context(job: Dict[str, Any], incoming_text: str) -> tuple[str, str, List[bytes]]:
    file_meta = job.get("file") if isinstance(job.get("file"), dict) else None
    if not file_meta:
        return incoming_text, incoming_text, []

    user_id = int(job.get("user_id") or 0)
    filename = str(file_meta.get("filename") or "file").strip() or "file"
    file_id = str(file_meta.get("file_id") or "").strip()
    mime_type = str(file_meta.get("mime_type") or "application/octet-stream").strip()
    size_bytes = int(file_meta.get("size_bytes") or 0)
    if not file_id:
        raise RuntimeError("Не смог прочитать file_id файла. Отправь файл ещё раз.")
    if size_bytes > AI_CHAT_FILE_MAX_BYTES:
        raise RuntimeError("Файл больше 10 МБ. Для Claude/ChatGPT можно отправлять файлы до 10 МБ.")

    file_path = await tg_get_file_path(file_id)
    raw = await tg_download_file_bytes(file_path)
    storage_ref: Dict[str, Any] = {}
    try:
        storage_ref = await upload_chat_attachment_bytes(
            raw,
            filename=filename,
            content_type=mime_type,
            user_id=user_id,
            origin="telegram",
            job_id=str(job.get("job_id") or "") or None,
        )
    except Exception as exc:
        # Storage must not break the chat answer; Redis still only contains Telegram file_id.
        print(f"[chat_worker] tg attachment storage upload failed job={job.get('job_id')}: {exc}", flush=True)

    is_image = str(file_meta.get("kind") or "").strip().lower() == "image" or mime_type.lower().startswith("image/")
    if is_image:
        user_text = incoming_text or "Опиши изображение и ответь по нему кратко и полезно."
        file_context = f"Пользователь приложил изображение: {filename} · {max(1, round(len(raw) / 1024))} KB"
        if storage_ref.get("storage_path"):
            file_context += f"\nStorage: {storage_ref.get('storage_bucket')}/{storage_ref.get('storage_path')}"
        memory_user = user_text + f"\n🖼 Изображение: {filename} ({max(1, round(len(raw) / 1024))} KB)"
        return f"{user_text}\n\n{file_context}", memory_user, [raw]

    kind, extracted, notice = extract_file_text(raw, filename, mime_type)
    extracted = (extracted or "")[:AI_CHAT_FILE_TEXT_MAX_CHARS]

    user_text = incoming_text or "Проанализируй приложенный файл и дай краткий полезный вывод."
    file_context = f"Пользователь приложил файл: {filename} · {kind} · {max(1, round(len(raw) / 1024))} KB"
    if storage_ref.get("storage_path"):
        file_context += f"\nStorage: {storage_ref.get('storage_bucket')}/{storage_ref.get('storage_path')}"
    if notice:
        file_context += f"\nЗаметка: {notice}"
    if extracted:
        file_context += f"\n\nИзвлечённый текст файла, первые {min(len(extracted), AI_CHAT_FILE_TEXT_MAX_CHARS)} символов:\n{extracted}"
    else:
        file_context += "\n\nТекст из файла извлечь не удалось. Ответь пользователю честно и попроси прислать текстовый/PDF/DOCX файл, если нужен анализ содержимого."

    memory_user = user_text + f"\n📎 Файл: {filename} ({kind}, {max(1, round(len(raw) / 1024))} KB)"
    return f"{user_text}\n\n{file_context}", memory_user, []


async def process_tg_ai_chat_job(job: Dict[str, Any]) -> None:
    chat_id = int(job.get("chat_id") or 0)
    user_id = int(job.get("user_id") or 0)
    status_message_id = int(job.get("status_message_id") or 0) or None
    reply_markup = job.get("reply_markup") if isinstance(job.get("reply_markup"), dict) else None
    incoming_text = str(job.get("text") or "").strip()
    system_prompt = str(job.get("system_prompt") or DEFAULT_TG_SYSTEM_PROMPT).strip() or DEFAULT_TG_SYSTEM_PROMPT
    model_key = _job_model_key(job)

    if not chat_id or not user_id:
        print(f"[chat_worker] bad tg job={job.get('job_id')}: missing chat_id/user_id", flush=True)
        return

    stop = asyncio.Event()
    typing_task = asyncio.create_task(_typing_heartbeat(chat_id, stop))
    try:
        async with _redis_lock(f"tgchat:{chat_id}:{user_id}"):
            user_payload, memory_user, image_bytes_list = await _prepare_tg_file_context(job, incoming_text)
            memory = await maybe_summarize_tg_chat_memory(chat_id, user_id)
            history = (memory.get("hist") or [])[-AI_CHAT_HISTORY_MAX:]
            summary = str(memory.get("summary") or "")

            model_actual = str(job.get("model") or "").strip()
            if model_key == "openai":
                answer = await openai_chat_answer(
                    user_text=user_payload,
                    system_prompt=system_prompt,
                    history=history,
                    temperature=0.4,
                    max_tokens=1500,
                    model=model_actual or str(os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini") or "gpt-4o-mini"),
                    image_bytes_list=image_bytes_list or None,
                )
            else:
                model_actual = normalize_kie_claude_model(model_actual) or KIE_CLAUDE_MODEL_ID
                history_limit = kie_claude_history_messages_for_model(model_actual)
                summary_limit = kie_claude_summary_chars_for_model(model_actual)
                max_tokens = kie_claude_max_tokens_for_model(model_actual)
                answer = await kie_claude_answer(
                    user_text=user_payload,
                    system_prompt=system_prompt,
                    history=history[-history_limit:],
                    summary=summary[:summary_limit],
                    max_tokens=max_tokens,
                    thinking=bool(job.get("thinking", False if kie_claude_is_fable_model(model_actual) else True)),
                    image_bytes_list=image_bytes_list or None,
                    model=model_actual,
                    raise_on_error=kie_claude_is_fable_model(model_actual) and _job_charge_tokens(job) > 0,
                )
            await add_tg_chat_turn(chat_id, user_id, user_text=memory_user, assistant_text=answer)

        await tg_delete_message(chat_id, status_message_id)
        await tg_send_long_message(chat_id, answer, reply_markup=reply_markup, user_id=user_id, job_id=str(job.get("job_id") or ""))
        await log_free_usage_event_async(
            source="telegram",
            service="ChatGPT" if model_key == "openai" else kie_claude_display_name(model_actual),
            model=model_actual or str(job.get("model") or ""),
            mode="chat",
            user_id=user_id,
            telegram_user_id=user_id,
            status="completed",
            ref_id=str(job.get("job_id") or ""),
            meta={
                "kind": "tg_ai_chat",
                "chat_id": chat_id,
                "telegram_user_id": user_id,
                "has_file": bool(job.get("file")),
                "text_length": len(incoming_text or ""),
            },
        )
        print(f"[chat_worker] completed tg job={job.get('job_id')} model={model_key}", flush=True)
    except Exception as exc:
        await _refund_paid_chat_job(job, stage="tg_worker_exception", error=str(exc))
        await tg_delete_message(chat_id, status_message_id)
        await tg_send_message(chat_id, f"❌ Чат временно не ответил: {exc}", reply_markup=reply_markup)
        print(f"[chat_worker] failed tg job={job.get('job_id')}: {exc}", flush=True)
    finally:
        stop.set()
        try:
            await typing_task
        except Exception:
            pass


def _decode_image_bytes_list(items: Any) -> List[bytes]:
    """Backward compatibility for jobs queued before storage refs were introduced."""
    out: List[bytes] = []
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, str) or not item:
            continue
        try:
            out.append(base64.b64decode(item.encode("ascii"), validate=False))
        except Exception:
            continue
    return out


async def _load_image_bytes_from_storage_refs(items: Any) -> List[bytes]:
    out: List[bytes] = []
    if not isinstance(items, list):
        return out
    for item in items:
        if len(out) >= 4:
            break
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip().lower()
        content_type = str(item.get("content_type") or "").strip().lower()
        if kind and kind != "image" and not content_type.startswith("image/"):
            continue
        path = str(item.get("storage_path") or "").strip()
        if not path:
            continue
        bucket = str(item.get("storage_bucket") or CHAT_ATTACHMENTS_BUCKET).strip() or CHAT_ATTACHMENTS_BUCKET
        try:
            raw = await download_chat_attachment_bytes(bucket, path)
            if raw:
                out.append(raw)
        except Exception as exc:
            print(f"[chat_worker] failed to load image attachment {bucket}/{path}: {exc}", flush=True)
    return out


async def process_workspace_ai_chat_job(job: Dict[str, Any]) -> None:
    job_id = str(job.get("job_id") or "").strip()
    if not job_id:
        return
    await set_chat_job_status(job_id, status="processing")
    try:
        user_text = str(job.get("user_text") or "").strip()
        system_prompt = str(job.get("system_prompt") or "Ты полезный ассистент.").strip()
        history = job.get("history") if isinstance(job.get("history"), list) else []
        summary = str(job.get("summary") or "")
        mode = str(job.get("mode") or "chat")
        model_label = str(job.get("model_label") or job.get("model_actual") or "")
        model_actual = str(job.get("model_actual") or "").strip()
        max_tokens = int(job.get("max_tokens") or 900)
        temperature = float(job.get("temperature") or 0.6)
        attachments = job.get("attachments") if isinstance(job.get("attachments"), list) else []
        image_bytes_list = await _load_image_bytes_from_storage_refs(job.get("image_storage_refs"))
        if not image_bytes_list:
            image_bytes_list = _decode_image_bytes_list(job.get("image_bytes_b64"))

        if is_kie_claude_model(model_actual) and mode == "chat":
            history_limit = kie_claude_history_messages_for_model(model_actual)
            summary_limit = kie_claude_summary_chars_for_model(model_actual)
            max_tokens = kie_claude_max_tokens_for_model(model_actual)
            answer = await kie_claude_answer(
                user_text=user_text,
                system_prompt=system_prompt,
                history=history[-history_limit:],
                summary=summary[:summary_limit],
                max_tokens=max_tokens,
                thinking=bool(job.get("thinking", False if kie_claude_is_fable_model(model_actual) else True)),
                image_bytes_list=image_bytes_list or None,
                model=model_actual,
                raise_on_error=kie_claude_is_fable_model(model_actual) and _job_charge_tokens(job) > 0,
            )
        else:
            answer = await openai_chat_answer(
                user_text=user_text,
                system_prompt=system_prompt,
                history=history,
                temperature=temperature,
                max_tokens=max_tokens,
                model=model_actual or None,
                image_bytes_list=image_bytes_list or None,
            )

        is_prompt = bool(job.get("is_prompt_builder") and str(answer or "").strip())
        await set_chat_job_status(
            job_id,
            status="completed",
            ok=True,
            answer=answer,
            mode=mode,
            model=model_label,
            resolved_model=model_actual,
            summary=summary,
            attachments=attachments,
            is_prompt=is_prompt,
        )
        workspace_uid = int(job.get("user_id") or 0) if str(job.get("user_id") or "").isdigit() else None
        await log_free_usage_event_async(
            source="site",
            service=kie_claude_display_name(model_actual) if is_kie_claude_model(model_actual) and mode == "chat" else "ChatGPT",
            model=model_actual or model_label,
            mode=mode,
            user_id=workspace_uid,
            workspace_account_id=workspace_uid,
            status="completed",
            ref_id=job_id,
            meta={
                "kind": "workspace_ai_chat",
                "workspace_user_id": workspace_uid,
                "attachments_count": len(attachments or []),
                "image_refs_count": len(image_bytes_list or []),
                "is_prompt_builder": bool(is_prompt),
                "text_length": len(user_text or ""),
            },
        )
        print(f"[chat_worker] completed workspace job={job_id} model={model_actual}", flush=True)
    except Exception as exc:
        await _refund_paid_chat_job(job, stage="workspace_worker_exception", error=str(exc))
        await set_chat_job_status(job_id, status="failed", ok=False, error=str(exc))
        print(f"[chat_worker] failed workspace job={job_id}: {exc}", flush=True)



def _split_tg_text(text: str, limit: int = 4096) -> List[str]:
    """Split a Telegram text message into safe chunks without changing content."""
    raw = str(text or "")
    if not raw:
        return []
    safe_limit = max(1000, min(int(limit or 4096), 4096))
    if len(raw) <= safe_limit:
        return [raw]

    chunks: List[str] = []
    rest = raw
    while rest:
        if len(rest) <= safe_limit:
            chunks.append(rest)
            break

        cut = rest.rfind("\n", 0, safe_limit)
        if cut < int(safe_limit * 0.6):
            cut = rest.rfind(" ", 0, safe_limit)
        if cut < int(safe_limit * 0.6):
            cut = safe_limit

        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    return [chunk for chunk in chunks if chunk]


def _fetch_broadcast_user_ids_sync() -> List[int]:
    """Read unique Telegram user ids from Supabase bot_users."""
    if sb is None:
        raise RuntimeError("Supabase не настроен (sb=None)")

    page_size = max(100, int(TG_BROADCAST_BATCH_SIZE or 1000))
    start = 0
    user_ids: List[int] = []
    seen: set[int] = set()

    while True:
        resp = (
            sb.table("bot_users")
            .select("telegram_user_id")
            .range(start, start + page_size - 1)
            .execute()
        )
        rows = getattr(resp, "data", None) or []
        if not rows:
            break

        for row in rows:
            try:
                raw_uid = row.get("telegram_user_id") if isinstance(row, dict) else None
                if raw_uid is None:
                    continue
                uid = int(raw_uid)
                if uid <= 0 or uid in seen:
                    continue
                seen.add(uid)
                user_ids.append(uid)
            except Exception:
                continue

        if len(rows) < page_size:
            break
        start += page_size

    return user_ids


async def _send_broadcast_message_to_user(client: httpx.AsyncClient, chat_id: int, text: str) -> Tuple[bool, str]:
    """Send broadcast text to one Telegram chat and return (ok, error)."""
    if not TG_API:
        return False, "TELEGRAM_BOT_TOKEN is not set"

    chunks = _split_tg_text(text)
    if not chunks:
        return False, "empty text"

    for chunk_index, chunk in enumerate(chunks, start=1):
        attempts = 0
        while True:
            attempts += 1
            try:
                response = await client.post(
                    f"{TG_API}/sendMessage",
                    json={"chat_id": int(chat_id), "text": chunk},
                )
                data = response.json() if response.content else {}
            except Exception as exc:
                if attempts < 3:
                    await asyncio.sleep(1.0 * attempts)
                    continue
                return False, f"request failed on part {chunk_index}: {exc}"

            if isinstance(data, dict) and data.get("ok"):
                break

            error_code = int(data.get("error_code") or response.status_code or 0) if isinstance(data, dict) else response.status_code
            description = str(data.get("description") or response.text or "unknown error") if isinstance(data, dict) else str(response.text or "unknown error")
            retry_after = 0
            if isinstance(data, dict):
                params = data.get("parameters") if isinstance(data.get("parameters"), dict) else {}
                try:
                    retry_after = int(params.get("retry_after") or 0)
                except Exception:
                    retry_after = 0

            if error_code == 429 and retry_after > 0 and attempts < 4:
                await asyncio.sleep(float(retry_after) + 0.5)
                continue

            if attempts < 2 and error_code >= 500:
                await asyncio.sleep(1.0)
                continue

            return False, f"telegram error {error_code}: {description}"

    return True, ""


async def process_tg_broadcast_job(job: Dict[str, Any]) -> None:
    job_id = str(job.get("job_id") or "")
    admin_chat_id = int(job.get("admin_chat_id") or job.get("chat_id") or 0)
    text = str(job.get("text") or "").strip()

    if admin_chat_id <= 0:
        print(f"[tg_broadcast] skipped job={job_id}: admin_chat_id is empty", flush=True)
        return
    if not text:
        await tg_send_message(admin_chat_id, "❌ Рассылка не запущена: пустой текст.")
        return

    await tg_send_message(admin_chat_id, "⏳ Начал рассылку...")

    try:
        user_ids = await asyncio.to_thread(_fetch_broadcast_user_ids_sync)
    except Exception as exc:
        await tg_send_message(admin_chat_id, f"❌ Не смог получить список пользователей для рассылки: {exc}")
        print(f"[tg_broadcast] failed to fetch users job={job_id}: {exc}", flush=True)
        return

    total = len(user_ids)
    if total <= 0:
        await tg_send_message(admin_chat_id, "⚠️ Пользователей для рассылки нет (bot_users пуст).")
        return

    sent = 0
    failed = 0
    error_examples: List[str] = []
    started_ts = time.time()

    timeout = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for idx, uid in enumerate(user_ids, start=1):
            ok, err = await _send_broadcast_message_to_user(client, uid, text)
            if ok:
                sent += 1
            else:
                failed += 1
                if len(error_examples) < 5:
                    error_examples.append(f"{uid}: {err}")

            if TG_BROADCAST_PROGRESS_EVERY > 0 and idx < total and idx % TG_BROADCAST_PROGRESS_EVERY == 0:
                await tg_send_message(
                    admin_chat_id,
                    f"📣 Рассылка в процессе: {idx}/{total}\n✅ Отправлено: {sent}\n❌ Ошибок: {failed}",
                )

            if TG_BROADCAST_DELAY_SEC > 0:
                await asyncio.sleep(TG_BROADCAST_DELAY_SEC)

    elapsed = max(0, int(time.time() - started_ts))
    report = (
        "📣 Рассылка завершена.\n"
        f"👥 Всего пользователей: {total}\n"
        f"✅ Отправлено: {sent}\n"
        f"❌ Ошибок: {failed}\n"
        f"⏱ Время: {elapsed} сек."
    )
    if error_examples:
        report += "\n\nПервые ошибки:\n" + "\n".join(error_examples)

    await tg_send_message(admin_chat_id, report)
    print(f"[tg_broadcast] completed job={job_id} total={total} sent={sent} failed={failed}", flush=True)


async def _notify_admins(text: str) -> None:
    if not ADMIN_IDS:
        return
    for admin_id in ADMIN_IDS:
        try:
            await tg_send_message(int(admin_id), text)
        except Exception as exc:
            print(f"[partner_worker] failed to notify admin={admin_id}: {exc}", flush=True)


async def process_partner_event(job: Dict[str, Any]) -> None:
    kind = _job_kind(job)
    if kind == "partner_bind_referral":
        result = await asyncio.to_thread(
            lambda: bind_referral(
                referred_user_id=int(job.get("referred_user_id") or 0),
                ref_code=str(job.get("ref_code") or ""),
                source=str(job.get("source") or "telegram_start"),
                meta=job.get("meta") if isinstance(job.get("meta"), dict) else {},
            )
        )
        print(f"[partner_worker] bind result={result}", flush=True)
        return

    if kind == "partner_topup":
        result = await asyncio.to_thread(
            lambda: apply_topup_event(
                referred_user_id=int(job.get("referred_user_id") or 0),
                source_payment_id=str(job.get("source_payment_id") or ""),
                payment_amount_rub=float(job.get("payment_amount_rub") or 0),
                purchased_tokens=int(job.get("purchased_tokens") or 0),
                payment_provider=str(job.get("payment_provider") or "unknown"),
                meta=job.get("meta") if isinstance(job.get("meta"), dict) else {},
            )
        )
        print(f"[partner_worker] topup result={result}", flush=True)
        return

    if kind == "partner_payout_created":
        payout = job.get("payout") if isinstance(job.get("payout"), dict) else {}
        if not payout:
            return
        amount = payout.get("amount_rub") or 0
        partner_user_id = payout.get("partner_user_id") or ""
        card_mask = payout.get("card_mask") or ""
        holder = payout.get("card_holder_name") or ""
        payout_id = payout.get("id") or ""
        text = (
            "💸 Новая заявка на выплату партнёрки\n\n"
            f"Партнёр user_id: {partner_user_id}\n"
            f"Сумма: {amount} ₽\n"
            f"Карта: {card_mask}\n"
            f"ФИО: {holder}\n"
            f"ID заявки: {payout_id}\n\n"
            "Открой админку партнёрских выплат и после ручного перевода нажми «Оплачено»."
        )
        await _notify_admins(text)
        print(f"[partner_worker] payout notification sent payout_id={payout_id}", flush=True)
        return

    print(f"[partner_worker] unsupported event kind={kind} job={job.get('job_id')}", flush=True)


async def _handle(job: Dict[str, Any]) -> None:
    kind = _job_kind(job)
    sem = _sem_for_job(job)
    async with sem:
        if kind == "tg_ai_chat":
            await process_tg_ai_chat_job(job)
            return
        if kind == "workspace_ai_chat":
            await process_workspace_ai_chat_job(job)
            return
        if kind == "tg_broadcast":
            await process_tg_broadcast_job(job)
            return
        if kind in {"partner_topup", "partner_bind_referral", "partner_payout_created"}:
            await process_partner_event(job)
            return
        print(f"[chat_worker] skipped unsupported kind={kind} job={job.get('job_id')}", flush=True)


def _unique_queue_names(values: List[str]) -> List[str]:
    out: List[str] = []
    for value in values:
        clean = str(value or "").strip()
        if clean and clean not in out:
            out.append(clean)
    return out


async def _queue_loop(queue_name: str) -> None:
    """Listen to one Redis queue independently.

    BLPOP with multiple keys is priority-ordered, so a busy normal-chat queue can
    starve queues listed later. One loop per queue keeps Fable truly parallel
    with regular chat while semaphores still enforce per-model concurrency.
    """
    tasks: set[asyncio.Task] = set()
    while True:
        done = {task for task in tasks if task.done()}
        tasks -= done
        job = await dequeue_job(timeout_sec=10, queue_name=queue_name)
        if not job:
            continue
        task = asyncio.create_task(_handle(job))
        tasks.add(task)


async def main() -> None:
    queues = _unique_queue_names([
        TG_CHAT_OPENAI_QUEUE_NAME,
        TG_CHAT_CLAUDE_QUEUE_NAME,
        WORKSPACE_CHAT_OPENAI_QUEUE_NAME,
        WORKSPACE_CHAT_CLAUDE_QUEUE_NAME,
        TG_CHAT_FABLE_QUEUE_NAME,
        WORKSPACE_CHAT_FABLE_QUEUE_NAME,
        PARTNER_EVENTS_QUEUE_NAME,
        TG_BROADCAST_QUEUE_NAME,
    ])
    print(
        "[chat_worker] started "
        f"queues={queues} tg_openai={TG_CHAT_OPENAI_CONCURRENCY} tg_claude={TG_CHAT_CLAUDE_CONCURRENCY} tg_fable={TG_CHAT_FABLE_CONCURRENCY} "
        f"workspace_openai={WORKSPACE_CHAT_OPENAI_CONCURRENCY} workspace_claude={WORKSPACE_CHAT_CLAUDE_CONCURRENCY} workspace_fable={WORKSPACE_CHAT_FABLE_CONCURRENCY} "
        f"tg_broadcast={TG_BROADCAST_CONCURRENCY}",
        flush=True,
    )
    await asyncio.gather(*(_queue_loop(queue_name) for queue_name in queues))


if __name__ == "__main__":
    asyncio.run(main())
