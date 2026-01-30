# kling_motion.py
# Minimal standalone module for Replicate Kling v2.6 + Motion Control
# Works without touching main.py. Use from bot later.

from future import annotations

import os
import json
import time
import asyncio
from typing import Any, Dict, Optional, Tuple

import aiohttp


# ----------------------------
# Config / ENV
# ----------------------------

REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN", "").strip()

# For selftest
TEST_IMAGE_URL = os.getenv("TEST_IMAGE_URL", "").strip()
TEST_VIDEO_URL = os.getenv("TEST_VIDEO_URL", "").strip()

# Replicate endpoints
REPLICATE_API_BASE = "https://api.replicate.com/v1"
MODEL_I2V = "kwaivgi/kling-v2.6"
MODEL_MOTION = "kwaivgi/kling-v2.6-motion-control"


class KlingError(RuntimeError):
    pass


def _require_env(name: str, value: str) -> None:
    if not value:
        raise KlingError(f"Missing required env var: {name}")


def _auth_headers() -> Dict[str, str]:
    _require_env("REPLICATE_API_TOKEN", REPLICATE_API_TOKEN)
    return {
        "Authorization": f"Bearer {REPLICATE_API_TOKEN}",
        "Content-Type": "application/json",
    }


# ----------------------------
# Low-level HTTP helpers
# ----------------------------

