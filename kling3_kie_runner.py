from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple

from kling3_kie_flow import (
    Kling3KieError,
    create_kling3_kie_task,
    extract_kling3_kie_task_id,
    get_kling3_kie_task,
    normalize_kling3_kie_task,
)


class Kling3KieRunnerError(Exception):
    pass


async def run_kling3_kie_task_and_wait(
    *,
    prompt: str = "",
    duration: int = 5,
    mode: str = "pro",
    enable_audio: bool = False,
    aspect_ratio: str = "16:9",
    generation_mode: str = "text_to_video",
    start_image_url: Optional[str] = None,
    end_image_url: Optional[str] = None,
    multi_shots: Optional[List[Dict[str, Any]]] = None,
    kling_elements: Optional[List[Dict[str, Any]]] = None,
    poll_interval_sec: float = 5.0,
    timeout_sec: int = 1800,
) -> Tuple[str, Dict[str, Any], Optional[str]]:
    try:
        created = await create_kling3_kie_task(
            prompt=prompt,
            duration=duration,
            mode=mode,
            enable_audio=enable_audio,
            aspect_ratio=aspect_ratio,
            generation_mode=generation_mode,
            start_image_url=start_image_url,
            end_image_url=end_image_url,
            multi_shots=multi_shots,
            kling_elements=kling_elements,
        )
    except Kling3KieError as exc:
        raise Kling3KieRunnerError(f"Kling 3.0 - New create failed: {exc}")

    task_id = extract_kling3_kie_task_id(created)
    if not task_id:
        raise Kling3KieRunnerError(f"Kling 3.0 - New create did not return taskId: {created}")

    loop = asyncio.get_event_loop()
    started = loop.time()
    last: Dict[str, Any] = {}
    while True:
        try:
            last = await get_kling3_kie_task(task_id)
        except Kling3KieError as exc:
            raise Kling3KieRunnerError(f"Kling 3.0 - New status failed: {exc}")
        normalized = normalize_kling3_kie_task(last)
        if normalized.get("status") == "failed":
            raise Kling3KieRunnerError(normalized.get("error_message") or f"Kling 3.0 - New failed: {normalized.get('provider_status')}")
        video_url = str(normalized.get("video_url") or normalized.get("output_url") or "").strip()
        if video_url and normalized.get("finished"):
            return task_id, last, video_url
        if (loop.time() - started) >= float(timeout_sec):
            raise Kling3KieRunnerError(f"Kling 3.0 - New timeout after {timeout_sec}s (taskId={task_id})")
        await asyncio.sleep(float(poll_interval_sec))
