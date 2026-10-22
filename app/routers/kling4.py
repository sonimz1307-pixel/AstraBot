from __future__ import annotations

import asyncio
import hmac
import os
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect

from billing_db import ensure_user_row, get_balance, resolve_billing_user_id
from app.services.admin_auth import require_admin_request
from app.services.workspace_auth import get_current_workspace_user, workspace_user_from_request
from app.services import kling4_service as service
from kling4_model import MAX_BYTES, RATES, public_job, verify_telegram

router = APIRouter(prefix="/api/kling4", tags=["kling4"])
UI = Path(__file__).resolve().parents[2] / "web_workspace_frontend" / "kling4"


def require_enabled():
    if not service.enabled():
        raise HTTPException(503, "Kling 4.0 пока не включён.")


async def identity(request: Request) -> dict:
    raw = request.headers.get("X-Telegram-Init-Data", "")
    if raw:
        try:
            user = verify_telegram(raw, os.getenv("TELEGRAM_BOT_TOKEN", ""))
        except (ValueError, TypeError, KeyError):
            raise HTTPException(401, "Заново откройте Kling 4.0 из Telegram-бота.")
        tg_id = int(user["id"])
        uid = await asyncio.to_thread(resolve_billing_user_id, tg_id)
        return {"id": uid, "billing_source_id": tg_id, "chat_id": tg_id}
    try:
        user = workspace_user_from_request(request)
    except Exception:
        user = None
    if not user:
        raise HTTPException(401, "Войдите на сайт или откройте Kling 4.0 из Telegram-бота.")
    user = await get_current_workspace_user(user)
    uid = int(user.get("workspace_user_id") or user.get("telegram_user_id") or 0)
    if uid <= 0:
        raise HTTPException(401, "Войдите заново.")
    return {"id": await asyncio.to_thread(resolve_billing_user_id, uid), "billing_source_id": uid, "chat_id": None}


async def mutation_identity(request: Request, user: dict = Depends(identity)) -> dict:
    # Signed Telegram data or a valid website session is mandatory. A bare UID
    # cannot authenticate. Cookie-auth writes also require a trusted browser origin.
    origin = (request.headers.get("origin") or "").rstrip("/")
    allowed = {v.strip().rstrip("/") for v in os.getenv("WORKSPACE_ALLOWED_ORIGINS", "https://nabex.ru,https://www.nabex.ru,https://astrabot-tchj.onrender.com").split(",")}
    if origin and origin not in allowed:
        raise HTTPException(403, "Недопустимый источник запроса.")
    if not origin and not user["chat_id"] and not request.headers.get("authorization"):
        raise HTTPException(403, "Для запроса требуется Origin.")
    return user


@router.get("/studio")
async def studio():
    return FileResponse(UI / "index.html", headers={"Cache-Control": "no-cache"})


@router.get("/ui/{filename}")
async def ui_file(filename: str):
    if filename not in {"app.js", "style.css"}:
        raise HTTPException(404)
    return FileResponse(UI / filename, media_type="text/javascript" if filename.endswith(".js") else "text/css", headers={"Cache-Control": "no-cache"})


@router.get("/config")
async def config():
    return {"enabled": service.enabled(), "rates": RATES, "max_bytes": MAX_BYTES,
            "duration_min": 3, "duration_max": 30, "resolutions": ["720p", "1080p"]}


@router.get("/me")
async def me(user: dict = Depends(identity)):
    # The billing helper needs the verified source ID to merge a linked legacy
    # Telegram balance. Passing only the canonical ID silently skips that merge.
    return {"id": user["id"], "balance": await asyncio.to_thread(get_balance, user["billing_source_id"]), "telegram": bool(user["chat_id"])}


@router.post("/upload")
async def upload(request: Request, user: dict = Depends(mutation_identity)):
    require_enabled()
    # No File/Form dependencies: authenticate before parsing or spooling input.
    # Bound the entire multipart stream, including fields and multipart headers.
    body_limit = MAX_BYTES + 65536
    length = request.headers.get("content-length")
    if length:
        try:
            if int(length) > body_limit:
                raise HTTPException(413, "Максимальный размер файла — 10 МБ.")
        except ValueError:
            raise HTTPException(400, "Неверный Content-Length.")
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "multipart/form-data":
        raise HTTPException(400, "Ожидается файл в multipart/form-data.")

    class UploadTooLarge(MultiPartException):
        pass

    async def bounded_stream():
        size = 0
        try:
            async for chunk in request.stream():
                size += len(chunk)
                if size > body_limit:
                    # MultiPartException also closes the parser's temporary files.
                    raise UploadTooLarge("Максимальный размер файла — 10 МБ.")
                yield chunk
        except ClientDisconnect:
            raise MultiPartException("Загрузка прервалась.")

    try:
        form = await MultiPartParser(request.headers, bounded_stream(), max_files=1, max_fields=1).parse()
    except UploadTooLarge as exc:
        raise HTTPException(413, exc.message)
    except (MultiPartException, ValueError):
        raise HTTPException(400, "Нужны один файл и поле kind.")
    try:
        file, kind = form.get("file"), form.get("kind")
        if not isinstance(file, UploadFile) or kind not in {"image", "video", "audio"}:
            raise HTTPException(400, "Нужны один файл и корректный тип материала.")
        raw = await file.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise HTTPException(413, "Максимальный размер файла — 10 МБ.")
        return await asyncio.to_thread(service.save_asset, user["id"], kind, raw)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    except RuntimeError:
        raise HTTPException(503, "Kling 4.0 ещё настраивается. Попробуйте позже.")
    finally:
        await form.close()


