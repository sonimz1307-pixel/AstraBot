import os
import json
import asyncio
from typing import Optional, Dict, Any

import aiohttp


# ====== CONFIG (ENV) ======
REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN", "").strip()

# Модель по умолчанию — motion control
REPLICATE_MOTION_MODEL = os.getenv("REPLICATE_KLING_MOTION_MODEL", "kwaivgi/kling-v2.6-motion-control").strip()
# Модель для image->video (обычный Kling v2.6)
REPLICATE_I2V_MODEL = os.getenv("REPLICATE_KLING_I2V_MODEL", "kwaivgi/kling-v2.6").strip()

# Для selftest (не обязательно)
TEST_IMAGE_URL = os.getenv("TEST_IMAGE_URL", "").strip()
TEST_VIDEO_URL = os.getenv("TEST_VIDEO_URL", "").strip()

# Таймауты
HTTP_TIMEOUT_SECONDS = int(os.getenv("REPLICATE_HTTP_TIMEOUT", "60"))          # на один HTTP запрос
POLL_INTERVAL_SECONDS = float(os.getenv("REPLICATE_POLL_INTERVAL", "2.0"))     # частота опроса
MAX_WAIT_SECONDS = int(os.getenv("REPLICATE_MAX_WAIT", "900"))                # максимум ждать (15 мин)


# ====== INTERNAL HELPERS ======
class ReplicateError(RuntimeError):
    pass


def _require_env() -> None:
    if not REPLICATE_API_TOKEN:
        raise ReplicateError("REPLICATE_API_TOKEN is missing (set it in Render Environment).")


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {REPLICATE_API_TOKEN}",
        "Content-Type": "application/json",
    }


