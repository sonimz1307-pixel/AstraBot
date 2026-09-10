"""Web-first Trend Workshop, with strict public projections and cookie CSRF checks."""
from __future__ import annotations

import asyncio
import html
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.routing import APIRoute

from app.services import trend_service as svc
from app.services import trend_finance as finance
from app.services import trend_assets as assets
from app.services.admin_auth import require_admin_request
from app.services.workspace_auth import get_current_workspace_user, get_optional_workspace_user


class TrendRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handler(request: Request):
            try:
                return await original(request)
            except svc.TrendError as exc:
                raise HTTPException(exc.status, str(exc)) from exc
            except assets.TrendAssetError as exc:
                raise HTTPException(400, "Не удалось принять файл. Проверьте его формат, размер и доступ к тренду.") from exc
            except HTTPException:
                raise
            except Exception as exc:
                # RPC messages are mapped to safe user-facing errors. No stack,
                # SQL detail, private recipe, provider payload or secret URL.
                codes = {
                    "INSUFFICIENT_BALANCE": (402, "Недостаточно токенов."),
                    "Insufficient balance": (402, "Недостаточно токенов."),
                    "TREND_INSUFFICIENT_CASH": (402, "Для авторской наценки нужны токены, купленные за деньги. Бонусные токены не подходят."),
                    "TREND_CASH_BACKED_TOKENS_REQUIRED": (402, "Для авторской наценки нужны токены, купленные за деньги. Бонусные токены не подходят."),
                    "TREND_PRICE_CHANGED": (409, "Цена изменилась. Обновите расчёт."),
                    "TREND_FINANCIAL_CONFIG_CHANGED": (409, "Условия изменились. Обновите расчёт."),
                    "TREND_IDEMPOTENCY_CONFLICT": (409, "Ключ запуска уже использован для другой операции."),
                    "TREND_UNAVAILABLE": (409, "Этот тренд временно недоступен."),
                    "TREND_MODEL_DISABLED": (409, "Этот тренд временно недоступен."),
                    "TREND_VERSION_UNAVAILABLE": (409, "Рецепт изменился. Обновите страницу тренда."),
                    "TREND_MARKETPLACE_DISABLED": (404, "Мастерская трендов пока недоступна."),
                    "TREND_CREATOR_SUSPENDED": (409, "Запуски трендов этого автора временно приостановлены."),
                    "TREND_REWARDS_NOT_ENABLED_OR_BACKED": (409, "Тренды с авторской наценкой пока недоступны."),
                    "TREND_TEST_ALREADY_RUNNING": (409, "Тестовая генерация уже выполняется. Дождитесь её результата."),
                    "TREND_TEST_UNAVAILABLE": (409, "Тестовая генерация сейчас недоступна. Обновите состояние рецепта."),
                    "TREND_INPUT_": (400, "Проверьте файлы в обязательных слотах и загрузите их заново при необходимости."),
                    "TREND_UNKNOWN_INPUT_SLOT": (409, "Слоты рецепта изменились. Обновите страницу."),
                    "TREND_WALLET_FROZEN": (409, "Авторский баланс или выплаты временно приостановлены. Обратитесь в поддержку."),
                    "TREND_FORBIDDEN": (403, "Недостаточно прав."),
                    "PAYOUT": (409, "Выплата сейчас недоступна. Проверьте сумму, доступный баланс и статус выплат."),
                }
                for key, (status, message) in codes.items():
                    if key in str(exc):
                        raise HTTPException(status, message) from exc
                raise HTTPException(503, "Операция не завершена. Обновите страницу и проверьте её статус перед повтором.") from exc
        return handler


router = APIRouter(route_class=TrendRoute, tags=["trend-workshop"])
page_router = APIRouter(route_class=TrendRoute, include_in_schema=False)
ROOT = Path(__file__).resolve().parents[2] / "web_workspace_frontend"
ADMIN_ACTOR_ID = max(1, int(os.getenv("TREND_ADMIN_ACTOR_ID", "1")))


