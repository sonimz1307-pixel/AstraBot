"""Kling 4.0 input contract and retail quotes (KIE docs, 2026-10-22).

Pure Python: shared by the authenticated API, worker and offline tests.
Amounts are NABEX tokens, never KIE credits. Video durations must come from
server-probed uploads, not from a browser-provided duration.
"""
from __future__ import annotations

import re
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl
from decimal import Decimal, ROUND_CEILING
from typing import Any

MODEL_REFERENCE = "kling-4.0/reference-to-video"
MODEL_KEYFRAME = "kling-4.0/keyframe-to-video"
RATES = {"720p": {"plain": "3", "video": "2"}, "1080p": {"plain": "4.5", "video": "3"}}
PROVIDER_USD = {"720p": {"plain": "0.1425", "video": "0.085"}, "1080p": {"plain": "0.2125", "video": "0.1275"}}
MAX_BYTES = 10 * 1024 * 1024
ASPECTS = {"16:9", "9:16", "1:1", "21:9", "auto"}


def verify_telegram(raw: str, token: str, *, now: float | None = None) -> dict:
    pairs = parse_qsl(raw, keep_blank_values=True)
    if not token or not pairs or len({k for k, _ in pairs}) != len(pairs):
        raise ValueError("Откройте Kling 4.0 из Telegram-бота.")
    data = dict(pairs)
    signature = data.pop("hash", "")
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(expected, signature):
        raise ValueError("Не удалось подтвердить вход через Telegram.")
    age = (time.time() if now is None else now) - int(data.get("auth_date") or 0)
    if not -60 <= age <= 86400:
        raise ValueError("Вход устарел. Заново откройте Kling 4.0 из бота.")
    user = json.loads(data.get("user") or "{}")
    if not isinstance(user, dict) or isinstance(user.get("id"), bool) or int(user.get("id") or 0) <= 0:
        raise ValueError("Пользователь Telegram не подтверждён.")
    return user


def quote_price(resolution: str, duration: int, video_seconds: Any = 0) -> dict:
    if resolution not in RATES:
        raise ValueError("Выберите 720p или 1080p.")
    if isinstance(duration, bool) or not isinstance(duration, int) or not 3 <= duration <= 30:
        raise ValueError("Длительность должна быть целым числом от 3 до 30 секунд.")
    seconds = Decimal(str(video_seconds))
    if not seconds.is_finite() or not 0 <= seconds <= 30:
        raise ValueError("Суммарная длительность видеореференсов — не более 30 секунд.")
    tier = "video" if seconds else "plain"
    billable = max(seconds, Decimal(duration)) + duration if seconds else Decimal(duration)
    # Round only the final token amount; preserve the actual probed clip length.
    tokens = int((billable * Decimal(RATES[resolution][tier])).to_integral_value(rounding=ROUND_CEILING))
    return {"tokens": tokens, "rate": RATES[resolution][tier], "billable_seconds": float(billable),
            "input_video_seconds": float(seconds), "output_seconds": duration,
            "resolution": resolution, "pricing_version": "kling4-20261022-v1"}


