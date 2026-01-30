# kling_flow.py
import os
import time
import uuid
from datetime import datetime

from supabase import create_client

from kling_motion import run_kling_image_to_video, run_kling_motion_control


SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET", "Kling")  # ВАЖНО: имя бакета case-sensitive (у тебя "Kling")


def _require_env():
    if not SUPABASE_URL:
        raise RuntimeError("SUPABASE_URL is missing")
    if not SUPABASE_KEY:
        raise RuntimeError("SUPABASE_SERVICE_KEY is missing")


def _make_object_path(prefix: str, ext: str) -> str:
    # Пример: kling/mc/2026-01-30/uuid.jpg
    day = datetime.utcnow().strftime("%Y-%m-%d")
    return f"kling/{prefix}/{day}/{uuid.uuid4().hex}.{ext}"


def upload_bytes_to_supabase(data: bytes, ext: str, prefix: str) -> str:
    """
    Загружает bytes в Supabase Storage и возвращает PUBLIC URL.
    ВАЖНО: передаём именно bytes (не BytesIO), чтобы не было ошибки PathLike/BytesIO.
    """
    _require_env()
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError(f"upload_bytes_to_supabase ожидает bytes, пришло: {type(data)}")

    sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    object_path = _make_object_path(prefix=prefix, ext=ext)

    # content-type по расширению (минимально достаточно)
    content_type = "application/octet-stream"
    if ext.lower() in ("jpg", "jpeg"):
        content_type = "image/jpeg"
    elif ext.lower() == "png":
        content_type = "image/png"
    elif ext.lower() == "webp":
        content_type = "image/webp"
    elif ext.lower() == "mp4":
        content_type = "video/mp4"

    # КЛЮЧЕВОЕ: file=data (bytes), а не BytesIO(data)
    sb.storage.from_(SUPABASE_BUCKET).upload(
        path=object_path,
        file=data,
        file_options={
            "content-type": content_type,
            "upsert": True,
        },
    )

    return sb.storage.from_(SUPABASE_BUCKET).get_public_url(object_path)


async def run_motion_control_from_bytes(
    image_bytes: bytes,
    video_bytes: bytes,
    prompt: str,
    mode: str = "std",
    character_orientation: str = "video",
    keep_original_sound: bool = True,
) -> str:
    """
    1) грузим image/video в Supabase
    2) вызываем Kling Motion Control (Replicate)
    3) возвращаем ссылку на mp4
    """
    image_url = upload_bytes_to_supabase(image_bytes, ext="jpg", prefix="mc")
    video_url = upload_bytes_to_supabase(video_bytes, ext="mp4", prefix="mc")

    out_url = await run_kling_motion_control(
        prompt=prompt,
        image_url=image_url,
        video_url=video_url,
        mode=mode,
        character_orientation=character_orientation,
        keep_original_sound=keep_original_sound,
    )
    return out_url


async def run_image_to_video_from_bytes(
    image_bytes: bytes,
    prompt: str,
    mode: str = "std",
) -> str:
    """
    1) грузим image в Supabase
    2) вызываем Kling Image→Video
    3) возвращаем ссылку на mp4
    """
    image_url = upload_bytes_to_supabase(image_bytes, ext="jpg", prefix="i2v")

    out_url = await run_kling_image_to_video(
        prompt=prompt,
        image_url=image_url,
        mode=mode,
    )
    return out_url
