# kling_motion.py
import os
import time
import json
import mimetypes
import asyncio
from typing import Optional, Dict, Any

import aiohttp
from supabase import create_client


# =========================
# Config (берём из ENV)
# =========================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")  # как у тебя на Render
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET")    # ВАЖНО: регистр (Kling vs kling)

REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN")
REPLICATE_API_BASE = "https://api.replicate.com/v1"

Kling_MODEL = os.getenv("KLING_MODEL", "kwaivgi/kling-v2.6-motion-control")


def _require_env():
    missing = []
    for k, v in [
        ("SUPABASE_URL", SUPABASE_URL),
        ("SUPABASE_SERVICE_KEY", SUPABASE_KEY),
        ("SUPABASE_BUCKET", SUPABASE_BUCKET),
        ("REPLICATE_API_TOKEN", REPLICATE_API_TOKEN),
    ]:
        if not v:
            missing.append(k)
    if missing:
        raise RuntimeError(f"Missing ENV: {', '.join(missing)}")


# =========================
# Supabase Storage helpers
# =========================
_sb = None

def _sb_client():
    global _sb
    if _sb is None:
        _require_env()
        _sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _sb


def supabase_upload_file(local_path: str, dest_path: str, content_type: Optional[str] = None) -> str:
    """
    Загружает файл в Supabase Storage (bucket из ENV) и возвращает public URL.
    bucket должен быть Public.
    """
    _require_env()
    sb = _sb_client()

    if not content_type:
        content_type, _ = mimetypes.guess_type(local_path)
    if not content_type:
        content_type = "application/octet-stream"

    with open(local_path, "rb") as f:
        sb.storage.from_(SUPABASE_BUCKET).upload(
            path=dest_path,
            file=f,
            file_options={"content-type": content_type},
        )

    return sb.storage.from_(SUPABASE_BUCKET).get_public_url(dest_path)


# =========================
# Replicate HTTP helpers
# =========================
async def replicate_create_prediction(model: str, inp: Dict[str, Any]) -> Dict[str, Any]:
    """
    POST /v1/models/{owner}/{name}/predictions
    """
    _require_env()
    owner, name = model.split("/", 1)
    url = f"{REPLICATE_API_BASE}/models/{owner}/{name}/predictions"
    headers = {
        "Authorization": f"Bearer {REPLICATE_API_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"input": inp}

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, data=json.dumps(payload)) as r:
            text = await r.text()
            if r.status >= 400:
                raise RuntimeError(f"Replicate create failed {r.status}: {text}")
            return json.loads(text)


async def replicate_get_prediction(pred_id: str) -> Dict[str, Any]:
    _require_env()
    url = f"{REPLICATE_API_BASE}/predictions/{pred_id}"
    headers = {"Authorization": f"Bearer {REPLICATE_API_TOKEN}"}

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as r:
            text = await r.text()
            if r.status >= 400:
                raise RuntimeError(f"Replicate get failed {r.status}: {text}")
            return json.loads(text)


async def replicate_wait(pred_id: str, poll_seconds: float = 5.0, timeout_seconds: float = 600.0) -> Dict[str, Any]:
    """
    Ждём prediction до succeeded/failed/canceled.
    """
    start = time.time()
    while True:
        d = await replicate_get_prediction(pred_id)
        st = d.get("status")
        if st in ("succeeded", "failed", "canceled"):
            return d
        if time.time() - start > timeout_seconds:
            raise TimeoutError(f"Replicate prediction timeout after {timeout_seconds}s, id={pred_id}, status={st}")
        await asyncio.sleep(poll_seconds)


# =========================
# Kling Motion Control (core)
# =========================
async def kling_motion_control(
    image_url: str,
    video_url: str,
    prompt: str = "",
    mode: str = "std",
    character_orientation: str = "video",
    keep_original_sound: bool = True,
) -> str:
    """
    Запускает kwaivgi/kling-v2.6-motion-control и возвращает output mp4 URL.
    """
    inp = {
        "prompt": prompt or "",
        "image": image_url,
        "video": video_url,
        "mode": mode,
        "character_orientation": character_orientation,
        "keep_original_sound": keep_original_sound,
    }

    created = await replicate_create_prediction(Kling_MODEL, inp)
    pred_id = created.get("id")
    if not pred_id:
        raise RuntimeError(f"Replicate did not return prediction id: {created}")

    done = await replicate_wait(pred_id)
    if done.get("status") != "succeeded":
        raise RuntimeError(f"Kling failed: status={done.get('status')} error={done.get('error')} logs={done.get('logs')}")

    out = done.get("output")
    if isinstance(out, list) and out:
        return out[0]
    if isinstance(out, str):
        return out
    raise RuntimeError(f"Unexpected output: {out}")


# =========================
# Self-test (запускать вручную в Shell)
# =========================
async def _selftest():
    """
    Пример: python -c "import asyncio; import kling_motion as km; asyncio.run(km._selftest())"
    Надо передать свои URL через ENV:
      TEST_IMAGE_URL, TEST_VIDEO_URL
    """
    image_url = os.getenv("TEST_IMAGE_URL")
    video_url = os.getenv("TEST_VIDEO_URL")
    if not image_url or not video_url:
        raise RuntimeError("Set ENV TEST_IMAGE_URL and TEST_VIDEO_URL for selftest")

    out = await kling_motion_control(
        image_url=image_url,
        video_url=video_url,
        prompt="A person performs the same motion as in the reference video.",
        mode="std",
        character_orientation="video",
        keep_original_sound=True,
    )
    print("OK output:", out)