def build_request(spec: dict, assets: dict[str, dict]) -> tuple[str, dict, dict]:
    """Accept asset IDs only. The caller has already restricted assets to owner."""
    mode = str(spec.get("mode") or "reference")
    if mode not in {"reference", "keyframe"}:
        raise ValueError("Неизвестный режим Kling 4.0.")
    prompt = str(spec.get("prompt") or "").strip()
    if not prompt or len(prompt) > 8000:
        raise ValueError("Введите описание: от 1 до 8000 символов.")
    duration = spec.get("duration", 5)
    resolution = str(spec.get("resolution") or "720p")
    quote_price(resolution, duration)  # Validate before duration-dependent limits.
    aspect = str(spec.get("aspect_ratio") or "16:9")
    if aspect not in ASPECTS:
        raise ValueError("Недопустимый формат видео.")
    out = {"prompt": prompt, "duration": duration, "resolution": resolution}
    if aspect != "auto":
        out["aspect_ratio"] = aspect
    video_durations = []
    tags: set[str] = set()
    used_ids: set[str] = set()

    def asset(asset_id: Any, kind: str) -> dict:
        key = str(asset_id or "")
        row = assets.get(key)
        if not row or row.get("kind") != kind:
            raise ValueError("Файл не найден, принадлежит другому аккаунту или имеет неверный тип.")
        if key in used_ids:
            raise ValueError("Один файл нельзя добавить в несколько слотов. Загрузите отдельную копию.")
        used_ids.add(key)
        if kind == "video":
            sec = Decimal(str(row.get("duration_seconds") or 0))
            if not sec.is_finite() or not 2 <= sec <= 30:
                raise ValueError("Каждый видеореференс должен длиться от 2 до 30 секунд.")
            video_durations.append(sec)
        return row

    def urls(values: Any, kind: str, limit: int) -> list[str]:
        if not isinstance(values, list) or len(values) > limit:
            raise ValueError(f"Слишком много файлов: максимум {limit}.")
        return [str(asset(v, kind)["url"]) for v in values]

    if mode == "keyframe":
        if spec.get("image_ids") or spec.get("video_ids") or spec.get("elements") or spec.get("voices"):
            raise ValueError("В режиме ключевых кадров используйте только первый, промежуточные и последний кадры.")
        if not spec.get("first_frame_id"):
            raise ValueError("Загрузите первый кадр.")
        out["first_frame_url"] = asset(spec["first_frame_id"], "image")["url"]
        tags.add("first_frame")
        middle = urls(spec.get("keyframe_ids", []), "image", 8)
        if middle:
            out["keyframe_urls"] = middle
            tags.update(f"keyframe_{i+1}" for i in range(len(middle)))
        if spec.get("last_frame_id"):
            out["last_frame_url"] = asset(spec["last_frame_id"], "image")["url"]
            tags.add("last_frame")
        if len(used_ids) > min(10, duration + 1):
            raise ValueError(f"Для {duration} сек допускается не более {min(10, duration + 1)} кадров всего.")
        model = MODEL_KEYFRAME
    else:
        if spec.get("first_frame_id") or spec.get("last_frame_id") or spec.get("keyframe_ids"):
            raise ValueError("Для кадров выберите режим «Ключевые кадры».")
        images = urls(spec.get("image_ids", []), "image", 10)
        videos = urls(spec.get("video_ids", []), "video", 5)
        if images:
            out["image_urls"] = images
        if videos:
            out["video_urls"] = videos
            out["keep_original_sound"] = bool(spec.get("keep_original_sound", False))
        tags.update(f"image_{i+1}" for i in range(len(images)))
        tags.update(f"video_{i+1}" for i in range(len(videos)))
        elements = spec.get("elements", [])
        voices = spec.get("voices", [])
        if not isinstance(elements, list) or len(elements) > 7 or not isinstance(voices, list) or len(voices) > 3:
            raise ValueError("Допускается до 7 объектов и до 3 голосов.")
        if len(images) + len(videos) + len(elements) + len(voices) > 15:
            raise ValueError("Всего можно использовать не более 15 референсов.")
        video_elements = 0
        for collection, kind in [(elements, "element"), (voices, "voice")]:
            for item in collection:
                if not isinstance(item, dict):
                    raise ValueError("Неверный формат референса.")
                name = str(item.get("name") or "").strip()
                if not re.fullmatch(r"[A-Za-z0-9_\u4e00-\u9fa5]{1,20}", name) or name.lower() in {t.lower() for t in tags} or re.fullmatch(r"(?:image|video|keyframe|object|audio)_\d+|first_frame|last_frame", name, re.I):
                    raise ValueError("Имя референса: 1–20 латинских букв, цифр или _, без повторений.")
                tags.add(name)
                ref = {"name": name}
                if kind == "voice":
                    ref["audio_url"] = asset(item.get("audio_id"), "audio")["url"]
                    out.setdefault("voices", []).append(ref)
                else:
                    if item.get("video_id"):
                        if item.get("image_ids"):
                            raise ValueError("Объект содержит либо видео, либо фотографии.")
                        row = asset(item["video_id"], "video")
                        if not 3 <= Decimal(str(row["duration_seconds"])) <= 8:
                            raise ValueError("Видео объекта: один непрерывный кадр длиной 3–8 секунд.")
                        if min(int(row.get("width") or 0), int(row.get("height") or 0)) < 700:
                            raise ValueError("Видео персонажа: короткая сторона не менее 700 пикселей.")
                        ref["video_url"] = row["url"]
                        ref["tag"] = "character"
                        video_elements += 1
                    else:
                        values = item.get("image_ids", [])
                        if not isinstance(values, list) or not 2 <= len(values) <= 4:
                            raise ValueError("Для объекта нужно от 2 до 4 фотографий.")
                        ref["image_urls"] = urls(values, "image", 4)
                    if item.get("description"):
                        if len(str(item["description"])) > 100:
                            raise ValueError("Описание объекта — не более 100 символов.")
                        ref["description"] = str(item["description"])
                    out.setdefault("elements", []).append(ref)
        if video_elements > 3 or len(video_durations) > 5:
            raise ValueError("Допускается до 3 видеообъектов и до 5 видео всего.")
        model = MODEL_REFERENCE
    for tag in re.findall(r"@([A-Za-z0-9_\u4e00-\u9fff]+)", prompt):
        if tag not in tags:
            raise ValueError(f"Референс @{tag} не добавлен. Проверьте имя и регистр.")
    quote = quote_price(resolution, duration, sum(video_durations, Decimal(0)))
    return model, out, quote


def public_job(row: dict) -> dict:
    return {k: row.get(k) for k in ("id", "status", "tokens", "result_url", "error_message", "created_at", "updated_at", "refunded", "mode", "resolution", "duration")}
