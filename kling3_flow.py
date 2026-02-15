import httpx
import os

PIAPI_KEY = os.getenv("PIAPI_API_KEY")

async def create_kling3_task(
    prompt: str,
    duration: int,
    resolution: str,
    enable_audio: bool,
):
    mode = "std" if resolution == "720" else "pro"

    payload = {
        "model": "kling",
        "task_type": "video_generation",
        "input": {
            "prompt": prompt,
            "duration": duration,
            "aspect_ratio": "16:9",
            "enable_audio": enable_audio,
            "prefer_multi_shots": False,
            "mode": mode,
            "version": "3.0",
        },
        "config": {
            "service_mode": "public"
        }
    }

    headers = {
        "x-api-key": PIAPI_KEY,
        "Content-Type": "application/json"
    }

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            "https://api.piapi.ai/api/v1/task",
            json=payload,
            headers=headers
        )

    response.raise_for_status()
    return response.json()