class QuoteIn(BaseModel):
    mode: str = "reference"
    prompt: str = Field(..., min_length=1, max_length=8000)
    duration: int = Field(default=5, ge=3, le=30, strict=True)
    resolution: str = "720p"
    aspect_ratio: str = "16:9"
    image_ids: list[str] = Field(default_factory=list, max_length=10)
    video_ids: list[str] = Field(default_factory=list, max_length=5)
    first_frame_id: str | None = None
    keyframe_ids: list[str] = Field(default_factory=list, max_length=8)
    last_frame_id: str | None = None
    keep_original_sound: bool = False
    elements: list[dict] = Field(default_factory=list, max_length=7)
    voices: list[dict] = Field(default_factory=list, max_length=3)


@router.post("/quote")
async def quote(payload: QuoteIn, user: dict = Depends(mutation_identity)):
    require_enabled()
    try:
        service.api_key()
        service.callback_url("configuration-check")
        return await asyncio.to_thread(service.create_quote, user["id"], user["chat_id"], payload.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except RuntimeError:
        raise HTTPException(503, "Kling 4.0 ещё настраивается. Попробуйте позже.")


class StartIn(BaseModel):
    quote_id: UUID


@router.post("/run")
async def start(payload: StartIn, user: dict = Depends(mutation_identity)):
    require_enabled()
    try:
        # Direct API clients can purchase without calling /me first. Use the
        # same initialization/legacy merge as the existing billing operations.
        await asyncio.to_thread(ensure_user_row, user["billing_source_id"])
        row = await asyncio.to_thread(service.rpc, "start", quote_id=str(payload.quote_id), user_id=user["id"])
        return public_job(row)
    except Exception as exc:
        message = str(exc)
        if "QUOTE_EXPIRED" in message:
            raise HTTPException(409, "Расчёт устарел. Нажмите «Рассчитать стоимость» ещё раз.")
        if "QUOTE_NOT_FOUND" in message:
            raise HTTPException(404, "Расчёт не найден.")
        if "TOO_MANY_JOBS" in message:
            raise HTTPException(429, "Дождитесь завершения текущих задач Kling 4.0.")
        if "insufficient" in message.lower() or "недостаточно" in message.lower():
            raise HTTPException(402, "Недостаточно токенов. Пополните баланс.")
        # A transport error can hide a successful atomic commit. Frontend keeps
        # the exact quote_id and retries it instead of buying a second job.
        raise HTTPException(503, "Ответ не получен. Повторите запуск с этим же расчётом: повторного списания не будет.")


@router.get("/jobs")
async def jobs(user: dict = Depends(identity)):
    rows = await asyncio.to_thread(lambda: service.db().table(service.JOBS).select("*").eq("user_id", user["id"]).is_("history_deleted_at", "null").order("created_at", desc=True).limit(40).execute().data or [])
    return {"items": [public_job(row) for row in rows]}


@router.get("/jobs/{job_id}")
async def job(job_id: UUID, user: dict = Depends(identity)):
    row = await asyncio.to_thread(service.get_job, str(job_id), user["id"])
    if not row:
        raise HTTPException(404, "Задача не найдена.")
    return public_job(row)


@router.post("/callback/{job_id}/{signature}")
async def callback(job_id: UUID, signature: str, request: Request):
    if not hmac.compare_digest(signature, service.callback_signature(str(job_id))):
        raise HTTPException(403)
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 65536:
            raise HTTPException(413)
    import json
    try:
        data = json.loads(raw)
        task_id = str((data.get("data") or {}).get("taskId") or data.get("taskId") or "")
        if not 1 <= len(task_id) <= 200:
            raise ValueError()
    except (ValueError, AttributeError):
        raise HTTPException(400, "Неверный callback.")
    row = await asyncio.to_thread(service.get_job, str(job_id))
    if not row:
        raise HTTPException(404)
    if row.get("provider_task_id") and row["provider_task_id"] != task_id:
        raise HTTPException(409)
    if row["status"] in {"completed", "failed"}:
        return {"ok": True}
    # Callback body is a notification only, never proof of payment or success.
    info = await service.task_info(task_id)
    try:
        service.verify_task(row, info, require_input=not bool(row.get("provider_task_id")))
    except ValueError:
        raise HTTPException(409, "Параметры задачи не совпадают.")
    await asyncio.to_thread(service.rpc, "attach", job_id=str(job_id), task_id=task_id)
    await service.settle_from_info(row, info)
    return {"ok": True}


class ReconcileIn(BaseModel):
    task_id: str = Field(..., min_length=1, max_length=200)


@router.post("/admin/jobs/{job_id}/reconcile")
async def reconcile(job_id: UUID, payload: ReconcileIn, request: Request, x_admin_token: str | None = Header(default=None)):
    require_admin_request(request, x_admin_token)
    row = await asyncio.to_thread(service.get_job, str(job_id))
    if not row:
        raise HTTPException(404)
    info = await service.task_info(payload.task_id)
    try:
        service.verify_task(row, info, require_input=True)
    except ValueError:
        raise HTTPException(409, "Параметры KIE не совпадают с исходной задачей.")
    await asyncio.to_thread(service.rpc, "attach", job_id=str(job_id), task_id=payload.task_id)
    await service.settle_from_info(row, info)
    return {"ok": True}
