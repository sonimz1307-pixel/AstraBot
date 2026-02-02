# kling_flow.py
import os
import time
import json
import asyncio

import aiohttp
from typing import Optional, Dict, Any

from supabase import create_client

from kling_motion import run_motion_control

# NEW: billing
from billing_db import (
    ensure_user_row,
    get_balance,
    hold_tokens_for_kling,
    confirm_kling_job,
    rollback_kling_job,
)
from billing_rules import calc_kling_tokens, normalize_mode
from video_duration import get_duration_seconds


SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").strip()
SUPABASE_SERVICE_KEY = (os.getenv("SUPABASE_SERVICE_KEY") or "").strip()
SUPABASE_BUCKET = (os.getenv("SUPABASE_BUCKET") or "").strip()  # важно: регистр (Kling vs kling)

# Можно временно выключать биллинг через ENV (для тестов)
BILLING_ENABLED = (os.getenv("BILLING_ENABLED", "true") or "true").strip().lower() in ("1", "true", "yes", "on")



# ====== Replicate (for Kling Image→Video v1.6) ======
REPLICATE_API_TOKEN = (os.getenv("REPLICATE_API_TOKEN") or "").strip()

# Default models for Kling 1.6 Image→Video (separate slugs)
REPLICATE_KLING_I2V_STD_MODEL = (os.getenv("REPLICATE_KLING_I2V_STD_MODEL") or "kwaivgi/kling-v1.6-standard").strip()
REPLICATE_KLING_I2V_PRO_MODEL = (os.getenv("REPLICATE_KLING_I2V_PRO_MODEL") or "kwaivgi/kling-v1.6-pro").strip()

REPLICATE_HTTP_TIMEOUT_SECONDS = int(os.getenv("REPLICATE_HTTP_TIMEOUT", "60"))
REPLICATE_POLL_INTERVAL_SECONDS = float(os.getenv("REPLICATE_POLL_INTERVAL", "2.0"))
REPLICATE_MAX_WAIT_SECONDS = int(os.getenv("REPLICATE_MAX_WAIT", "900"))


def _rep_require_env() -> None:
    if not REPLICATE_API_TOKEN:
        raise KlingFlowError("REPLICATE_API_TOKEN is missing (set it in Render Environment).")


def _rep_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {REPLICATE_API_TOKEN}",
        "Content-Type": "application/json",
    }


