import asyncio
from typing import Any, Dict, Optional, Tuple, List


def _extract_error_message(task_resp: Dict[str, Any]) -> str:
    """Try to extract provider error message from PiAPI task response."""
    if not isinstance(task_resp, dict):
        return ""
    data = task_resp.get("data") if isinstance(task_resp.get("data"), dict) else task_resp
    err = data.get("error") or {}
    if isinstance(err, dict):
        msg = (err.get("message") or err.get("raw_message") or "").strip()
        if msg:
            return msg
    # sometimes 'detail' or 'logs' may exist
    for k in ("detail", "logs", "message"):
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""

from kling3_flow import create_kling3_task, get_kling3_task, Kling3Error


class Kling3RunnerError(Exception):
    pass


def _extract_task_id(resp: Dict[str, Any]) -> Optional[str]:
    data = resp.get("data") if isinstance(resp, dict) else None
    if isinstance(data, dict) and data.get("task_id"):
        return str(data.get("task_id"))
    return None


def _extract_video_url(task_json: Dict[str, Any]) -> Optional[str]:
    data = task_json.get("data") if isinstance(task_json, dict) else None
    if not isinstance(data, dict):
        return None
    output = data.get("output") or {}
    if isinstance(output, dict):
        v = (output.get("video") or "").strip()
        return v or None
    return None


async def run_kling3_task_and_wait(
    *,
    # Text-to-video fallback (ignored if multi_shots present)
    prompt: str = "",
    duration: int = 5,
    resolution: str,
    enable_audio: bool,
    aspect_ratio: str = "16:9",
    prefer_multi_shots: bool = False,
    multi_shots: Optional[List[Dict[str, Any]]] = None,
    # Image->Video
    start_image_bytes: Optional[bytes] = None,
    end_image_bytes: Optional[bytes] = None,
    # Polling
    poll_interval_sec: float = 2.0,
    timeout_sec: int = 3600,
) -> Tuple[str, Dict[str, Any], Optional[str]]:
    """Create Kling 3.0 task and wait until Completed/Failed."""
    try:
        created = await create_kling3_task(
            prompt=prompt,
            duration=int(duration),
            resolution=str(resolution),
            enable_audio=bool(enable_audio),
            aspect_ratio=str(aspect_ratio),
            prefer_multi_shots=bool(prefer_multi_shots),
            multi_shots=multi_shots,
            start_image_bytes=start_image_bytes,
            end_image_bytes=end_image_bytes,
        )
    except Kling3Error as e:
        raise Kling3RunnerError(f"Kling3 create failed: {e}")

    task_id = _extract_task_id(created)
    if not task_id:
        raise Kling3RunnerError(f"Kling3 create did not return task_id: {created}")

    t0 = asyncio.get_event_loop().time()
    last: Dict[str, Any] = {}

    while True:
        try:
            last = await get_kling3_task(task_id)
        except Kling3Error as e:
            raise Kling3RunnerError(f"Kling3 get failed: {e}")

        data = last.get("data") if isinstance(last, dict) else None
        status = (data.get("status") if isinstance(data, dict) else "") or ""
        status_l = str(status).lower()

        if status_l in ("completed", "succeed", "succeeded", "success", "done", "finished", "failed", "error", "canceled", "cancelled"):
                break

        if (asyncio.get_event_loop().time() - t0) > float(timeout_sec):
            raise Kling3RunnerError(f"Kling3 timeout after {timeout_sec}s (task_id={task_id}, status={status})")

        await asyncio.sleep(float(poll_interval_sec))

    if str(status).lower() in ("failed", "error", "canceled", "cancelled"):
        # keep the raw error if present
        err = None
        if isinstance(data, dict):
            err = data.get("error") or {}
        raise Kling3RunnerError(f"Kling3 failed: {status}. {err}")

    video_url = _extract_video_url(last)
    return (task_id, last, video_url)
