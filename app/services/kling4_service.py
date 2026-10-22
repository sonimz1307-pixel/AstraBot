"""Authenticated Kling 4.0 jobs: durable DB queue in the existing media worker."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from uuid import uuid4

from db_supabase import supabase
from kling4_model import MAX_BYTES, MODEL_REFERENCE, MODEL_KEYFRAME, build_request, public_job

LOG = logging.getLogger("nabex.kling4")
ASSETS = "nabex_kling4_assets"
QUOTES = "nabex_kling4_quotes"
JOBS = "nabex_kling4_jobs"


def enabled() -> bool:
    return os.getenv("KLING4_ENABLED", "0").lower() in {"1", "true", "yes"}


def db():
    if supabase is None:
        raise RuntimeError("Supabase не настроен.")
    return supabase


def rpc(name: str, **params):
    data = db().rpc("nabex_kling4_" + name, {"p_" + k: v for k, v in params.items()}).execute().data
    return data[0] if isinstance(data, list) and len(data) == 1 else data


def get_job(job_id: str, user_id: int | None = None) -> dict | None:
    query = db().table(JOBS).select("*").eq("id", job_id)
    if user_id is not None:
        query = query.eq("user_id", user_id)
    rows = query.limit(1).execute().data or []
    return rows[0] if rows else None


def workspace_history_item(row: dict) -> dict:
    """Expose only fields consumed by the existing website video components."""
    data = row.get("input") or {}
    return {
        "id": row["id"], "user_id": str(row["user_id"]), "provider": "kling",
        "model": "kling-4.0", "mode": row["mode"], "task_id": row.get("provider_task_id"),
        "prompt": data.get("prompt", ""), "status": row["status"],
        "aspect_ratio": data.get("aspect_ratio", "16:9"), "duration_sec": row["duration"],
        "resolution": row["resolution"], "enable_audio": True, "tokens": row["tokens"],
        "refunded": bool(row.get("refunded")), "origin": "kling4", "is_favorite": False,
        "error_message": row.get("error_message"), "error_code": None,
        "created_at": row["created_at"], "updated_at": row.get("updated_at"),
        "completed_at": row.get("updated_at") if row["status"] in {"completed", "failed"} else None,
        "video_url": row.get("result_url"), "download_url": row.get("result_url"),
        "provider_video_url": row.get("result_url"), "has_storage_file": False,
        "storage_path": None, "thumbnail_path": None, "mime_type": "video/mp4",
    }


def workspace_history_page(user_id: int, limit: int, offset: int) -> list[dict] | None:
    try:
        return rpc("history", user_id=user_id, limit=limit, offset=offset)["items"]
    except Exception as exc:
        # A rolling deployment before the v3 migration must not break old models.
        # Never hide a real DB/permission/transport failure behind an empty history.
        if getattr(exc, "code", "") == "PGRST202":
            return None
        raise


def workspace_job(job_id: str, user_id: int) -> dict | None:
    from uuid import UUID
    try:
        UUID(job_id)
    except (ValueError, TypeError, AttributeError):
        return None
    try:
        row = get_job(job_id, user_id)
    except Exception as exc:
        if getattr(exc, "code", "") in {"42P01", "PGRST205"}:
            return None
        raise
    return row if row and not row.get("history_deleted_at") else None


def hide_workspace_job(row: dict) -> None:
    if row["status"] not in {"completed", "failed"}:
        raise ValueError("Дождитесь завершения задачи перед удалением из истории.")
    # Hide a finished result only. Keep the paid job and its idempotency/refund
    # record: replaying an old quote must never buy or refund another generation.
    db().table(JOBS).update({"history_deleted_at": datetime.now(timezone.utc).isoformat()}) \
        .eq("id", row["id"]).eq("user_id", row["user_id"]).in_("status", ["completed", "failed"]).execute()


def save_asset(user_id: int, kind: str, raw: bytes) -> dict:
    from app.services.trend_assets import _validated_image, _validated_media
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError("Максимальный размер файла — 10 МБ.")
    if kind == "image":
        raw, mime, ext, meta = _validated_image(raw, max_bytes=MAX_BYTES)
        width, height = int(meta["width"]), int(meta["height"])
        if min(width, height) < 300 or not 0.4 <= width / height <= 2.5:
            raise ValueError("Изображение: от 300 пикселей по каждой стороне, соотношение сторон от 0,4 до 2,5.")
    elif kind in {"video", "audio"}:
        raw, mime, ext, meta = _validated_media(raw, kind, max_bytes=MAX_BYTES)
    else:
        raise ValueError("Неизвестный тип файла.")
    duration = float(meta.get("duration_seconds") or meta.get("duration") or 0)
    if kind == "video" and not 2 <= duration <= 30:
        raise ValueError("Видео должно длиться от 2 до 30 секунд.")
    asset_id = str(uuid4())
    path = f"{user_id}/{asset_id}.{ext}"
    bucket = db().storage.from_("kling4-inputs")
    bucket.upload(path, raw, {"content-type": mime, "upsert": "false"})
    try:
        signed = bucket.create_signed_url(path, 86400)
        url = signed.get("signedURL") or signed.get("signedUrl")
        if not url or urlsplit(url).scheme != "https":
            raise RuntimeError("Не удалось создать защищённую ссылку на файл.")
        row = {"id": asset_id, "user_id": user_id, "kind": kind, "url": url,
               "storage_path": path, "duration_seconds": duration,
               "width": int(meta.get("width") or 0), "height": int(meta.get("height") or 0)}
        db().table(ASSETS).insert(row).execute()
    except Exception:
        try:
            bucket.remove([path])
        except Exception:
            pass
        raise
    return {"id": asset_id, "kind": kind, "duration_seconds": duration,
            "width": row["width"], "height": row["height"]}


def create_quote(user_id: int, chat_id: int | None, spec: dict) -> dict:
    # Never accept client URLs, a client price or a client-probed duration.
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    rows = db().table(ASSETS).select("*").eq("user_id", user_id).gte("created_at", cutoff).order("created_at", desc=True).limit(300).execute().data or []
    model, payload, quote = build_request(spec, {str(x["id"]): x for x in rows})
    row = {"id": str(uuid4()), "user_id": user_id, "telegram_chat_id": chat_id,
           "model": model, "input": payload, "price": quote, "tokens": quote["tokens"]}
    db().table(QUOTES).insert(row).execute()
    return {"id": row["id"], **quote, "expires_in": 900}


def api_key() -> str:
    value = os.getenv("KIE_API_TOKEN") or os.getenv("KIE_API_KEY") or os.getenv("KIE_TOKEN") or ""
    if not value.strip():
        raise RuntimeError("KIE API не настроен.")
    return value.strip()


def callback_signature(job_id: str) -> str:
    secret = os.getenv("KLING4_CALLBACK_SECRET") or os.getenv("WORKSPACE_AUTH_SECRET") or os.getenv("TELEGRAM_BOT_TOKEN") or ""
    if not secret:
        raise RuntimeError("Не настроен секрет callback Kling 4.0.")
    return hmac.new(secret.encode(), ("kling4:" + job_id).encode(), hashlib.sha256).hexdigest()


def callback_url(job_id: str) -> str:
    base = (os.getenv("KLING4_PUBLIC_BASE_URL") or os.getenv("PUBLIC_BASE_URL") or "https://nabex.ru").rstrip("/")
    if urlsplit(base).scheme != "https":
        raise RuntimeError("KLING4_PUBLIC_BASE_URL должен использовать HTTPS.")
    return f"{base}/api/kling4/callback/{job_id}/{callback_signature(job_id)}"


class ProviderRejected(ValueError):
    """A definitive rejection before a task was created; safe to refund."""


class SubmissionUncertain(RuntimeError):
    """Never POST again: KIE may have created and billed the original task."""


async def submit(job: dict) -> str:
    import httpx
    # Configuration is checked before the POST, so a local error cannot spend KIE credits.
    token = api_key()
    callback = callback_url(str(job["id"]))
    base = (os.getenv("KIE_API_BASE") or "https://api.kie.ai").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
            response = await client.post(base + "/api/v1/jobs/createTask",
                headers={"Authorization": "Bearer " + token},
                json={"model": job["model"], "input": job["input"], "callBackUrl": callback})
        try:
            data = response.json()
        except Exception as exc:
            raise SubmissionUncertain("KIE вернул ответ без подтверждённого номера задачи.") from exc
        task_id = str((data.get("data") or {}).get("taskId") or "")
        if task_id:
            return task_id
        # Only documented client rejections are considered proof of no task.
        code = str(data.get("code") or response.status_code)
        if code in {"400", "401", "402", "403", "422", "429"} and response.status_code < 500:
            raise ProviderRejected("Сервис отклонил запрос. Проверьте материалы и параметры.")
        raise SubmissionUncertain("Подтверждение запуска ещё не получено.")
    except (ProviderRejected, SubmissionUncertain):
        raise
    except Exception as exc:
        raise SubmissionUncertain("Связь прервалась при запуске. Проверяем исходную задачу.") from exc


async def task_info(task_id: str) -> dict:
    import httpx
    base = (os.getenv("KIE_API_BASE") or "https://api.kie.ai").rstrip("/")
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        response = await client.get(base + "/api/v1/jobs/recordInfo",
            headers={"Authorization": "Bearer " + api_key()}, params={"taskId": task_id})
    response.raise_for_status()
    result = response.json()
    if str(result.get("code")) != "200" or not isinstance(result.get("data"), dict):
        raise RuntimeError("Не удалось подтвердить состояние задачи в KIE.")
    data = result["data"]
    if str(data.get("taskId") or "") != task_id:
        raise RuntimeError("KIE вернул другую задачу.")
    return data


def verify_task(job: dict, info: dict, *, require_input: bool = False) -> None:
    if info.get("model") != job["model"]:
        raise ValueError("Модель задачи не совпадает.")
    if require_input:
        raw = info.get("param")
        param = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(param, dict):
            raise ValueError("Провайдер не вернул параметры для сверки.")
        value = param.get("input")
        value = json.loads(value) if isinstance(value, str) else value
        if value != job["input"] or param.get("callBackUrl") != callback_url(str(job["id"])):
            raise ValueError("Параметры или callback задачи не совпадают.")


def result_url(info: dict) -> str:
    raw = info.get("resultJson")
    result = json.loads(raw) if isinstance(raw, str) else raw
    urls = result.get("resultUrls", []) if isinstance(result, dict) else []
    for value in urls:
        parsed = urlsplit(str(value))
        if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password:
            return str(value)
    raise RuntimeError("Провайдер ещё не предоставил ссылку на готовое видео.")


async def settle_from_info(job: dict, info: dict) -> dict | None:
    verify_task(job, info)
    if info.get("state") == "success":
        return await asyncio.to_thread(rpc, "finish", job_id=job["id"], outcome="completed", result_url=result_url(info), error=None)
    if info.get("state") == "fail":
        return await asyncio.to_thread(rpc, "finish", job_id=job["id"], outcome="failed", result_url=None,
            error="Kling не смог создать видео. Токены возвращены.")
    return None


def release_lease(job: dict, *, review: bool = False, delay: int = 20) -> None:
    patch = {"lease_owner": None, "lease_until": None,
             "next_check_at": (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()}
    if review:
        patch.update(status="needs_review", error_message="Проверяем подтверждение запуска. Повторное списание не производится.")
    query = db().table(JOBS).update(patch).eq("id", job["id"]).eq("lease_owner", job["lease_owner"])
    # A callback may already have completed the task. Never downgrade a terminal state.
    query.in_("status", ["submitting"] if review else ["submitting", "processing"]).execute()


async def process_claimed(job: dict) -> None:
    if job.get("submit_authorized"):
        created = datetime.fromisoformat(job["created_at"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - created > timedelta(hours=1):
            await asyncio.to_thread(rpc, "finish", job_id=job["id"], outcome="failed",
                error="Очередь занята больше часа. Токены возвращены; можно повторить позже.")
            return
        try:
            # Missing config is known to precede any provider call.
            api_key()
            callback_url(job["id"])
        except RuntimeError:
            await asyncio.to_thread(rpc, "finish", job_id=job["id"], outcome="failed", error="Сервис временно не настроен. Токены возвращены.")
            return
        try:
            task_id = await submit(job)
        except ProviderRejected as exc:
            await asyncio.to_thread(rpc, "finish", job_id=job["id"], outcome="failed", error=str(exc))
            return
        except SubmissionUncertain:
            await asyncio.to_thread(release_lease, job, review=True)
            LOG.error("Kling4 needs_review job=%s: creation outcome unknown; do not resubmit", job["id"])
            return
        # Persist the ID before polling. A DB failure leaves 'submitting', never
        # sends createTask again; a signed callback can recover the same task ID.
        await asyncio.to_thread(rpc, "attach", job_id=job["id"], task_id=task_id)
        job["provider_task_id"] = task_id
    elif not job.get("provider_task_id"):
        await asyncio.to_thread(release_lease, job, review=True)
        LOG.error("Kling4 stale submission requires reconciliation job=%s", job["id"])
        return
    info = await task_info(job["provider_task_id"])
    if not await settle_from_info(job, info):
        await asyncio.to_thread(release_lease, job)


def start_notice_candidates() -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    return db().table(JOBS).select("*").in_("status", ["queued", "submitting", "processing", "needs_review"]) \
        .is_("start_notified_at", "null").not_.is_("telegram_chat_id", "null") \
        .lte("start_notice_next_at", now).or_(f"start_notice_lease_until.is.null,start_notice_lease_until.lte.{now}") \
        .order("start_notice_next_at").order("created_at").limit(5).execute().data or []


def claim_start_notice(job_id: str) -> dict | None:
    now = datetime.now(timezone.utc)
    # Conditional UPDATE is atomic across workers. Recheck the status because a
    # queued candidate might already have finished while we sent another notice.
    rows = db().table(JOBS).update({"start_notice_lease_until": (now + timedelta(minutes=2)).isoformat()}) \
        .eq("id", job_id).in_("status", ["queued", "submitting", "processing", "needs_review"]) \
        .is_("start_notified_at", "null").not_.is_("telegram_chat_id", "null") \
        .lte("start_notice_next_at", now.isoformat()) \
        .or_(f"start_notice_lease_until.is.null,start_notice_lease_until.lte.{now.isoformat()}").execute().data or []
    return rows[0] if rows else None


def record_start_notice(row: dict, delivered: bool, delay: int) -> None:
    now = datetime.now(timezone.utc)
    update = {"start_notice_lease_until": None}
    if delivered:
        update["start_notified_at"] = now.isoformat()
    else:
        update["start_notice_next_at"] = (now + timedelta(seconds=delay)).isoformat()
    # An old sender cannot overwrite a later claim after a worker restart.
    db().table(JOBS).update(update).eq("id", row["id"]) \
        .eq("start_notice_lease_until", row["start_notice_lease_until"]).execute()


async def notify_started() -> None:
    import httpx
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return
    rows = await asyncio.to_thread(start_notice_candidates)
    for candidate in rows:
        row = None
        try:
            row = await asyncio.to_thread(claim_start_notice, candidate["id"])
            if not row:
                continue
            text = (f"🎬 Задача Kling 4.0 принята.\n"
                    f"{row['resolution']} · {row['duration']} сек.\n"
                    f"Списано: {row['tokens']} токенов.\n\n"
                    "Ожидайте — готовое видео пришлю в этот чат. Форму можно закрыть.")
            if row["status"] == "needs_review":
                text = (f"🎬 Задача Kling 4.0 оплачена: {row['tokens']} токенов.\n"
                        "Проверяем подтверждение запуска. Повторно запускать задачу не нужно.\n"
                        "Результат или сообщение об ошибке пришлю в этот чат. Форму можно закрыть.")
            delivered, delay = False, 300
            try:
                # Total deadline is below the database lease, including all HTTP
                # phases; this notification never holds a generation worker slot.
                async with httpx.AsyncClient(timeout=10) as client:
                    response = await asyncio.wait_for(client.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                        "chat_id": row["telegram_chat_id"], "text": text}), timeout=15)
                    data = response.json()
                delivered = data.get("ok") is True
                if not delivered:
                    code = int(data.get("error_code") or response.status_code)
                    delay = 3600 if code in {401, 403} else 300
                    delay = max(delay, int((data.get("parameters") or {}).get("retry_after") or 0))
                    LOG.warning("Kling4 Telegram start notice retry job=%s code=%s", row["id"], code)
            except Exception as exc:
                LOG.warning("Kling4 Telegram start notice retry job=%s (%s)", row["id"], type(exc).__name__)
            await asyncio.to_thread(record_start_notice, row, delivered, delay)
        except asyncio.CancelledError:
            raise  # Keep the lease; another worker can recover it after expiry.
        except Exception as exc:
            LOG.warning("Kling4 Telegram start notice pending job=%s (%s)", candidate["id"], type(exc).__name__)


def finished_notice_candidates() -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    def query():
        return db().table(JOBS).select("*").in_("status", ["completed", "failed"]) \
            .is_("notified_at", "null").not_.is_("telegram_chat_id", "null") \
            .lte("next_check_at", now).order("next_check_at").order("created_at").limit(5)
    try:
        # Do not send the result ahead of an in-flight acknowledgement. A crashed
        # acknowledgement sender can delay a ready result for at most two minutes.
        return query().or_(f"start_notice_lease_until.is.null,start_notice_lease_until.lte.{now}").execute().data or []
    except Exception as exc:
        if getattr(exc, "code", "") != "42703":
            raise
        # A rolling deployment before the new columns must retain final delivery.
        return query().execute().data or []


async def notify_finished() -> None:
    import httpx
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return
    rows = await asyncio.to_thread(finished_notice_candidates)
    for row in rows:
        delivered, delay = False, 300
        try:
            async with httpx.AsyncClient(timeout=40) as client:
                if row["status"] == "completed":
                    payload = {"chat_id": row["telegram_chat_id"], "video": row["result_url"],
                        "caption": f"Kling 4.0 · {row['resolution']} · {row['duration']} сек · {row['tokens']} ток."}
                    response = await client.post(f"https://api.telegram.org/bot{token}/sendVideo", json=payload)
                    data = response.json()
                    # Fall back only on a definitive bad-video rejection. A
                    # blocked chat, rate limit or timeout cannot be fixed by a link.
                    if not data.get("ok") and int(data.get("error_code") or response.status_code) == 400:
                        response = await client.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                            "chat_id": row["telegram_chat_id"], "text": "Kling 4.0: видео готово.\n" + row["result_url"]})
                else:
                    response = await client.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                        "chat_id": row["telegram_chat_id"], "text": (row.get("error_message") or "Генерация не завершилась.") + f"\nВозвращено: {row['tokens']} ток."})
                data = response.json()
                delivered = data.get("ok") is True
                if not delivered:
                    code = int(data.get("error_code") or response.status_code)
                    delay = 3600 if code in {401, 403} else 300
                    delay = max(delay, int((data.get("parameters") or {}).get("retry_after") or 0))
                    LOG.warning("Kling4 Telegram retry job=%s code=%s", row["id"], code)
        except Exception as exc:
            # Isolate recipients and never log an HTTP exception's token-bearing URL.
            LOG.warning("Kling4 Telegram retry job=%s (%s)", row["id"], type(exc).__name__)
        now = datetime.now(timezone.utc)
        patch = {"notified_at": now.isoformat()} if delivered else {"next_check_at": (now + timedelta(seconds=delay)).isoformat()}
        try:
            await asyncio.to_thread(lambda: db().table(JOBS).update(patch).eq("id", row["id"]).execute())
        except Exception as exc:
            LOG.warning("Kling4 Telegram delivery state pending job=%s (%s)", row["id"], type(exc).__name__)


async def run_worker() -> None:
    """Run in worker_workspace_media.py; no separate Render service needed."""
    if not enabled():
        return
    cap = max(1, min(10, int(os.getenv("KLING4_CONCURRENCY", "2"))))
    owner = "kling4-" + uuid4().hex
    print(f"[workspace_media] Kling4 durable queue enabled; provider concurrency={cap}", flush=True)

    async def slot(number: int):
        while True:
            job = None
            try:
                job = await asyncio.to_thread(rpc, "claim", owner=f"{owner}-{number}", max_active=cap)
                if job:
                    await process_claimed(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                # No refund on network/status/DB errors: leave for safe recovery.
                LOG.exception("Kling4 worker error job=%s", (job or {}).get("id"))
                if job:
                    try:
                        await asyncio.to_thread(release_lease, job, delay=45)
                    except Exception:
                        pass
            await asyncio.sleep(1 if job else 5)

    async def notification_loop(notify, interval):
        while True:
            try:
                await notify()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # HTTP exception strings may contain Telegram's bot-token URL.
                LOG.warning("Kling4 Telegram %s pending (%s)", notify.__name__, type(exc).__name__)
            await asyncio.sleep(interval)

    await asyncio.gather(*(slot(i) for i in range(cap)),
                         notification_loop(notify_started, 5), notification_loop(notify_finished, 15))