async def _post_prediction(session: aiohttp.ClientSession, model: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    POST /v1/models/{model}/predictions
    """
    url = f"https://api.replicate.com/v1/models/{model}/predictions"
    async with session.post(url, headers=_headers(), json=payload) as r:
        text = await r.text()
        if r.status >= 400:
            raise ReplicateError(f"Replicate POST failed ({r.status}): {text}")
        return json.loads(text)


async def _get_prediction(session: aiohttp.ClientSession, get_url: str) -> Dict[str, Any]:
    """
    GET prediction by URL from response.urls.get
    """
    async with session.get(get_url, headers=_headers()) as r:
        text = await r.text()
        if r.status >= 400:
            raise ReplicateError(f"Replicate GET failed ({r.status}): {text}")
        return json.loads(text)


def _extract_output_url(pred: Dict[str, Any]) -> Optional[str]:
    """
    Output у Replicate бывает:
    - строка (часто для видео)
    - массив строк
    - объект
    """
    out = pred.get("output")
    if out is None:
        return None
    if isinstance(out, str):
        return out
    if isinstance(out, list) and out and isinstance(out[0], str):
        return out[0]
    return None


async def _wait_for_result(session: aiohttp.ClientSession, get_url: str, max_wait_seconds: int) -> str:
    start = asyncio.get_event_loop().time()
    last_status = None

    while True:
        pred = await _get_prediction(session, get_url)
        status = pred.get("status")

        if status != last_status:
            last_status = status

        if status == "succeeded":
            out_url = _extract_output_url(pred)
            if not out_url:
                raise ReplicateError(f"Prediction succeeded but output missing/unexpected: {pred.get('output')}")
            return out_url

        if status in ("failed", "canceled"):
            raise ReplicateError(f"Prediction {status}: {pred.get('error') or pred}")

        elapsed = asyncio.get_event_loop().time() - start
        if elapsed > max_wait_seconds:
            raise ReplicateError(f"Timeout: waited {int(elapsed)}s > {max_wait_seconds}s. Last status={status}")

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


# ====== PUBLIC API (LOW-LEVEL) ======
async def run_motion_control(
    *,
    image_url: str,
    video_url: str,
    prompt: str = "",
    mode: str = "std",                 # "std" | "pro"
    character_orientation: str = "video",  # "image" | "video"
    keep_original_sound: bool = True,
    model: str = REPLICATE_MOTION_MODEL,
    max_wait_seconds: int = MAX_WAIT_SECONDS,
) -> str:
    """
    Запускает Kling Motion Control через Replicate и возвращает URL готового mp4.
    Требует ENV: REPLICATE_API_TOKEN
    """
    _require_env()

    image_url = (image_url or "").strip()
    video_url = (video_url or "").strip()
    prompt = (prompt or "").strip()

    if not image_url:
        raise ReplicateError("image_url is empty")
    if not video_url:
        raise ReplicateError("video_url is empty")

    payload = {
        "input": {
            "prompt": prompt,
            "image": image_url,
            "video": video_url,
            "mode": mode,
            "character_orientation": character_orientation,
            "keep_original_sound": bool(keep_original_sound),
        }
    }

    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        pred = await _post_prediction(session, model, payload)
        urls = pred.get("urls") or {}
        get_url = urls.get("get")
        if not get_url:
            raise ReplicateError(f"Missing urls.get in prediction response: {pred}")
        return await _wait_for_result(session, get_url, max_wait_seconds)


async def run_image_to_video(
    *,
    image_url: str,
    prompt: str = "",
    mode: str = "std",                 # "std" | "pro"
    model: str = REPLICATE_I2V_MODEL,
    max_wait_seconds: int = MAX_WAIT_SECONDS,
) -> str:
    """
    Запускает Kling Image->Video (kwaivgi/kling-v2.6) через Replicate и возвращает URL mp4.
    Требует ENV: REPLICATE_API_TOKEN
    """
    _require_env()

    image_url = (image_url or "").strip()
    prompt = (prompt or "").strip()

    if not image_url:
        raise ReplicateError("image_url is empty")

    payload = {
        "input": {
            "image": image_url,
            "prompt": prompt,
            "mode": mode,
        }
    }

    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        pred = await _post_prediction(session, model, payload)
        urls = pred.get("urls") or {}
        get_url = urls.get("get")
        if not get_url:
            raise ReplicateError(f"Missing urls.get in prediction response: {pred}")
        return await _wait_for_result(session, get_url, max_wait_seconds)


# ====== COMPAT LAYER (WHAT kling_flow EXPECTS) ======
# Эти имена нужны, потому что kling_flow.py импортирует их напрямую.

async def run_kling_motion_control(
    *,
    image_url: str,
    video_url: str,
    prompt: str = "",
    mode: str = "std",
    character_orientation: str = "video",
    keep_original_sound: bool = True,
) -> str:
    return await run_motion_control(
        image_url=image_url,
        video_url=video_url,
        prompt=prompt,
        mode=mode,
        character_orientation=character_orientation,
        keep_original_sound=keep_original_sound,
    )


async def run_kling_image_to_video(
    *,
    image_url: str,
    prompt: str = "",
    mode: str = "std",
) -> str:
    return await run_image_to_video(
        image_url=image_url,
        prompt=prompt,
        mode=mode,
    )


# ====== SELFTEST (OPTIONAL) ======
async def selftest() -> str:
    """
    Быстрый тест Motion Control: берет TEST_IMAGE_URL и TEST_VIDEO_URL из ENV.
    """
    if not TEST_IMAGE_URL or not TEST_VIDEO_URL:
        raise ReplicateError("Set TEST_IMAGE_URL and TEST_VIDEO_URL env vars to run selftest.")

    return await run_kling_motion_control(
        image_url=TEST_IMAGE_URL,
        video_url=TEST_VIDEO_URL,
        prompt="A person performs the same motion as in the reference video.",
        mode="std",
        character_orientation="video",
        keep_original_sound=True,
    )


def main_cli() -> None:
    """
    Запуск:
      python kling_motion.py selftest
    """
    import sys
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()

    if cmd == "selftest":
        out = asyncio.run(selftest())
        print("OK output:", out)
    else:
        print("Usage: python kling_motion.py selftest")
        print("ENV required: REPLICATE_API_TOKEN (+ TEST_IMAGE_URL/TEST_VIDEO_URL for selftest)")


if __name__ == "__main__":
    main_cli()