async def _rep_post_prediction(session: aiohttp.ClientSession, model: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = f"https://api.replicate.com/v1/models/{model}/predictions"
    async with session.post(url, headers=_rep_headers(), json=payload) as r:
        text = await r.text()
        if r.status >= 400:
            raise KlingFlowError(f"Replicate POST failed ({r.status}): {text}")
        return json.loads(text)


async def _rep_get_prediction(session: aiohttp.ClientSession, get_url: str) -> Dict[str, Any]:
    async with session.get(get_url, headers=_rep_headers()) as r:
        text = await r.text()
        if r.status >= 400:
            raise KlingFlowError(f"Replicate GET failed ({r.status}): {text}")
        return json.loads(text)


def _rep_extract_output_url(pred: Dict[str, Any]) -> Optional[str]:
    out = pred.get("output")
    if out is None:
        return None
    if isinstance(out, str):
        return out
    if isinstance(out, list) and out and isinstance(out[0], str):
        return out[0]
    return None


async def _rep_wait_for_result(session: aiohttp.ClientSession, get_url: str, max_wait_seconds: int) -> str:
    start = asyncio.get_event_loop().time()
    last_status = None

    while True:
        pred = await _rep_get_prediction(session, get_url)
        status = pred.get("status")

        if status != last_status:
            last_status = status

        if status == "succeeded":
            out_url = _rep_extract_output_url(pred)
            if not out_url:
                raise KlingFlowError(f"Prediction succeeded but output missing/unexpected: {pred.get('output')}")
            return out_url

        if status in ("failed", "canceled"):
            raise KlingFlowError(f"Prediction {status}: {pred.get('error') or pred}")

        elapsed = asyncio.get_event_loop().time() - start
        if elapsed > max_wait_seconds:
            raise KlingFlowError(f"Timeout: waited {int(elapsed)}s > {max_wait_seconds}s. Last status={status}")

        await asyncio.sleep(REPLICATE_POLL_INTERVAL_SECONDS)


class KlingFlowError(RuntimeError):
    pass


def _sb():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise KlingFlowError("Supabase ENV missing: SUPABASE_URL / SUPABASE_SERVICE_KEY")
    if not SUPABASE_BUCKET:
        raise KlingFlowError("Supabase ENV missing: SUPABASE_BUCKET")
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def upload_bytes_to_supabase(path: str, data: bytes, content_type: str) -> str:
    """
    Загружает bytes в Supabase Storage и возвращает public URL.
    ВАЖНО: НЕ BytesIO — storage3 в твоей среде падает на BytesIO.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise KlingFlowError(f"upload_bytes_to_supabase: data must be bytes, got {type(data)}")
    if not data:
        raise KlingFlowError("upload_bytes_to_supabase: empty bytes")

    sb = _sb()

    sb.storage.from_(SUPABASE_BUCKET).upload(
        path=path,
        file=bytes(data),  # <-- ключевая правка: передаём bytes
        file_options={
            "content-type": content_type,
            "upsert": "true",  # строкой! (иначе httpx может упасть на encode(bool))
        },
    )

    return sb.storage.from_(SUPABASE_BUCKET).get_public_url(path)


def _make_paths(user_id: int) -> tuple[str, str]:
    ts = int(time.time())
    base = f"kling_inputs/{user_id}/{ts}"
    return f"{base}_avatar.jpg", f"{base}_motion.mp4"


def _make_i2v_path(user_id: int) -> str:
    ts = int(time.time())
    return f"kling_inputs/{user_id}/{ts}_start.jpg"


async def run_motion_control_from_bytes(
    *,
    user_id: int,
    avatar_bytes: bytes,
    motion_video_bytes: bytes,
    prompt: str,
    mode: str = "std",
    character_orientation: str = "video",
    keep_original_sound: bool = True,
    duration_seconds: Optional[int] = None,   # NEW: чтобы не зависеть от ffprobe
    billing_meta: Optional[Dict[str, Any]] = None,
) -> str:
    """
    1) Загружает avatar+motion video в Supabase Storage
    2) (NEW) Если BILLING_ENABLED=true:
        - считает seconds
        - считает tokens_cost
        - проверяет баланс
        - делает hold (списывает)
        - при успехе confirm, при ошибке rollback
    3) Запускает Replicate Motion Control и возвращает URL mp4
    """
    mode_norm = normalize_mode(mode)

    # 0) seconds (для списания)
    seconds: int
    if duration_seconds is not None:
        try:
            seconds = int(duration_seconds)
        except Exception:
            seconds = 0
    else:
        seconds = 0

    if seconds <= 0:
        # fallback: попробуем определить по bytes (через ffprobe, если доступен)
        # (если ffprobe не установлен, будет понятная ошибка)
        seconds = get_duration_seconds(video_bytes=motion_video_bytes, suffix=".mp4")

    tokens_cost = calc_kling_tokens(seconds=seconds, mode=mode_norm)

    job_id: Optional[str] = None
    if BILLING_ENABLED:
        ensure_user_row(user_id)
        bal = get_balance(user_id)
        if bal < tokens_cost:
            raise KlingFlowError(f"Недостаточно токенов. Нужно: {tokens_cost}, баланс: {bal}. (Видео: {seconds} сек, режим: {mode_norm})")

        # HOLD: списываем токены и создаём job
        job_id = hold_tokens_for_kling(
            telegram_user_id=user_id,
            seconds=seconds,
            mode=mode_norm,
            tokens_cost=tokens_cost,
            meta=billing_meta or {},
        )

    avatar_path, video_path = _make_paths(user_id)

    image_url = upload_bytes_to_supabase(avatar_path, avatar_bytes, "image/jpeg")
    video_url = upload_bytes_to_supabase(video_path, motion_video_bytes, "video/mp4")

    try:
        out_url = await run_motion_control(
            image_url=image_url,
            video_url=video_url,
            prompt=prompt,
            mode=mode_norm,
            character_orientation=character_orientation,
            keep_original_sound=keep_original_sound,
        )

        if BILLING_ENABLED and job_id:
            confirm_kling_job(job_id, out_url=out_url, meta={"seconds": seconds, "mode": mode_norm, "tokens_cost": tokens_cost})

        return out_url

    except Exception as e:
        if BILLING_ENABLED and job_id:
            try:
                rollback_kling_job(job_id, error=str(e))
            except Exception:
                # не маскируем основную ошибку
                pass
        raise


async def run_image_to_video_from_bytes(
    *,
    user_id: int,
    start_image_bytes: bytes,
    prompt: str,
    duration_seconds: int,
    mode: str = "std",  # "std" | "pro"
    cfg_scale: float = 0.5,
    aspect_ratio: str = "16:9",
    negative_prompt: str = "",
    billing_meta: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Image → Video (Kling v1.6 Standard/Pro) via Replicate.

    1) Upload start image to Supabase Storage → public URL
    2) If BILLING_ENABLED=true: hold tokens for duration_seconds
    3) Call Replicate model:
        - std: REPLICATE_KLING_I2V_STD_MODEL
        - pro: REPLICATE_KLING_I2V_PRO_MODEL
    4) confirm / rollback billing
    """
    _rep_require_env()
    mode_norm = normalize_mode(mode)

    try:
        seconds = int(duration_seconds)
    except Exception:
        seconds = 0
    if seconds not in (5, 10):
        # MVP: allow only 5/10
        seconds = 5

    tokens_cost = calc_kling_tokens(seconds=seconds, mode=mode_norm)

    job_id: Optional[str] = None
    if BILLING_ENABLED:
        ensure_user_row(user_id)
        bal = get_balance(user_id)
        if bal < tokens_cost:
            raise KlingFlowError(f"Недостаточно токенов. Нужно: {tokens_cost}, баланс: {bal}. (Видео: {seconds} сек, режим: {mode_norm})")

        job_id = hold_tokens_for_kling(
            telegram_user_id=user_id,
            seconds=seconds,
            mode=mode_norm,
            tokens_cost=tokens_cost,
            meta=billing_meta or {},
        )

    image_path = _make_i2v_path(user_id)
    image_url = upload_bytes_to_supabase(image_path, start_image_bytes, "image/jpeg")

    model = REPLICATE_KLING_I2V_PRO_MODEL if mode_norm == "pro" else REPLICATE_KLING_I2V_STD_MODEL

    payload = {
        "input": {
            "prompt": (prompt or "").strip(),
            "duration": seconds,
            "cfg_scale": float(cfg_scale),
            "start_image": image_url,
            "aspect_ratio": (aspect_ratio or "16:9").strip(),
            "negative_prompt": (negative_prompt or "").strip(),
        }
    }

    timeout = aiohttp.ClientTimeout(total=REPLICATE_HTTP_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            pred = await _rep_post_prediction(session, model, payload)
            urls = pred.get("urls") or {}
            get_url = urls.get("get")
            if not get_url:
                raise KlingFlowError(f"Missing urls.get in prediction response: {pred}")

            out_url = await _rep_wait_for_result(session, get_url, REPLICATE_MAX_WAIT_SECONDS)

            if BILLING_ENABLED and job_id:
                confirm_kling_job(job_id, out_url=out_url, meta={"seconds": seconds, "mode": mode_norm, "tokens_cost": tokens_cost})
            return out_url

        except Exception as e:
            if BILLING_ENABLED and job_id:
                try:
                    rollback_kling_job(job_id, error=str(e))
                except Exception:
                    pass
            raise