def mutation_guard(request: Request):
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    if request.headers.get("authorization", "").lower().startswith("bearer ") or request.headers.get("x-admin-token"):
        # Explicit credentials cannot be attached by an attacker-controlled form.
        return
    origin = request.headers.get("origin")
    if not origin:
        referer = request.headers.get("referer", "")
        parsed = urlparse(referer)
        origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else ""
    allowed = {f"{request.url.scheme}://{request.url.netloc}"}
    allowed.update(x.strip().rstrip("/") for x in os.getenv("WORKSPACE_ALLOWED_ORIGINS", "https://nabex.ru,https://www.nabex.ru").split(",") if x.strip())
    if not origin or origin.rstrip("/") not in allowed or origin == "null":
        raise HTTPException(403, "Недопустимый источник запроса.")


def admin(request: Request, x_admin_token: str | None = Header(None)):
    require_admin_request(request, x_admin_token)
    mutation_guard(request)
    return ADMIN_ACTOR_ID  # Verified single service-admin identity; never supplied by client.


def payload_object(payload):
    if not isinstance(payload, dict) or len(json.dumps(payload, ensure_ascii=False)) > 100000:
        raise svc.TrendError("Некорректный запрос.")
    return payload


@router.get("/api/trends/config")
def public_config():
    result = svc.public_config()
    if result.get("marketplace_enabled"):
        result["categories"] = svc.db().table("trend_categories").select("key,title").eq("enabled", True).order("position").execute().data or []
    return result


@router.get("/api/trends/models")
def model_registry(user: dict = Depends(get_current_workspace_user)):
    svc.require_creation()
    return {"items": svc.registry(creator=True)}


@router.get("/api/trends")
def catalog(type: str = "", sort: str = "trending", q: str = "", category: str = "", offset: int = 0, cursor: int | None = None, limit: int = 24):
    sorts = {"all": "trending", "newest": "new", "most_liked": "loved", "favorites": "loved", "nabex": "featured"}
    return svc.list_public(media_type=type, sort=sorts.get(sort, sort), q=q, category=category, offset=cursor if cursor is not None else offset, limit=limit)


@router.post("/api/trends/assets", dependencies=[Depends(mutation_guard)])
async def upload_asset(file: UploadFile = File(...), trend_id: str = Form(...), kind: str = Form(...), result_type: str = Form("image"), trend_version_id: str | None = Form(None), user: dict = Depends(get_current_workspace_user)):
    try:
        if kind not in assets.UPLOAD_KINDS:
            raise svc.TrendError("Загрузка демонстрационного результата запрещена.")
        if result_type not in {"image", "video", "audio"}:
            raise svc.TrendError("Неподдерживаемый тип файла.")
        await asyncio.to_thread(svc.config, required=True)
        if kind != "input":
            await asyncio.to_thread(svc.require_creation)
        owner = await asyncio.to_thread(svc.uid, user)
        maximum = await asyncio.to_thread(assets.preflight_upload, owner_id=owner, trend_id=trend_id, version_id=trend_version_id, kind=kind, result_type=result_type)
        await asyncio.to_thread(svc.rpc, "rate", user_id=owner, trend_id=svc.entity_id(trend_id), action="upload")
        if file.size is not None and file.size > maximum:
            raise svc.TrendError("Файл слишком большой.", 413)
        # Multipart data may already be spooled by ASGI. This bounds application
        # memory; deployment must ALSO set its reverse-proxy request body limit.
        raw = await file.read(maximum + 1)
    finally:
        await file.close()
    if len(raw) > maximum:
        raise svc.TrendError("Файл слишком большой.", 413)
    row = await asyncio.to_thread(assets.upload_asset, raw, owner_id=owner, trend_id=trend_id, version_id=trend_version_id, kind=kind, result_type=result_type)
    return {key: row.get(key) for key in ("id", "kind", "result_type", "content_type", "size_bytes", "metadata")}


@router.get("/api/trends/media/{asset_id}")
def published_media(asset_id: str):
    svc.config(required=True)
    row = svc.one("trend_assets", id=svc.entity_id(asset_id))
    if not row or row.get("deleted_at") or row.get("bucket") != assets.PUBLIC_BUCKET:
        raise svc.TrendError("Файл не найден.", 404)
    trend = svc.one("trends", id=row["trend_id"])
    if not trend or trend.get("status") != "published" or trend.get("deleted_at") or row.get("owner_id") != trend.get("creator_id"):
        raise svc.TrendError("Файл не найден.", 404)
    if row.get("kind") == "cover":
        allowed = trend.get("type") == "video" and row.get("result_type") == "image" and trend.get("video_cover_asset_id") == row["id"]
    elif row.get("kind") == "preview":
        version = svc.one("trend_versions", id=trend.get("current_version_id"), trend_id=trend["id"]) if trend.get("current_version_id") else None
        allowed = bool(version and version.get("preview_asset_id") == row["id"] and row.get("trend_version_id") == version["id"])
    else:
        allowed = False
    if not allowed:
        raise svc.TrendError("Файл не найден.", 404)
    # Signed access to approved media expires in 60s; draft/private references
    # never reach this branch. Do not cache this authorization redirect.
    return RedirectResponse(assets.signed_preview_url(row), status_code=307, headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})


