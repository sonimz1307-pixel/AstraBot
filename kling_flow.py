# kling_flow.py
import os
import time

from supabase import create_client

from kling_motion import run_motion_control


SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").strip()
SUPABASE_SERVICE_KEY = (os.getenv("SUPABASE_SERVICE_KEY") or "").strip()
SUPABASE_BUCKET = (os.getenv("SUPABASE_BUCKET") or "").strip()  # важно: регистр (Kling vs kling)


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
) -> str:
    avatar_path, video_path = _make_paths(user_id)

    image_url = upload_bytes_to_supabase(avatar_path, avatar_bytes, "image/jpeg")
    video_url = upload_bytes_to_supabase(video_path, motion_video_bytes, "video/mp4")

    out_url = await run_motion_control(
        image_url=image_url,
        video_url=video_url,
        prompt=prompt,
        mode=mode,
        character_orientation=character_orientation,
        keep_original_sound=keep_original_sound,
    )
    return out_url