async def _http_post_json(session: aiohttp.ClientSession, url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    async with session.post(url, headers=_auth_headers(), data=json.dumps(payload)) as resp:
        text = await resp.text()
        if resp.status >= 400:
            # Replicate typically returns JSON with title/detail/status
            try:
                j = json.loads(text)
            except Exception:
                j = {"status": resp.status, "detail": text}
            raise KlingError(f"POST {url} failed: {j}")
        try:
            return json.loads(text)
        except Exception:
            raise KlingError(f"POST {url} invalid JSON response: {text}")


async def _http_get_json(session: aiohttp.ClientSession, url: str) -> Dict[str, Any]:
    async with session.get(url, headers=_auth_headers()) as resp:
        text = await resp.text()
        if resp.status >= 400:
            try:
                j = json.loads(text)
            except Exception:
                j = {"status": resp.status, "detail": text}
            raise KlingError(f"GET {url} failed: {j}")
        try:
            return json.loads(text)
        except Exception:
            raise KlingError(f"GET {url} invalid JSON response: {text}")


# ----------------------------
# Public API (simple)
# ----------------------------

async def create_prediction(
    model: str,
    inputs: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Create a Replicate prediction for a given model.
    Returns the full prediction JSON (contains id, status, urls.get, urls.web, etc.).
    """
    url = f"{REPLICATE_API_BASE}/models/{model}/predictions"
    payload = {"input": inputs}
    async with aiohttp.ClientSession() as session:
        return await _http_post_json(session, url, payload)


async def get_prediction(prediction_id: str) -> Dict[str, Any]:
    """
    Fetch prediction JSON by id.
    """
    url = f"{REPLICATE_API_BASE}/predictions/{prediction_id}"
    async with aiohttp.ClientSession() as session:
        return await _http_get_json(session, url)


async def wait_prediction(
    prediction_id: str,
    *,
    poll_every: float = 3.0,
    timeout_seconds: int = 20 * 60,
    verbose: bool = True,
) -> Dict[str, Any]:
    """
    Poll prediction until succeeded/failed/canceled or timeout.
    Returns final prediction JSON.
    """
    start = time.time()
    last_status: Optional[str] = None

    while True:
        pred = await get_prediction(prediction_id)
        status = pred.get("status")

        if verbose and status != last_status:
            last_status = status
            print(f"[kling] status={status} id={prediction_id}")

        if status in ("succeeded", "failed", "canceled"):
            return pred

        if time.time() - start > timeout_seconds:
            raise KlingError(f"Timeout waiting prediction {prediction_id} after {timeout_seconds}s")

        await asyncio.sleep(poll_every)


# ----------------------------
# High-level wrappers
# ----------------------------

async def kling_i2v(
    *,
    image_url: str,
    prompt: str = "A person walking",
    mode: str = "std",  # std | pro
) -> Tuple[str, Dict[str, Any]]:
    """
    Kling v2.6 image-to-video.
    Returns (output_url, final_prediction_json)
    """
    _require_env("REPLICATE_API_TOKEN", REPLICATE_API_TOKEN)
    if not image_url:
        raise KlingError("image_url is required")

    pred = await create_prediction(
        MODEL_I2V,
        inputs={
            "image": image_url,
            "prompt": prompt or "",
            "mode": mode,
        },
    )
    pid = pred["id"]
    final = await wait_prediction(pid, verbose=True)
    if final.get("status") != "succeeded":
        raise KlingError(f"I2V failed: {final.get('error') or final}")

    output = final.get("output")
    if not output:
        raise KlingError(f"I2V succeeded but no output: {final}")

    # output is usually a direct mp4 URL string for these models
    return str(output), final


async def kling_motion_control(
    *,
    image_url: str,
    video_url: str,
    prompt: str = "A person performs the same motion as in the reference video.",
    mode: str = "std",  # std | pro
    character_orientation: str = "video",  # image | video
    keep_original_sound: bool = True,
) -> Tuple[str, Dict[str, Any]]:
    """
    Kling v2.6 motion-control: transfers motion from reference video to character image.
    Returns (output_url, final_prediction_json)
    """
    _require_env("REPLICATE_API_TOKEN", REPLICATE_API_TOKEN)
    if not image_url:
        raise KlingError("image_url is required")
    if not video_url:
        raise KlingError("video_url is required")

    pred = await create_prediction(
        MODEL_MOTION,
        inputs={
            "image": image_url,
            "video": video_url,
            "prompt": prompt or "",
            "mode": mode,
            "character_orientation": character_orientation,
            "keep_original_sound": bool(keep_original_sound),
        },
    )
    pid = pred["id"]
    final = await wait_prediction(pid, verbose=True)
    if final.get("status") != "succeeded":
        raise KlingError(f"Motion control failed: {final.get('error') or final}")

    output = final.get("output")
    if not output:
        raise KlingError(f"Motion control succeeded but no output: {final}")

    return str(output), final


# ----------------------------
# Self-test + CLI
# ----------------------------

async def _selftest() -> None:
    """
    Uses env TEST_IMAGE_URL and TEST_VIDEO_URL.
    Creates motion-control generation and prints the output URL.
    """
    _require_env("REPLICATE_API_TOKEN", REPLICATE_API_TOKEN)
    _require_env("TEST_IMAGE_URL", TEST_IMAGE_URL)
    _require_env("TEST_VIDEO_URL", TEST_VIDEO_URL)

    print("[kling] selftest start")
    out, _final = await kling_motion_control(
        image_url=TEST_IMAGE_URL,
        video_url=TEST_VIDEO_URL,
        mode="std",
        character_orientation="video",
        keep_original_sound=True,
        prompt="A person performs the same motion as in the reference video.",
    )
    print("OK output:", out)


async def _cli() -> None:
    import sys

    cmd = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()

    if cmd in ("selftest", "test"):
        await _selftest()
        return

    if cmd == "i2v":
        # Usage: python kling_motion.py i2v <image_url> [prompt]
        if len(sys.argv) < 3:
            raise SystemExit("Usage: python kling_motion.py i2v <image_url> [prompt]")
        image_url = sys.argv[2]
        prompt = sys.argv[3] if len(sys.argv) >= 4 else "A person walking"
        out, _ = await kling_i2v(image_url=image_url, prompt=prompt, mode="std")
        print("OK output:", out)
        return

    if cmd in ("motion", "motion-control", "mc"):
        # Usage: python kling_motion.py motion <image_url> <video_url> [prompt]
        if len(sys.argv) < 4:
            raise SystemExit("Usage: python kling_motion.py motion <image_url> <video_url> [prompt]")
        image_url = sys.argv[2]
        video_url = sys.argv[3]
        prompt = sys.argv[4] if len(sys.argv) >= 5 else "A person performs the same motion as in the reference video."
        out, _ = await kling_motion_control(
            image_url=image_url,
            video_url=video_url,
            prompt=prompt,
            mode="std",
            character_orientation="video",
            keep_original_sound=True,
        )
        print("OK output:", out)
        return

    raise SystemExit(
        "Commands:\n"
        "  python kling_motion.py selftest\n"
        "  python kling_motion.py i2v <image_url> [prompt]\n"
        "  python kling_motion.py motion <image_url> <video_url> [prompt]\n"
        "\nEnv needed:\n"
        "  REPLICATE_API_TOKEN\n"
        "  (for selftest) TEST_IMAGE_URL, TEST_VIDEO_URL\n"
    )


if name == "main":
    asyncio.run(_cli())
