"""Marketplace orchestration. Public projections never serialize recipe rows.

All mutations with lifecycle/financial races go through PostgreSQL RPCs. The
generation adapters and the existing user balance remain the source of truth.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from db_supabase import supabase
from billing_db import resolve_billing_user_id


class TrendError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def db():
    if supabase is None:
        raise TrendError("Мастерская временно недоступна.", 503)
    return supabase


def rpc(name: str, **kwargs):
    try:
        result = db().rpc("nabex_trend_" + name, {"p_" + k: v for k, v in kwargs.items()}).execute().data
        if isinstance(result, list) and len(result) == 1:
            return result[0]
        return result
    except TrendError:
        raise
    except Exception as exc:
        # Never expose SQL, provider payloads, prompts or signed storage URLs.
        msg = str(exc)
        codes = {
            "TREND_NOT_FOUND": ("Тренд не найден или удалён.", 404),
            "TREND_FORBIDDEN": ("Недостаточно прав.", 403),
            "TREND_UNAVAILABLE": ("Этот тренд временно недоступен.", 409),
            "TREND_CONFLICT": ("Тренд изменился. Обновите страницу.", 409),
            "TREND_TEST_REQUIRED": ("Сначала выполните успешную тестовую генерацию.", 409),
            "TREND_COVER_REQUIRED": ("Добавьте обложку видео.", 400),
            "TREND_RATE_LIMIT": ("Слишком много действий. Попробуйте позже.", 429),
            "TREND_INVALID": ("Проверьте введённые данные.", 400),
        }
        for code, (message, status) in codes.items():
            if code in msg:
                raise TrendError(message, status) from exc
        raise TrendError("Операция не завершена. Обновите страницу и проверьте её статус.", 503) from exc


def one(table: str, **filters) -> dict | None:
    query = db().table(table).select("*")
    for key, value in filters.items():
        query = query.eq(key, value)
    rows = query.limit(1).execute().data or []
    return rows[0] if rows else None


def uid(user: dict) -> int:
    value = int(user.get("workspace_user_id") or user.get("telegram_user_id") or 0)
    if value <= 0:
        raise TrendError("Войдите в аккаунт.", 401)
    return resolve_billing_user_id(value)


def entity_id(value: Any) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise TrendError("Некорректный идентификатор.")


def config(*, required: bool = False) -> dict:
    row = one("trend_settings", singleton=True)
    settings = dict((row or {}).get("config") or {})
    for env, key in (("TREND_MARKETPLACE_ENABLED", "marketplace_enabled"),
                     ("TREND_CREATION_ENABLED", "creation_enabled"),
                     ("CREATOR_PAYOUTS_ENABLED", "payouts_enabled")):
        # Environment may turn a DB flag OFF as an emergency switch; cannot
        # turn it on before the database launch gates have been configured.
        if os.getenv(env, "").lower() in {"0", "false", "off", "no"}:
            settings[key] = False
    if required and not settings.get("marketplace_enabled"):
        raise TrendError("Мастерская трендов пока недоступна.", 404)
    return settings


def public_config() -> dict:
    try:
        settings = config()
    except Exception:
        return {"marketplace_enabled": False, "creation_enabled": False, "payouts_enabled": False}
    return {key: settings.get(key, False) for key in
            ("marketplace_enabled", "creation_enabled", "payouts_enabled", "rewards_enabled")} | {
                "min_markup_tokens": settings.get("min_markup_tokens", 0),
                "max_markup_tokens": settings.get("max_markup_tokens", 1000),
                "allow_zero_markup": settings.get("allow_zero_markup", True),
                "min_payout_kopecks": settings.get("min_payout_kopecks", 100000),
            }


def require_creation():
    settings = config(required=True)
    if not settings.get("creation_enabled"):
        raise TrendError("Создание трендов временно недоступно.", 403)
    return settings


def creator_profile(user_id: int) -> dict:
    row = one("trend_creators", user_id=user_id) or {}
    return {"id": user_id, "display_name": row.get("display_name") or "Автор Nabex",
            "username": row.get("username") or "", "avatar_url": row.get("avatar_url") or ""}


def ensure_creator(user: dict) -> int:
    user_id = uid(user)
    row = one("trend_creators", user_id=user_id)
    if row and row.get("status") in {"blocked", "suspended"}:
        raise TrendError("Создание и публикация трендов для этого автора приостановлены.", 403)
    if not row:
        name = str(user.get("first_name") or user.get("username") or "Автор Nabex")[:100]
        db().table("trend_creators").upsert({"user_id": user_id, "display_name": name,
                    "username": str(user.get("username") or "")[:80]}, on_conflict="user_id",
                    ignore_duplicates=True).execute()
    return user_id


def get_trend(value: str, *, owner_id: int | None = None, admin: bool = False) -> dict:
    try:
        ident = str(UUID(value))
        row = one("trends", id=ident)
    except ValueError:
        row = one("trends", slug=value)
    if not row:
        raise TrendError("Тренд не найден.", 404)
    if admin:
        return row
    if row.get("deleted_at"):
        raise TrendError("Тренд удалён.", 410)
    if owner_id is not None:
        if int(row["creator_id"]) != owner_id:
            raise TrendError("Недостаточно прав.", 403)
        return row
    if row.get("status") != "published" or not row.get("current_version_id"):
        raise TrendError("Этот тренд временно недоступен.", 404)
    return row


def version_for(trend: dict, *, creator: bool = False) -> dict:
    version_id = (trend.get("draft_version_id") if creator else None) or trend.get("current_version_id")
    if not version_id:
        return {}
    return one("trend_versions", id=version_id, trend_id=trend["id"]) or {}


def slots_for(version_id: str | None, *, public: bool = False) -> list:
    if not version_id:
        return []
    rows = db().table("trend_input_slots").select("*").eq("trend_version_id", version_id).order("position").execute().data or []
    if public:
        keys = ("id", "position", "input_type", "title", "instruction", "required", "min_files", "max_files", "validation_json")
        return [{key: row.get(key) for key in keys} for row in rows]
    return rows


def asset_public_url(asset_id: str | None) -> str | None:
    if not asset_id:
        return None
    from app.services.trend_assets import public_asset_url
    asset = one("trend_assets", id=asset_id)
    return public_asset_url(asset) if asset else None


def registry(*, creator: bool = False) -> list:
    from app.services.trend_registry import list_models
    allowed = {r["model_key"]: r for r in (db().table("trend_enabled_models").select("*").execute().data or [])}
    result = []
    for model in list_models():
        key = model["model_key"]
        state = allowed.get(key, {})
        if creator and (not state.get("enabled") or not state.get("creation_enabled")):
            continue
        result.append(dict(model, enabled=bool(state.get("enabled")), creation_enabled=bool(state.get("creation_enabled"))))
    return result


def current_price(trend: dict, version: dict, *, buyer_id: int | None = None, is_test: bool = False, cache: dict | None = None) -> dict:
    from app.services.trend_registry import base_price
    cache = cache if cache is not None else {}
    if "settings" not in cache:
        cache["settings"] = config(required=True)
    settings = cache["settings"]
    # Cache only inside this request. Pricing can change before the next quote.
    prices = cache.setdefault("base_prices", {})
    price_key = str(version.get("recipe_hash") or json.dumps(version["recipe_json"], sort_keys=True, separators=(",", ":")))
    if price_key not in prices:
        prices[price_key] = int(base_price(version["recipe_json"]))
    base = prices[price_key]
    markup = 0 if is_test or buyer_id == int(trend["creator_id"]) else int(trend["creator_markup_tokens"])
    rate = int(settings.get("settlement_kopecks_per_markup_token", 0))
    fee = int(settings.get("marketplace_fee_bps", 0))
    reward = markup * rate * (10000 - fee) // 10000
    return {"base_tokens": base, "markup_tokens": markup, "total_tokens": base + markup,
            "expected_creator_reward_kopecks": reward,
            "financial_config": {k: settings.get(k) for k in (
                "settlement_kopecks_per_markup_token", "marketplace_fee_bps", "hold_seconds", "rewards_enabled",
                "max_backed_settlement_kopecks_per_token")}}


def public_trend(trend: dict, *, viewer_id: int | None = None, detailed: bool = False, cache: dict | None = None) -> dict:
    from app.services.trend_assets import public_asset_url
    cache = cache if cache is not None else {}
    if "settings" not in cache:
        cache["settings"] = config(required=True)
    versions = cache.setdefault("versions", {})
    version_id = str(trend.get("current_version_id") or "")
    if version_id not in versions:
        versions[version_id] = version_for(trend)
    version = versions[version_id]
    if not version or str(version.get("trend_id")) != str(trend["id"]):
        raise TrendError("Этот тренд временно недоступен.", 404)
    models = cache.setdefault("models", {})
    model_key = version["model_key"]
    if model_key not in models:
        models[model_key] = one("trend_enabled_models", model_key=model_key) or {}
    allowed = models[model_key]
    asset_rows = cache.setdefault("assets", {})
    asset_urls = cache.setdefault("asset_urls", {})

    def asset_url(asset_id):
        if not asset_id:
            return None
        if asset_id not in asset_urls:
            if asset_id not in asset_rows:
                asset_rows[asset_id] = one("trend_assets", id=asset_id)
            asset = asset_rows[asset_id]
            asset_urls[asset_id] = public_asset_url(asset) if asset and not asset.get("deleted_at") else None
        return asset_urls[asset_id]

    preview = asset_url(version.get("preview_asset_id"))
    cover = asset_url(trend.get("video_cover_asset_id")) if trend["type"] == "video" else None
    price = None
    available = bool(allowed.get("enabled"))
    try:
        price = current_price(trend, version, buyer_id=viewer_id, cache=cache)["total_tokens"]
        if int(trend.get("creator_markup_tokens") or 0) and not cache["settings"].get("rewards_enabled") and viewer_id != int(trend["creator_id"]):
            available = False
    except Exception:
        available = False
    creators = cache.setdefault("creators", {})
    creator_id = int(trend["creator_id"])
    if creator_id not in creators:
        creators[creator_id] = creator_profile(creator_id)
    result = {key: trend.get(key) for key in
              ("id", "slug", "type", "title", "description", "category", "tags", "language", "published_at")}
    result.update({"preview_url": preview, "cover_url": cover, "image_url": cover or preview,
        "creator": creators[creator_id], "price_tokens": price,
        "likes_count": int(trend.get("likes_count") or 0), "successful_runs": int(trend.get("successful_runs") or 0),
        "available": available, "url": "/trend/" + trend["slug"], "liked": False,
        "current_version_id": version["id"]})
    if viewer_id:
        likes = cache.setdefault("likes", {})
        key = (trend["id"], viewer_id)
        if key not in likes:
            likes[key] = bool(one("trend_likes", trend_id=trend["id"], user_id=viewer_id))
        result["liked"] = likes[key]
    if detailed:
        result["slots"] = slots_for(version["id"], public=True)
    return result


def list_public(*, media_type: str = "", sort: str = "trending", q: str = "", category: str = "", offset: int = 0, limit: int = 24) -> dict:
    settings = config(required=True)
    if media_type not in {"", "photo", "video"}:
        raise TrendError("Неизвестный тип тренда.")
    safe_offset = max(0, min(offset, 10000))
    safe_limit = max(1, min(limit, 48))
    rows = rpc("catalog", media_type=media_type, sort=sort, query=q[:120], category=category[:80],
               offset=safe_offset, limit=safe_limit) or []
    if isinstance(rows, dict):
        rows = [rows]
    cache = {"settings": settings, "versions": {}, "models": {}, "assets": {}, "creators": {}}

    def batch(table, column, values):
        values = list({v for v in values if v is not None})
        if not values:
            return []
        return db().table(table).select("*").in_(column, values).execute().data or []

    version_ids = [row.get("current_version_id") for row in rows]
    cache["versions"] = {str(v["id"]): v for v in batch("trend_versions", "id", version_ids)}
    # Cache missing rows too, to avoid a hidden N+1 fallback for invalid data.
    for value in version_ids:
        cache["versions"].setdefault(str(value or ""), {})
    versions = [v for v in cache["versions"].values() if v]
    model_keys = [v["model_key"] for v in versions]
    cache["models"] = {m["model_key"]: m for m in batch("trend_enabled_models", "model_key", model_keys)}
    for value in model_keys:
        cache["models"].setdefault(value, {})
    asset_ids = [v.get("preview_asset_id") for v in versions] + [r.get("video_cover_asset_id") for r in rows if r.get("type") == "video"]
    cache["assets"] = {a["id"]: a for a in batch("trend_assets", "id", asset_ids)}
    for value in asset_ids:
        if value:
            cache["assets"].setdefault(value, None)
    creator_ids = [int(r["creator_id"]) for r in rows]
    for creator in batch("trend_creators", "user_id", creator_ids):
        creator_id = int(creator["user_id"])
        cache["creators"][creator_id] = {"id": creator_id, "display_name": creator.get("display_name") or "Автор Nabex",
            "username": creator.get("username") or "", "avatar_url": creator.get("avatar_url") or ""}
    for value in creator_ids:
        cache["creators"].setdefault(value, {"id": value, "display_name": "Автор Nabex", "username": "", "avatar_url": ""})
    items = [public_trend(row, cache=cache) for row in rows]
    return {"items": items, "next_cursor": safe_offset + len(items) if len(items) == safe_limit else None}


def projection_cache(rows: list, *, creator: bool = False) -> dict:
    """Batch relations for creator/profile/admin lists within one HTTP request."""
    cache = {"settings": config(), "versions": {}, "models": {}, "assets": {}, "creators": {}, "slots": {}, "fixed": {}}
    def batch(table, field, values):
        values = list({str(v) for v in values if v is not None})
        return db().table(table).select("*").in_(field, values).execute().data or [] if values else []
    ids = [(r.get("draft_version_id") if creator else None) or r.get("current_version_id") for r in rows]
    versions = batch("trend_versions", "id", ids)
    cache["versions"] = {v["id"]: v for v in versions}
    cache["models"] = {m["model_key"]: m for m in batch("trend_enabled_models", "model_key", [v["model_key"] for v in versions])}
    covers = [((r.get("pending_metadata") or {}).get("video_cover_asset_id", r.get("video_cover_asset_id")) if creator else r.get("video_cover_asset_id")) for r in rows]
    asset_ids = [v.get("preview_asset_id") for v in versions] + covers
    cache["assets"] = {a["id"]: a for a in batch("trend_assets", "id", asset_ids)}
    for c in batch("trend_creators", "user_id", [r["creator_id"] for r in rows]):
        cache["creators"][int(c["user_id"])] = {"id": c["user_id"], "display_name": c.get("display_name") or "Автор Nabex", "username": c.get("username") or "", "avatar_url": c.get("avatar_url") or ""}
    if creator:
        for table, key in (("trend_input_slots", "slots"), ("trend_fixed_assets", "fixed")):
            for item in batch(table, "trend_version_id", ids):
                cache[key].setdefault(item["trend_version_id"], []).append(item)
            for items in cache[key].values():
                items.sort(key=lambda x: x.get("position", 0))
    return cache


def creator_detail(trend: dict, *, cache: dict | None = None) -> dict:
    vid = trend.get("draft_version_id") or trend.get("current_version_id")
    version = cache["versions"].get(vid, {}) if cache is not None else version_for(trend, creator=True)
    result = dict(trend)
    result.update(trend.get("pending_metadata") or {})
    result["creator"] = cache["creators"].get(int(trend["creator_id"]), {}) if cache is not None else creator_profile(int(trend["creator_id"]))
    result["version"] = version
    result["draft_status"] = version.get("status")
    result["slots"] = cache["slots"].get(vid, []) if cache is not None else slots_for(version.get("id"))
    result["fixed_assets"] = cache["fixed"].get(vid, []) if cache is not None else ((db().table("trend_fixed_assets").select("*").eq("trend_version_id", version["id"]).order("position").execute().data or []) if version else [])
    def owned_url(ident):
        if not ident:
            return None
        # Pending versions/covers are visible only through an authenticated route.
        return "/api/trends/assets/" + entity_id(ident)
    result["preview_url"] = owned_url(version.get("preview_asset_id"))
    result["cover_url"] = owned_url(result.get("video_cover_asset_id")) if trend["type"] == "video" else None
    if version:
        try:
            result["price"] = current_price(trend, version, cache=cache)
        except Exception:
            result["price"] = None
    return result


_META_KEYS = {"title", "description", "category", "tags", "language", "content_attributes", "creator_markup_tokens", "video_cover_asset_id"}


def clean_metadata(payload: dict, *, media_type: str | None = None) -> dict:
    result = {}
    limits = {"title": 120, "description": 2000, "category": 80, "language": 20}
    for key, value in payload.items():
        if key not in _META_KEYS:
            continue
        if key in limits:
            if not isinstance(value, str) or len(value) > limits[key] or (key == "title" and not value.strip()):
                raise TrendError("Проверьте название и описание тренда.")
            result[key] = value.strip()
        elif key == "tags":
            if not isinstance(value, list) or len(value) > 12 or any(not isinstance(x, str) or len(x) > 40 for x in value):
                raise TrendError("Можно добавить до 12 тегов длиной до 40 символов.")
            result[key] = list(dict.fromkeys(x.strip() for x in value if x.strip()))
        elif key == "creator_markup_tokens":
            if isinstance(value, bool) or not isinstance(value, int):
                raise TrendError("Наценка должна быть целым количеством токенов.")
            settings = config()
            if value < int(settings.get("min_markup_tokens", 0)) or value > int(settings.get("max_markup_tokens", 1000)) or (not value and not settings.get("allow_zero_markup", True)):
                raise TrendError("Наценка находится вне допустимых границ.")
            result[key] = value
        elif key == "video_cover_asset_id":
            if value and media_type == "photo":
                raise TrendError("Для фото обложкой служит только результат генерации.")
            result[key] = entity_id(value) if value else None
        elif key == "content_attributes":
            if not isinstance(value, dict) or len(json.dumps(value)) > 2000:
                raise TrendError("Некорректные атрибуты контента.")
            result[key] = value
    return result


def save_trend(user: dict, payload: dict, *, trend_id: str | None = None) -> dict:
    require_creation()
    owner = ensure_creator(user)
    old = get_trend(trend_id, owner_id=owner) if trend_id else None
    media_type = old["type"] if old else payload.get("type")
    if media_type not in {"photo", "video"}:
        raise TrendError("Выберите Фото или Видео.")
    if any(k in payload for k in ("preview_url", "preview_asset_id", "preview_generation_id", "current_version_id", "status", "creator_id")):
        raise TrendError("Результат генерации и статус нельзя установить вручную.")
    metadata = clean_metadata(payload, media_type=media_type)
    if not old and not metadata.get("title"):
        raise TrendError("Укажите название тренда.")
    recipe = payload.get("recipe")
    slots, fixed, recipe_hash = None, None, None
    if recipe is not None:
        if not isinstance(recipe, dict):
            raise TrendError("Некорректный рецепт.")
        from app.services.trend_registry import validate_recipe, list_models
        slots = recipe.get("slots", payload.get("slots", []))
        fixed = recipe.get("fixed_assets", payload.get("fixed_assets", []))
        if not isinstance(slots, list) or not isinstance(fixed, list):
            raise TrendError("Некорректные референсы.")
        slot_keys = {"id", "position", "input_type", "title", "instruction", "required", "min_files", "max_files", "validation_json", "provider_mapping"}
        if any(not isinstance(x, dict) for x in slots + fixed):
            raise TrendError("Некорректный слот или референс.")
        slots = [{k: v for k, v in item.items() if k in slot_keys} for item in slots]
        fixed = [{k: v for k, v in item.items() if k in {"asset_id", "position", "provider_mapping"}} for item in fixed]
        for i, slot in enumerate(slots):
            slot.setdefault("required", True)
            slot.setdefault("min_files", 1 if slot["required"] else 0)
            slot.setdefault("max_files", 1)
            slot.setdefault("instruction", "")
            slot.setdefault("provider_mapping", "references")
            rules = slot.setdefault("validation_json", {})
            if not isinstance(rules, dict) or set(rules) - {"max_bytes", "min_width", "min_height", "max_width", "max_height"} or any(isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in rules.values()):
                raise TrendError("Некорректные правила проверки файла.")
            slot["id"] = entity_id(slot.get("id") or str(uuid4()))
            slot.setdefault("position", i)
        try:
            recipe = validate_recipe({k: v for k, v in recipe.items() if k not in {"slots", "fixed_assets"}}, slots, fixed)
        except (ValueError, TypeError) as exc:
            raise TrendError(str(exc)) from exc
        model = next((m for m in list_models() if m["model_key"] == recipe["model_key"]), {})
        if (model.get("type") or model.get("media_type")) != media_type:
            raise TrendError("Тип модели не соответствует типу тренда.")
        from app.services.trend_assets import get_owned_asset
        for i, item in enumerate(fixed):
            item["asset_id"] = entity_id(item.get("asset_id"))
            item.setdefault("position", i)
            item.setdefault("provider_mapping", "references")
            if old is None:
                raise TrendError("Сначала создайте черновик и загрузите референсы.")
            asset = get_owned_asset(item["asset_id"], owner, trend_id=old["id"], kinds=["fixed"])
            if asset["result_type"] != "image" or asset["size_bytes"] > model.get("max_input_bytes", 10485760):
                raise TrendError("Постоянный референс превышает ограничения модели.")
        semantic_slots = [{k: v for k, v in slot.items() if k != "id"} for slot in slots]
        recipe_hash = hashlib.sha256(json.dumps({"recipe": recipe, "slots": semantic_slots, "fixed": fixed}, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    result = rpc("save", user_id=owner, trend_id=entity_id(trend_id) if trend_id else None,
                 media_type=media_type, metadata=metadata, recipe=recipe, slots=slots,
                 fixed=fixed, recipe_hash=recipe_hash, expected_revision=payload.get("revision"))
    return creator_detail(result)


def _quote_secret() -> bytes:
    value = os.getenv("TREND_QUOTE_SECRET") or os.getenv("WORKSPACE_AUTH_SECRET") or os.getenv("TELEGRAM_BOT_TOKEN")
    if not value:
        raise TrendError("Сервис расчёта цены не настроен.", 503)
    return value.encode()


def _encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def create_quote(trend_id: str, user_id: int, *, is_test: bool = False) -> dict:
    config(required=True)
    trend = get_trend(trend_id, owner_id=user_id if is_test else None)
    version = version_for(trend, creator=is_test)
    if not version:
        raise TrendError("Сначала сохраните рецепт.")
    price = current_price(trend, version, buyer_id=user_id, is_test=is_test)
    body = {k: price[k] for k in ("base_tokens", "markup_tokens", "total_tokens")}
    body.update(trend_id=trend["id"], trend_version_id=version["id"], buyer_user_id=user_id,
                is_test=is_test, expires_at=int(time.time()) + 180,
                financial_hash=hashlib.sha256(json.dumps(price["financial_config"], sort_keys=True).encode()).hexdigest())
    token = _encode(json.dumps(body, sort_keys=True, separators=(",", ":")).encode())
    quote_id = token + "." + _encode(hmac.new(_quote_secret(), token.encode(), hashlib.sha256).digest())
    result = {k: v for k, v in body.items() if k not in {"financial_hash", "buyer_user_id"}}
    result.update(quote_id=quote_id, current_version_id=version["id"])
    if int(trend["creator_id"]) == user_id:
        customer_price = current_price(trend, version)
        result["buyer_total_tokens"] = customer_price["total_tokens"]
        result["estimated_creator_reward_kopecks"] = customer_price["expected_creator_reward_kopecks"]
    return result



def decode_quote(value: str, *, trend_id: str, user_id: int, is_test: bool) -> dict:
    try:
        if len(value) > 8192:
            raise ValueError()
        token, signature = value.split(".")
        expected = _encode(hmac.new(_quote_secret(), token.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        body = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        if body["trend_id"] != trend_id or body["buyer_user_id"] != user_id or body["is_test"] != is_test:
            raise ValueError()
        return body
    except (ValueError, KeyError, TypeError):
        raise TrendError("Расчёт цены недействителен. Обновите цену.", 409)


def start_generation(trend_id: str, user_id: int, payload: dict, *, is_test: bool = False) -> dict:
    from app.services import trend_finance
    from app.services.trend_assets import get_owned_asset
    trend_id = entity_id(trend_id)
    idem = entity_id(payload.get("idempotency_key"))
    quote = decode_quote(str(payload.get("quote_id") or ""), trend_id=trend_id, user_id=user_id, is_test=is_test)
    inputs = payload.get("slot_assets")
    if not isinstance(inputs, dict) or len(inputs) > 16:
        raise TrendError("Проверьте загруженные материалы.")
    canonical_inputs = {entity_id(k): [entity_id(x) for x in v] for k, v in inputs.items() if isinstance(v, list)}
    if len(canonical_inputs) != len(inputs) or any(len(v) > 16 for v in canonical_inputs.values()):
        raise TrendError("Некорректные входные файлы.")
    request_hash = hashlib.sha256(json.dumps({"trend": trend_id, "version": quote["trend_version_id"], "inputs": canonical_inputs, "test": is_test}, sort_keys=True).encode()).hexdigest()
    # Return the already committed operation even when its quote expired or the
    # author subsequently hid/deleted the trend. Never charge an idempotent retry.
    existing = one("trend_runs", buyer_user_id=user_id, idempotency_key=idem)
    if existing:
        if existing.get("request_hash") != request_hash:
            raise TrendError("Этот ключ уже использован для другого запуска.", 409)
        return safe_run(existing)
    if quote["expires_at"] < time.time():
        raise TrendError("Цена могла измениться. Получите новый расчёт.", 409)
    if is_test:
        require_creation()
    else:
        config(required=True)
    trend = get_trend(trend_id, owner_id=user_id if is_test else None)
    version = version_for(trend, creator=is_test)
    if version.get("id") != quote["trend_version_id"]:
        raise TrendError("Рецепт изменился. Обновите страницу.", 409)
    price = current_price(trend, version, buyer_id=user_id, is_test=is_test)
    if any(price[k] != quote[k] for k in ("base_tokens", "markup_tokens", "total_tokens")) or hashlib.sha256(json.dumps(price["financial_config"], sort_keys=True).encode()).hexdigest() != quote["financial_hash"]:
        raise TrendError("Цена или условия изменились. Подтвердите новый расчёт.", 409)
    slots = slots_for(version["id"])
    if set(canonical_inputs) - {s["id"] for s in slots}:
        raise TrendError("В запросе есть неизвестные слоты.")
    for slot in slots:
        files = canonical_inputs.get(slot["id"], [])
        minimum = max(int(slot.get("min_files") or 0), 1 if slot.get("required") else 0)
        if not minimum <= len(files) <= int(slot.get("max_files") or 1):
            raise TrendError("Заполните обязательные слоты: " + str(slot.get("title") or "Референс"))
        for asset_id in files:
            asset = get_owned_asset(asset_id, user_id, trend_id=trend_id, kinds=["test_input"] if is_test else ["input"])
            if asset["result_type"] != slot["input_type"]:
                raise TrendError("Тип загруженного файла не подходит для слота.")
            from app.services.trend_registry import get_model
            maximum = get_model(version["recipe_json"]["model_key"])["max_input_bytes"]
            rules = slot.get("validation_json") or {}
            if asset["size_bytes"] > min(maximum, rules.get("max_bytes", maximum)):
                raise TrendError("Файл слишком большой для выбранного тренда.")
            details = asset.get("metadata") or {}
            for dimension in ("width", "height"):
                dimension_value = int(details.get(dimension) or 0)
                if ("min_" + dimension in rules and dimension_value < rules["min_" + dimension]) or ("max_" + dimension in rules and dimension_value > rules["max_" + dimension]):
                    raise TrendError("Размер изображения не соответствует требованиям слота.")
    run = trend_finance.start_run(buyer_user_id=user_id, trend_id=trend_id, trend_version_id=version["id"],
        base_tokens=price["base_tokens"], expected_markup_tokens=price["markup_tokens"],
        max_total_tokens=price["total_tokens"], idempotency_key=idem, request_hash=request_hash,
        input_assets=canonical_inputs, is_test=is_test, expected_financial_config=price["financial_config"])
    return safe_run(run)


def safe_run(run: dict) -> dict:
    # No provider task IDs, financial snapshot, input URLs, prompt or raw errors.
    result = {key: run.get(key) for key in ("id", "trend_id", "trend_version_id", "generation_id", "is_test", "status", "base_tokens", "markup_tokens", "total_tokens", "created_at", "completed_at")}
    result["result"] = {}
    if run.get("status") == "completed" and run.get("generation_id"):
        try:
            from app.services.trend_generation import get_history
            from app.routers import web_workspace_api as ww
            version = one("trend_versions", id=run["trend_version_id"])
            history = get_history(run, version) if version else {}
            if history.get("status") == "completed" and history.get("origin") == "trend_marketplace" and not history.get("deleted_at"):
                output_type = run_result_type(run, version)
                if output_type is None:
                    raise ValueError("Unknown generation result type")
                output = ww._serialize_workspace_image_generation(history) if output_type == "image" else ww._serialize_workspace_generation(history)
                result["result"] = {k: output[k] for k in ("image_url", "video_url", "download_url") if output.get(k)}
        except Exception:
            # Access URLs are generated again on the next read; expired stored
            # signed URLs and raw provider fallback fields are never exposed.
            result["message"] = "Результат готов. Если файл не открылся, обновите страницу."
    if run.get("status") in {"failed", "refunded", "cancelled"}:
        result["message"] = "Генерация не завершилась. Списанные токены возвращены."
    elif run.get("status") == "needs_reconciliation":
        result["message"] = ("Видео создано. Сохраняем результат; повторная оплата не требуется."
                             if run.get("error") == "video_archive_pending" else
                             "Проверяем статус генерации. Повторная оплата не требуется.")
    return result


def run_result_type(run: dict, version: dict) -> str | None:
    """The immutable version wins over legacy/inconsistent result metadata."""
    from app.services.trend_registry import get_model
    model_key = version.get("model_key") or (version.get("recipe_json") or {}).get("model_key")
    if model_key:
        media_type = get_model(model_key)["type"]
        return {"photo": "image", "video": "video"}.get(media_type)
    metadata = run.get("result_json") or {}
    # Read both historical DTO shapes; never default an unknown type to video.
    return next((metadata[k] for k in ("result_type", "type") if metadata.get(k) in {"image", "video"}), None)


def creator_stats(user_id: int, trend_id: str | None = None) -> dict:
    if trend_id:
        get_trend(trend_id, owner_id=user_id)
    return rpc("stats", user_id=user_id, trend_id=entity_id(trend_id) if trend_id else None) or {}


def change_lifecycle(trend_id: str, user_id: int, action: str, *, expected_revision: int | None = None) -> dict:
    if action not in {"submit", "hide", "restore", "delete"}:
        raise TrendError("Неизвестное действие.")
    return rpc("lifecycle", trend_id=entity_id(trend_id), user_id=user_id,
               action=action, expected_revision=expected_revision)


def validate_config(patch: dict) -> dict:
    patch = dict(patch)
    bools = {"marketplace_enabled", "creation_enabled", "payouts_enabled", "rewards_enabled", "allow_zero_markup"}
    bounds = {"settlement_kopecks_per_markup_token": (0, 100000), "max_backed_settlement_kopecks_per_token": (0, 100000), "marketplace_fee_bps": (0, 10000),
              "min_markup_tokens": (0, 100000), "max_markup_tokens": (0, 100000),
              "min_payout_kopecks": (1, 100000000), "hold_seconds": (3600, 7776000)}
    allowed = bools | set(bounds) | {"ranking_coefficients"}
    if set(patch) - allowed:
        raise TrendError("В настройках есть неизвестные поля.")
    for key, value in patch.items():
        if key in bools and not isinstance(value, bool):
            raise TrendError("Некорректное значение настройки.")
        if key in bounds and (isinstance(value, bool) or not isinstance(value, int) or not bounds[key][0] <= value <= bounds[key][1]):
            raise TrendError("Настройка находится вне допустимых границ.")
        if key == "ranking_coefficients":
            if not isinstance(value, dict) or set(value) - {"uses", "successful_runs", "likes", "freshness", "half_life_hours"} or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 10000 for v in value.values()):
                raise TrendError("Некорректные коэффициенты рейтинга.")
            value = dict(value)
            if "successful_runs" in value:
                if "uses" in value and value["uses"] != value["successful_runs"]:
                    raise TrendError("Укажите одинаковый вес в uses и successful_runs или оставьте только uses.")
                value["uses"] = value.pop("successful_runs")
            if value.get("half_life_hours", 72) < 1 or value.get("uses", 20) < 5 * value.get("likes", 1):
                raise TrendError("Вес успешной генерации должен значительно превышать вес лайка.")
            patch[key] = value
    merged = config() | patch
    if merged["min_markup_tokens"] > merged["max_markup_tokens"]:
        raise TrendError("Минимальная наценка превышает максимальную.")
    return patch


def event_fingerprint(user_id: int | None, session: str, ip: str) -> tuple[str, str]:
    secret = _quote_secret()
    identity = "user:" + str(user_id) if user_id else "session:" + session
    digest = hmac.new(secret, identity.encode(), hashlib.sha256).hexdigest()
    ip_hash = hmac.new(secret, (datetime.now(timezone.utc).strftime("%Y-%m-%d") + ":" + ip).encode(), hashlib.sha256).hexdigest()
    return digest, ip_hash


def traffic_source(payload: dict, referrer: str) -> tuple[str, dict]:
    meta = {k: str(payload.get(k) or "")[:160] for k in ("utm_source", "utm_medium", "utm_campaign", "ref")}
    text = (meta["utm_source"] + " " + referrer).lower()
    if meta["ref"]:
        return "creator_share", meta
    if "telegram" in text or "t.me" in text:
        return "telegram", meta
    if "vk.com" in text or meta["utm_source"].lower() == "vk":
        return "vk", meta
    if any(x in text for x in ("google.", "yandex.", "bing.")):
        return "search", meta
    return ("other" if referrer else "direct"), meta