@router.get("/api/trends/assets/{asset_id}")
def creator_asset(asset_id: str, user: dict = Depends(get_current_workspace_user)):
    owner = svc.uid(user)
    row = assets.get_owned_asset(asset_id, owner)
    raw = assets.read_owned_asset_bytes(asset_id, owner)
    return Response(raw, media_type=row["content_type"], headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'"})


@router.get("/api/trends/runs/{run_id}")
def run_status(run_id: str, user: dict = Depends(get_current_workspace_user)):
    row = svc.one("trend_runs", id=svc.entity_id(run_id), buyer_user_id=svc.uid(user))
    if not row:
        raise svc.TrendError("Генерация не найдена.", 404)
    return svc.safe_run(row)


@router.get("/api/trends/operations/{idempotency_key}")
def operation_status(idempotency_key: str, user: dict = Depends(get_current_workspace_user)):
    row = svc.one("trend_runs", idempotency_key=svc.entity_id(idempotency_key), buyer_user_id=svc.uid(user))
    if not row:
        raise svc.TrendError("Запуск пока не найден.", 404)
    return svc.safe_run(row)


@router.get("/api/trends/creators/{creator_id}")
def public_creator(creator_id: int):
    svc.config(required=True)
    profile = svc.creator_profile(creator_id)
    rows = svc.db().table("trends").select("*").eq("creator_id", creator_id).eq("status", "published").is_("deleted_at", "null").order("published_at", desc=True).limit(100).execute().data or []
    cache = svc.projection_cache(rows)
    return {**profile, "published_trends": len(rows), "likes_count": sum(int(r["likes_count"]) for r in rows), "successful_runs": sum(int(r["successful_runs"]) for r in rows), "items": [svc.public_trend(row, cache=cache) for row in rows]}


@router.get("/api/trends/{slug}")
def public_detail(slug: str, user: dict | None = Depends(get_optional_workspace_user)):
    svc.config(required=True)
    return svc.public_trend(svc.get_trend(slug), viewer_id=svc.uid(user) if user else None, detailed=True)


@router.post("/api/trends/{trend_id}/quote", dependencies=[Depends(mutation_guard)])
def quote(trend_id: str, payload: dict, user: dict = Depends(get_current_workspace_user)):
    payload_object(payload)
    return svc.create_quote(svc.entity_id(trend_id), svc.uid(user), is_test=payload.get("test") is True)


@router.post("/api/trends/{trend_id}/run", dependencies=[Depends(mutation_guard)])
def run_trend(trend_id: str, payload: dict, user: dict = Depends(get_current_workspace_user)):
    return svc.start_generation(trend_id, svc.uid(user), payload_object(payload))


@router.post("/api/trends/{trend_id}/like", dependencies=[Depends(mutation_guard)])
def like(trend_id: str, request: Request, user: dict = Depends(get_current_workspace_user)):
    svc.config(required=True)
    owner = svc.uid(user)
    _, ip_hash = svc.event_fingerprint(owner, "", request.client.host if request.client else "")
    return svc.rpc("like", trend_id=svc.entity_id(trend_id), user_id=owner, liked=True, ip_hash=ip_hash)


@router.delete("/api/trends/{trend_id}/like", dependencies=[Depends(mutation_guard)])
def unlike(trend_id: str, request: Request, user: dict = Depends(get_current_workspace_user)):
    svc.config(required=True)
    owner = svc.uid(user)
    _, ip_hash = svc.event_fingerprint(owner, "", request.client.host if request.client else "")
    return svc.rpc("like", trend_id=svc.entity_id(trend_id), user_id=owner, liked=False, ip_hash=ip_hash)


@router.post("/api/trends/{trend_id}/view", dependencies=[Depends(mutation_guard)])
def view(trend_id: str, payload: dict, request: Request, response: Response, user: dict | None = Depends(get_optional_workspace_user)):
    svc.config(required=True)
    ua = request.headers.get("user-agent", "").lower()
    if any(word in ua for word in ("bot", "crawler", "spider", "preview", "headless")):
        return {"recorded": False}
    session = request.cookies.get("nabex_trend_session", "")
    if not re.fullmatch(r"[a-f0-9]{32}", session):
        session = uuid4().hex
        response.set_cookie("nabex_trend_session", session, max_age=86400 * 30, secure=True, httponly=True, samesite="lax")
    owner = svc.uid(user) if user else None
    session_hash, ip_hash = svc.event_fingerprint(owner, session, request.client.host if request.client else "")
    source, meta = svc.traffic_source(payload_object(payload), str(payload.get("referrer") or "")[:1000])
    return svc.rpc("view", trend_id=svc.entity_id(trend_id), user_id=owner, session_hash=session_hash, ip_hash=ip_hash, source=source, metadata=meta)


@router.post("/api/trends/{trend_id}/reports", dependencies=[Depends(mutation_guard)])
def report(trend_id: str, payload: dict, user: dict = Depends(get_current_workspace_user)):
    svc.config(required=True)
    payload_object(payload)
    return svc.rpc("report", trend_id=svc.entity_id(trend_id), user_id=svc.uid(user), category=str(payload.get("category") or "other")[:80], description=str(payload.get("description") or payload.get("comment") or "")[:2000])


@router.get("/api/creator/trends")
def creator_trends(user: dict = Depends(get_current_workspace_user)):
    owner = svc.uid(user)
    rows = svc.rpc("creator_cards", user_id=owner) or []
    if isinstance(rows, dict):
        rows = [rows]
    cache = svc.projection_cache(rows, creator=True)
    return {"items": [svc.creator_detail(row, cache=cache) for row in rows], "stats": svc.creator_stats(owner)}


@router.post("/api/creator/trends", dependencies=[Depends(mutation_guard)])
def create_trend(payload: dict, user: dict = Depends(get_current_workspace_user)):
    return svc.save_trend(user, payload_object(payload))


@router.get("/api/creator/trends/{trend_id}")
def creator_trend(trend_id: str, user: dict = Depends(get_current_workspace_user)):
    return svc.creator_detail(svc.get_trend(trend_id, owner_id=svc.uid(user)))


@router.patch("/api/creator/trends/{trend_id}", dependencies=[Depends(mutation_guard)])
def edit_trend(trend_id: str, payload: dict, user: dict = Depends(get_current_workspace_user)):
    return svc.save_trend(user, payload_object(payload), trend_id=trend_id)


@router.post("/api/creator/trends/{trend_id}/test-generate", dependencies=[Depends(mutation_guard)])
def test_trend(trend_id: str, payload: dict, user: dict = Depends(get_current_workspace_user)):
    return svc.start_generation(trend_id, svc.uid(user), payload_object(payload), is_test=True)


@router.post("/api/creator/trends/{trend_id}/submit", dependencies=[Depends(mutation_guard)])
def submit_trend(trend_id: str, payload: dict | None = None, user: dict = Depends(get_current_workspace_user)):
    svc.require_creation()
    return svc.change_lifecycle(trend_id, svc.uid(user), "submit", expected_revision=(payload or {}).get("revision"))


@router.post("/api/creator/trends/{trend_id}/hide", dependencies=[Depends(mutation_guard)])
def hide_trend(trend_id: str, payload: dict | None = None, user: dict = Depends(get_current_workspace_user)):
    return svc.change_lifecycle(trend_id, svc.uid(user), "hide", expected_revision=(payload or {}).get("revision"))


@router.post("/api/creator/trends/{trend_id}/restore", dependencies=[Depends(mutation_guard)])
def restore_trend(trend_id: str, payload: dict | None = None, user: dict = Depends(get_current_workspace_user)):
    return svc.change_lifecycle(trend_id, svc.uid(user), "restore", expected_revision=(payload or {}).get("revision"))


@router.delete("/api/creator/trends/{trend_id}", dependencies=[Depends(mutation_guard)])
def delete_trend(trend_id: str, user: dict = Depends(get_current_workspace_user)):
    return svc.change_lifecycle(trend_id, svc.uid(user), "delete")


@router.get("/api/creator/trends/{trend_id}/stats")
def trend_stats(trend_id: str, user: dict = Depends(get_current_workspace_user)):
    return svc.creator_stats(svc.uid(user), trend_id)


@router.get("/api/creator/wallet")
def wallet(user: dict = Depends(get_current_workspace_user)):
    return finance.wallet(svc.uid(user)) | {"min_payout_kopecks": svc.config().get("min_payout_kopecks", 100000)}


@router.get("/api/creator/ledger")
def ledger(user: dict = Depends(get_current_workspace_user)):
    return {"items": finance.ledger(svc.uid(user))}


@router.get("/api/creator/payouts")
def payouts(user: dict = Depends(get_current_workspace_user)):
    return {"items": finance.payouts(svc.uid(user))}


@router.post("/api/creator/payouts", dependencies=[Depends(mutation_guard)])
def request_payout(payload: dict, user: dict = Depends(get_current_workspace_user)):
    payload_object(payload)
    if not svc.config().get("payouts_enabled"):
        raise svc.TrendError("Вывод средств пока не включён.", 403)
    amount = payload.get("amount_kopecks")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount < 1:
        raise svc.TrendError("Укажите сумму выплаты.")
    details = payload.get("details") or {"payout_details": str(payload.get("payout_details") or "")[:4000]}
    if not isinstance(details, dict) or len(json.dumps(details)) > 8000:
        raise svc.TrendError("Проверьте реквизиты выплаты.")
    return finance.request_payout(svc.uid(user), amount, idempotency_key=svc.entity_id(payload.get("idempotency_key")), details=details)


@router.post("/api/creator/payouts/{payout_id}/cancel", dependencies=[Depends(mutation_guard)])
def cancel_payout(payout_id: str, user: dict = Depends(get_current_workspace_user)):
    return finance.cancel_own_payout(svc.uid(user), svc.entity_id(payout_id))


@router.get("/api/admin/trends/config", dependencies=[Depends(admin)])
def admin_config():
    return svc.config()


@router.patch("/api/admin/trends/config", dependencies=[Depends(admin)])
def admin_save_config(payload: dict):
    return svc.rpc("admin_config", patch=svc.validate_config(payload_object(payload)), admin_id=ADMIN_ACTOR_ID)


@router.get("/api/admin/trends/models", dependencies=[Depends(admin)])
def admin_models():
    return {"items": svc.registry()}


@router.patch("/api/admin/trends/models", dependencies=[Depends(admin)])
def admin_save_models(payload: dict):
    payload_object(payload)
    from app.services.trend_registry import get_model
    models = payload.get("models", [payload])
    if not isinstance(models, list) or len(models) > 20:
        raise svc.TrendError("Некорректные настройки моделей.")
    changes = []
    for item in models:
        spec = get_model(item.get("model_key"))
        patch = {k: item[k] for k in ("enabled", "creation_enabled") if k in item}
        if "run_enabled" in item:
            patch["enabled"] = item["run_enabled"]
        if any(not isinstance(v, bool) for v in patch.values()):
            raise svc.TrendError("Некорректный переключатель модели.")
        changes.append({"model_key": spec["key"], **patch})
    return svc.rpc("admin_models", models=changes)


@router.get("/api/admin/trends/reports", dependencies=[Depends(admin)])
def admin_reports():
    return {"items": svc.db().table("trend_reports").select("*").order("created_at", desc=True).limit(200).execute().data or []}


@router.post("/api/admin/trends/reports/{report_id}", dependencies=[Depends(admin)])
@router.patch("/api/admin/trends/reports/{report_id}", dependencies=[Depends(admin)])
def admin_resolve_report(report_id: str, payload: dict):
    if payload.get("status") not in {"resolved", "dismissed"}:
        raise svc.TrendError("Некорректный статус жалобы.")
    return svc.db().table("trend_reports").update({"status": payload["status"], "admin_note": str(payload.get("note") or "")[:2000]}).eq("id", svc.entity_id(report_id)).execute().data


@router.get("/api/admin/trends/creators", dependencies=[Depends(admin)])
def admin_creators():
    rows = svc.db().table("trend_creators").select("*").order("created_at", desc=True).limit(200).execute().data or []
    return {"items": [r | {"wallet": finance.wallet(r["user_id"])} for r in rows]}


@router.get("/api/admin/trends/creators/{creator_id}", dependencies=[Depends(admin)])
def admin_creator(creator_id: int):
    creator = svc.one("trend_creators", user_id=creator_id)
    if not creator:
        raise svc.TrendError("Автор не найден.", 404)
    runs = svc.db().table("trend_runs").select("id,buyer_user_id,status,total_tokens,created_at,is_test").eq("creator_id", creator_id).order("created_at", desc=True).limit(200).execute().data or []
    inbound = {}
    for run in runs:
        if run["status"] == "completed" and not run["is_test"]:
            buyer = int(run["buyer_user_id"])
            inbound[buyer] = inbound.get(buyer, 0) + 1
    outbound = svc.db().table("trend_runs").select("creator_id,total_tokens,created_at").eq("buyer_user_id", creator_id).eq("status", "completed").eq("is_test", False).order("created_at", desc=True).limit(200).execute().data or []
    circular = sorted({int(r["creator_id"]) for r in outbound if int(r["creator_id"]) in inbound and int(r["creator_id"]) != creator_id})
    return {"creator": creator, "wallet": finance.wallet(creator_id), "ledger": finance.ledger(creator_id),
            "signals": {"recent_runs": runs, "self_uses": sum(1 for r in runs if r["buyer_user_id"] == creator_id and not r["is_test"]),
                        "unique_buyers": len({r["buyer_user_id"] for r in runs}), "circular_generation_accounts": circular,
                        "repeated_buyers": [{"user_id": key, "successful_runs": value} for key, value in sorted(inbound.items(), key=lambda item: -item[1]) if value >= 5],
                        "scope": "Последние 200 запусков. Сигналы требуют ручной проверки и сами по себе не доказывают нарушение."}}



@router.post("/api/admin/trends/creators/{creator_id}/freeze", dependencies=[Depends(admin)])
def freeze(creator_id: int, payload: dict):
    if not all(isinstance(payload.get(k), bool) for k in ("balance_frozen", "payouts_frozen")) or not str(payload.get("reason") or "").strip():
        raise svc.TrendError("Укажите причину и параметры блокировки.")
    return finance.set_freeze(creator_id, balance_frozen=payload["balance_frozen"], payouts_frozen=payload["payouts_frozen"], admin_id=ADMIN_ACTOR_ID, reason=str(payload["reason"])[:2000])


@router.post("/api/admin/trends/creators/{creator_id}/status", dependencies=[Depends(admin)])
def creator_status(creator_id: int, payload: dict):
    if payload.get("status") not in {"active", "suspended"} or not payload.get("reason"):
        raise svc.TrendError("Укажите статус и причину.")
    return svc.rpc("creator_status", user_id=creator_id, status=payload["status"], reason=str(payload["reason"])[:2000])


@router.get("/api/admin/trends/payouts", dependencies=[Depends(admin)])
def admin_payouts():
    return {"items": svc.db().table("creator_payouts").select("*").order("created_at", desc=True).limit(200).execute().data or []}


@router.post("/api/admin/trends/payouts/{payout_id}", dependencies=[Depends(admin)])
def admin_payout_action(payout_id: str, payload: dict):
    action = {"paid": "pay", "rejected": "reject", "approved": "approve"}.get(payload.get("action"), payload.get("action"))
    if action not in {"approve", "pay", "reject", "cancel"}:
        raise svc.TrendError("Неизвестное действие.")
    evidence = str(payload.get("payment_reference") or "")[:1000]
    note = str(payload.get("note") or "")[:2000]
    if action == "pay" and not (evidence or note).strip():
        raise svc.TrendError("Укажите подтверждение фактической выплаты.")
    return finance.update_payout(payout_id, action, admin_id=ADMIN_ACTOR_ID, note=(note + " " + evidence).strip())


@router.get("/api/admin/trends/finance", dependencies=[Depends(admin)])
def admin_finance():
    return {"wallets": svc.db().table("creator_wallets").select("*").order("updated_at", desc=True).limit(200).execute().data or [],
            "items": svc.db().table("creator_ledger").select("*").order("created_at", desc=True).limit(200).execute().data or [],
            "runs": svc.db().table("trend_runs").select("id,trend_id,buyer_user_id,creator_id,status,total_tokens,creator_reward_kopecks,created_at,error").eq("status", "needs_reconciliation").limit(200).execute().data or [],
            "archives": svc.db().table("workspace_video_archive_jobs").select("generation_id,user_id,status,attempts,next_attempt_at,last_error_code,updated_at").in_("status", ["pending", "running", "needs_review"]).order("updated_at").limit(100).execute().data or []}


@router.post("/api/admin/trends/video-archives/{generation_id}/retry", dependencies=[Depends(admin)])
def admin_retry_video_archive(generation_id: str):
    from app.services.workspace_video_archive import rpc
    return rpc("retry", generation_id=svc.entity_id(generation_id))


@router.post("/api/admin/trends/runs/{run_id}/refund", dependencies=[Depends(admin)])
def admin_refund(run_id: str, payload: dict):
    if not str(payload.get("reason") or "").strip():
        raise svc.TrendError("Укажите основание возврата после проверки операции.")
    return finance.refund_run(run_id, reason=str(payload["reason"])[:2000], allow_completed=True)


@router.get("/api/admin/trends/assets/{asset_id}", dependencies=[Depends(admin)])
def admin_asset(asset_id: str):
    row = svc.one("trend_assets", id=svc.entity_id(asset_id))
    if not row:
        raise svc.TrendError("Файл не найден.", 404)
    raw = assets.read_owned_asset_bytes(asset_id, row["owner_id"])
    return Response(raw, media_type=row["content_type"], headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'"})


@router.get("/api/admin/trends", dependencies=[Depends(admin)])
def admin_list(status: str = ""):
    query = svc.db().table("trends").select("*").is_("deleted_at", "null")
    if status == "moderation":
        query = query.eq("review_pending", True)
    elif status:
        query = query.eq("status", status)
    rows = query.order("updated_at", desc=True).limit(100).execute().data or []
    cache = svc.projection_cache(rows, creator=True)
    return {"items": [svc.creator_detail(row, cache=cache) for row in rows]}


@router.get("/api/admin/trends/categories", dependencies=[Depends(admin)])
def categories():
    return {"items": svc.db().table("trend_categories").select("*").order("position").execute().data or []}


@router.patch("/api/admin/trends/categories", dependencies=[Depends(admin)])
def edit_category(payload: dict):
    if not re.fullmatch(r"[a-z0-9_-]{1,80}", str(payload.get("key") or "")) or not str(payload.get("title") or "").strip():
        raise svc.TrendError("Укажите ключ и название категории.")
    return svc.db().table("trend_categories").upsert({"key": payload["key"], "title": str(payload["title"])[:120], "position": int(payload.get("position") or 0), "enabled": payload.get("enabled", True) is True}).execute().data


@router.get("/api/admin/trends/{trend_id}", dependencies=[Depends(admin)])
def admin_detail(trend_id: str):
    trend = svc.get_trend(trend_id, admin=True)
    result = svc.creator_detail(trend)
    if trend.get("review_pending"):
        result["version"] = svc.one("trend_versions", id=trend["review_version_id"])
        result["slots"] = svc.slots_for(trend["review_version_id"])
        result["fixed_assets"] = svc.db().table("trend_fixed_assets").select("*").eq("trend_version_id", trend["review_version_id"]).order("position").execute().data or []
    return result | {"trend": (trend | (trend.get("review_metadata") or {})) if trend.get("review_pending") else trend, "recipe": (result.get("version") or {}).get("recipe_json", {}), "revision": trend["revision"]}


@router.post("/api/admin/trends/{trend_id}/moderate", dependencies=[Depends(admin)])
def moderate(trend_id: str, payload: dict):
    return svc.rpc("moderate", trend_id=svc.entity_id(trend_id), action=str(payload.get("action") or ""), reason=str(payload.get("reason") or "")[:2000], expected_revision=payload.get("revision"), admin_id=ADMIN_ACTOR_ID)


@router.post("/api/admin/trends/{trend_id}/featured", dependencies=[Depends(admin)])
def featured(trend_id: str, payload: dict):
    if not isinstance(payload.get("featured"), bool):
        raise svc.TrendError("Некорректное значение.")
    return svc.db().table("trends").update({"featured": payload["featured"]}).eq("id", svc.entity_id(trend_id)).execute().data




def shell(*, trend: dict | None = None, admin_page: bool = False, unavailable: str | None = None) -> HTMLResponse:
    path = ROOT / ("trends-admin.html" if admin_page else "trends.html")
    source = path.read_text(encoding="utf-8")
    if trend:
        public = svc.public_trend(trend, detailed=True)
        canonical = (os.getenv("TREND_PUBLIC_ORIGIN") or "https://nabex.ru").rstrip("/") + public["url"]
        title = html.escape(public["title"] + " — Nabex")
        description = html.escape(public["description"][:300], quote=True)
        source = re.sub(r"<title>.*?</title>", lambda _: "<title>" + title + "</title>", source, count=1)
        source = re.sub(r'<meta name="description"[^>]*>', lambda _: '<meta name="description" content="' + description + '">', source, count=1)
        source = re.sub(r'<link rel="canonical"[^>]*>', lambda _: '<link rel="canonical" href="' + html.escape(canonical, quote=True) + '">', source, count=1)
        og = '<meta property="og:type" content="website"><meta property="og:title" content="' + title + '"><meta property="og:description" content="' + description + '"><meta property="og:url" content="' + html.escape(canonical, quote=True) + '">'
        if public.get("image_url"):
            og += '<meta property="og:image" content="' + html.escape(public["image_url"], quote=True) + '"><meta name="twitter:card" content="summary_large_image">'
        source = source.replace('</head>', og + '</head>')
        source = source.replace('</main>', '<noscript><h1>' + html.escape(public["title"]) + '</h1><p>' + html.escape(public["description"]) + '</p><p>Включите JavaScript, чтобы загрузить материалы и создать результат.</p></noscript></main>', 1)
    if unavailable:
        source = re.sub(r'<main\b[^>]*>.*?</main>', '<main class="tw-main"><div class="tw-empty">' + html.escape(unavailable) + '</div></main>', source, count=1, flags=re.S)
        source = re.sub(r'<script\b[^>]*>.*?</script>', '', source, flags=re.S)
        source = source.replace('</head>', '<meta name="robots" content="noindex,nofollow"></head>')
    return HTMLResponse(source, status_code=410 if unavailable == "Тренд удалён." else 404 if unavailable else 200,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "strict-origin-when-cross-origin"})


@page_router.get("/trends")
@page_router.get("/trends.html")
def trend_catalog_page():
    if not svc.public_config().get("marketplace_enabled"):
        return shell(unavailable="Мастерская трендов пока недоступна.")
    return shell()


@page_router.get("/trend/{slug}")
def trend_page(slug: str):
    try:
        svc.config(required=True)
        trend = svc.get_trend(slug)
    except svc.TrendError as exc:
        return shell(unavailable="Тренд удалён." if exc.status == 410 else "Этот тренд временно недоступен.")
    return shell(trend=trend)


@page_router.get("/trends-admin.html")
def trend_admin_page():
    return shell(admin_page=True)


@page_router.get("/trends-sitemap.xml")
def sitemap():
    if not svc.public_config().get("marketplace_enabled"):
        return Response('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"/>', media_type="application/xml")
    rows = []
    last_id = None
    while len(rows) < 50000:
        query = svc.db().table("trends").select("id,slug,updated_at").eq("status", "published").is_("deleted_at", "null").order("id").limit(500)
        if last_id:
            query = query.gt("id", last_id)
        batch = query.execute().data or []
        rows.extend(batch)
        if len(batch) < 500:
            break
        last_id = batch[-1]["id"]

    origin = (os.getenv("TREND_PUBLIC_ORIGIN") or "https://nabex.ru").rstrip("/")
    body = ''.join('<url><loc>' + html.escape(origin + '/trend/' + r['slug']) + '</loc><lastmod>' + html.escape(r['updated_at']) + '</lastmod></url>' for r in rows)
    return Response('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + body + '</urlset>', media_type="application/xml")


@page_router.get("/trends.css")
@page_router.get("/trends.js")
@page_router.get("/trends-admin.js")
@page_router.get("/trends-nav.js")
def trend_static(request: Request):
    return FileResponse(ROOT / request.url.path.rsplit('/', 1)[-1], headers={"Cache-Control": "public, max-age=300", "X-Content-Type-Options": "nosniff"})
