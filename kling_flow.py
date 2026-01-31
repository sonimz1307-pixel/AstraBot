# kling_flow.py
import os
import time
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
