# kling_flow.py
import os
import time
from io import BytesIO
from typing import Tuple

from supabase import create_client

from kling_motion import run_motion_control  # твой боевой модуль


SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET", "").strip()  # важно: как реально называется бакет (регистр!)

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    supabase = None
else:
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


class KlingFlowError(RuntimeError):
    pass


def _require_supabase():
    if supabase is None:
        raise KlingFlowError("Supabase client not ready. Check SUPABASE_URL / SUPABASE_SERVICE_KEY")
    if not SUPABASE_BUCKET:
        raise KlingFlowError("SUPABASE_BUCKET is missing")


def upload_bytes_to_supabase(path: str, data: bytes, content_type: str) -> str:
    """
    Upload bytes to Supabase Storage bucket and return public URL.
    Bucket must be Public OR policy must allow public read (you already used public links).
    """
    _require_supabase()
    if not data:
        raise KlingFlowError("Empty bytes to upload")

    bio = BytesIO(data)
    # ВАЖНО: для storage3/supabase python — file_options ключи строками
    supabase.storage.from_(SUPABASE_BUCKET).upload(
        path=path,
        file=bio,
        file_options={
            "content-type": content_type,
            "upsert": "true",
        },
    )
    return supabase.storage.from_(SUPABASE_BUCKET).get_public_url(path)


def make_paths(user_id: int) -> Tuple[str, str]:
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
    """
    1) Upload avatar + motion video to Supabase
    2) Call Replicate Kling Motion Control
    3) Return output mp4 URL from Replicate
    """
    avatar_path, video_path = make_paths(user_id)

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
