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


# ====== Replicate ======
REPLICATE_API_TOKEN = (os.getenv("REPLICATE_API_TOKEN") or "").strip()

# Kling 1.6 Image→Video
REPLICATE_KLING_I2V_STD_MODEL = (os.getenv("REPLICATE_KLING_I2V_STD_MODEL") or "kwaivgi/kling-v1.6-standard").strip()
REPLICATE_KLING_I2V_PRO_MODEL = (os.getenv("REPLICATE_KLING_I2V_PRO_MODEL") or "kwaivgi/kling-v1.6-pro").strip()

# Kling 2.5 Turbo Pro
REPLICATE_KLING_25_TURBO_PRO_MODEL = (os.getenv("REPLICATE_KLING_25_TURBO_PRO_MODEL") or "kwaivgi/kling-v2.5-turbo-pro").strip()

REPLICATE_HTTP_TIMEOUT_SECONDS = int(os.getenv("REPLICATE_HTTP_TIMEOUT", "60"))
REPLICATE_POLL_INTERVAL_SECONDS = float(os.getenv("REPLICATE_POLL_INTERVAL", "2.0"))
REPLICATE_MAX_WAIT_SECONDS = int(os.getenv("REPLICATE_MAX_WAIT", "900"))


class KlingFlowError(RuntimeError):
    pass


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

    while True:
        pred = await _rep_get_prediction(session, get_url)
        status = pred.get("status")

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
        file=bytes(data),
        file_options={
            "content-type": content_type,
            "upsert": "true",
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


def _make_end_image_path(user_id: int) -> str:
    ts = int(time.time())
    return f"kling_inputs/{user_id}/{ts}_end.jpg"


def _norm_aspect_ratio(value: str) -> str:
    v = (value or "16:9").strip()
    return v if v in ("16:9", "9:16", "1:1") else "16:9"


async def _run_replicate_model(*, model: str, input_payload: Dict[str, Any]) -> str:
    _rep_require_env()
    timeout = aiohttp.ClientTimeout(total=REPLICATE_HTTP_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        pred = await _rep_post_prediction(session, model, {"input": input_payload})
        urls = pred.get("urls") or {}
        get_url = urls.get("get")
        if not get_url:
            raise KlingFlowError(f"Missing urls.get in prediction response: {pred}")
        return await _rep_wait_for_result(session, get_url, REPLICATE_MAX_WAIT_SECONDS)


async def run_motion_control_from_bytes(
    *,
    user_id: int,
    avatar_bytes: bytes,
    motion_video_bytes: bytes,
    prompt: str,
    mode: str = "std",
    character_orientation: str = "video",
    keep_original_sound: bool = True,
    duration_seconds: Optional[int] = None,
    billing_meta: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Motion Control для legacy-ветки.
    """
    mode_norm = normalize_mode(mode)

    seconds: int
    if duration_seconds is not None:
        try:
            seconds = int(duration_seconds)
        except Exception:
            seconds = 0
    else:
        seconds = 0

    if seconds <= 0:
        seconds = get_duration_seconds(video_bytes=motion_video_bytes, suffix=".mp4")

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
                pass
        raise


async def run_image_to_video_from_bytes(
    *,
    user_id: int,
    start_image_bytes: bytes,
    prompt: str,
    duration_seconds: int,
    mode: str = "std",
    cfg_scale: float = 0.5,
    aspect_ratio: str = "16:9",
    negative_prompt: str = "",
    model_slug: Optional[str] = None,
    product: Optional[str] = None,
    end_image_bytes: Optional[bytes] = None,
    billing_meta: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Image → Video via Replicate.

    - legacy Kling 1.6 Standard/Pro
    - Kling 2.5 Turbo Pro
    """
    mode_norm = normalize_mode(mode)
    try:
        seconds = int(duration_seconds)
    except Exception:
        seconds = 0
    if seconds not in (5, 10):
        seconds = 5

    effective_model = (model_slug or "").strip()
    is_kling25 = bool(effective_model and effective_model == REPLICATE_KLING_25_TURBO_PRO_MODEL)
    if not effective_model:
        effective_model = REPLICATE_KLING_I2V_PRO_MODEL if mode_norm == "pro" else REPLICATE_KLING_I2V_STD_MODEL

    tokens_cost = calc_kling_tokens(seconds=seconds, mode=mode_norm, product=product)

    job_id: Optional[str] = None
    if BILLING_ENABLED:
        ensure_user_row(user_id)
        bal = get_balance(user_id)
        if bal < tokens_cost:
            raise KlingFlowError(f"Недостаточно токенов. Нужно: {tokens_cost}, баланс: {bal}. (Видео: {seconds} сек)")

        meta = {"model_slug": effective_model, **(billing_meta or {})}
        job_id = hold_tokens_for_kling(
            telegram_user_id=user_id,
            seconds=seconds,
            mode=mode_norm,
            tokens_cost=tokens_cost,
            meta=meta,
        )

    start_image_path = _make_i2v_path(user_id)
    start_image_url = upload_bytes_to_supabase(start_image_path, start_image_bytes, "image/jpeg")

    end_image_url: Optional[str] = None
    if end_image_bytes:
        end_image_path = _make_end_image_path(user_id)
        end_image_url = upload_bytes_to_supabase(end_image_path, end_image_bytes, "image/jpeg")

    if is_kling25:
        input_payload: Dict[str, Any] = {
            "prompt": (prompt or "").strip(),
            "duration": seconds,
            "start_image": start_image_url,
        }
        if negative_prompt:
            input_payload["negative_prompt"] = (negative_prompt or "").strip()
        if end_image_url:
            input_payload["end_image"] = end_image_url
    else:
        input_payload = {
            "prompt": (prompt or "").strip(),
            "duration": seconds,
            "cfg_scale": float(cfg_scale),
            "start_image": start_image_url,
            "aspect_ratio": _norm_aspect_ratio(aspect_ratio),
            "negative_prompt": (negative_prompt or "").strip(),
        }

    try:
        out_url = await _run_replicate_model(model=effective_model, input_payload=input_payload)
        if BILLING_ENABLED and job_id:
            confirm_kling_job(
                job_id,
                out_url=out_url,
                meta={
                    "seconds": seconds,
                    "mode": mode_norm,
                    "tokens_cost": tokens_cost,
                    "model_slug": effective_model,
                    **(billing_meta or {}),
                },
            )
        return out_url
    except Exception as e:
        if BILLING_ENABLED and job_id:
            try:
                rollback_kling_job(job_id, error=str(e))
            except Exception:
                pass
        raise


async def run_text_to_video_from_prompt(
    *,
    user_id: int,
    prompt: str,
    duration_seconds: int,
    aspect_ratio: str = "16:9",
    negative_prompt: str = "",
    model_slug: Optional[str] = None,
    product: Optional[str] = None,
    billing_meta: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Text → Video via Replicate (Kling 2.5 Turbo Pro).
    """
    try:
        seconds = int(duration_seconds)
    except Exception:
        seconds = 0
    if seconds not in (5, 10):
        seconds = 5

    effective_model = (model_slug or REPLICATE_KLING_25_TURBO_PRO_MODEL).strip() or REPLICATE_KLING_25_TURBO_PRO_MODEL
    mode_norm = "pro"
    tokens_cost = calc_kling_tokens(seconds=seconds, mode=mode_norm, product=product)

    job_id: Optional[str] = None
    if BILLING_ENABLED:
        ensure_user_row(user_id)
        bal = get_balance(user_id)
        if bal < tokens_cost:
            raise KlingFlowError(f"Недостаточно токенов. Нужно: {tokens_cost}, баланс: {bal}. (Видео: {seconds} сек)")

        meta = {"model_slug": effective_model, **(billing_meta or {})}
        job_id = hold_tokens_for_kling(
            telegram_user_id=user_id,
            seconds=seconds,
            mode=mode_norm,
            tokens_cost=tokens_cost,
            meta=meta,
        )

    input_payload: Dict[str, Any] = {
        "prompt": (prompt or "").strip(),
        "duration": seconds,
        "aspect_ratio": _norm_aspect_ratio(aspect_ratio),
    }
    if negative_prompt:
        input_payload["negative_prompt"] = (negative_prompt or "").strip()

    try:
        out_url = await _run_replicate_model(model=effective_model, input_payload=input_payload)
        if BILLING_ENABLED and job_id:
            confirm_kling_job(
                job_id,
                out_url=out_url,
                meta={
                    "seconds": seconds,
                    "mode": mode_norm,
                    "tokens_cost": tokens_cost,
                    "aspect_ratio": _norm_aspect_ratio(aspect_ratio),
                    "model_slug": effective_model,
                    **(billing_meta or {}),
                },
            )
        return out_url
    except Exception as e:
        if BILLING_ENABLED and job_id:
            try:
                rollback_kling_job(job_id, error=str(e))
            except Exception:
                pass
        raise
