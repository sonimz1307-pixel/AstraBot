import httpx
import os
from typing import Dict, Any

PIAPI_KEY = os.getenv("PIAPI_API_KEY")

BASE_URL = "https://api.piapi.ai/api/v1/task"


class Kling3Error(Exception):
    pass


def _build_headers() -> Dict[str, str]:
    if not PIAPI_KEY:
        raise Kling3Error("PIAPI_API_KEY is not set")

    return {
        "x-api-key": PIAPI_KEY,
        "Content-Type": "application/json"
    }


def _validate_inputs(duration: int, resolution: str):
    if duration < 3 or duration > 15:
        raise Kling3Error("Duration must be between 3 and 15 seconds")

    if resolution not in ("720", "1080"):
        raise Kling3Error("Resolution must be '720' or '1080'")


async def create_kling3_task(
    prompt: str,
    duration: int,
    resolution: str,
    enable_audio: bool,
    aspect_ratio: str = "16:9",
    prefer_multi_shots: bool = False
) -> Dict[str, Any]:

    _validate_inputs(duration, resolution)

    mode = "std" if resolution == "720" else "pro"

    payload = {
        "model": "kling",
        "task_type": "video_generation",
        "input": {
            "prompt": prompt,
            "duration": duration,
            "aspect_ratio": aspect_ratio,
            "enable_audio": enable_audio,
            "prefer_multi_shots": prefer_multi_shots,
            "mode": mode,
            "version": "3.0",
        },
        "config": {
            "service_mode": "public"
        }
    }

    headers = _build_headers()

    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            BASE_URL,
            json=payload,
            headers=headers
        )

    if response.status_code != 200:
        raise Kling3Error(response.text)

    return response.json()


async def get_kling3_task(task_id: str) -> Dict[str, Any]:

    headers = _build_headers()

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.get(
            f"{BASE_URL}/{task_id}",
            headers=headers
        )

    if response.status_code != 200:
        raise Kling3Error(response.text)

    return response.json()
