import asyncio
from typing import Any, Dict, Optional, Tuple

from kling3_flow import create_kling3_task, get_kling3_task, Kling3Error


class Kling3RunnerError(Exception):
    pass


def _extract_task_id(resp: Dict[str, Any]) -> Optional[str]:
    # PiAPI typically returns: { code:200, data:{ task_id: "...", status:"pending", ... } }
    if not isinstance(resp, dict):
        return None
    data = resp.get("data")
    if isinstance(data, dict) and data.get("task_id"):
        return str(data.get("task_id"))
    if resp.get("task_id"):
        return str(resp.get("task_id"))
    return None


def _extract_video_url(task_resp: Dict[str, Any]) -> Optional[str]:
    if not isinstance(task_resp, dict):
        return None
    data = task_resp.get("data") if isinstance(task_resp.get("data"), dict) else task_resp
    output = data.get("output") if isinstance(data.get("output"), dict) else {}
    video = output.get("video")
    if isinstance(video, str) and video.strip():
        return video.strip()
    return None


def _extract_status(task_resp: Dict[str, Any]) -> str:
    if not isinstance(task_resp, dict):
        return ""
    data = task_resp.get("data") if isinstance(task_resp.get("data"), dict) else task_resp
    status = data.get("status") or task_resp.get("status") or ""
    return str(status).lower().strip()


async def run_kling3_task_and_wait(
    *,
    prompt: str,
    duration: int,
    resolution: str,
    enable_audio: bool,
    aspect_ratio: str = "16:9",
    poll_interval_sec: float = 2.0,
    timeout_sec: int = 300,
) -> Tuple[str, Dict[str, Any], Optional[str]]:
    """Create Kling 3.0 task and wait until it finishes.

    Returns: (task_id, final_task_json, video_url_or_None)
    Raises: Kling3RunnerError / Kling3Error
    """

    create_resp = await create_kling3_task(
        prompt=prompt,
        duration=duration,
        resolution=resolution,
        enable_audio=enable_audio,
        aspect_ratio=aspect_ratio,
    )

    task_id = _extract_task_id(create_resp)
    if not task_id:
        raise Kling3RunnerError(f"PiAPI: task_id not found in response: {create_resp}")

    deadline = asyncio.get_event_loop().time() + float(timeout_sec)

    last = None
    while True:
        if asyncio.get_event_loop().time() > deadline:
            raise Kling3RunnerError(f"Timeout waiting Kling3 task {task_id}")

        last = await get_kling3_task(task_id)
        status = _extract_status(last)

        if status in ("succeed", "succeeded", "success", "completed", "done", "finished"):
            video_url = _extract_video_url(last)
            return task_id, last, video_url

        if status in ("failed", "error", "canceled", "cancelled"):
            # Try to pass provider error message if present
            data = last.get("data") if isinstance(last.get("data"), dict) else last
            err = data.get("error") or {}
            msg = ""
            if isinstance(err, dict):
                msg = err.get("message") or err.get("raw_message") or ""
            raise Kling3RunnerError(f"Kling3 failed: {msg or last}")

        await asyncio.sleep(float(poll_interval_sec))
