from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

from replicate_common import (
    ReplicateError,
    ReplicatePrediction,
    create_and_poll_prediction,
    create_prediction,
    extract_output_url,
    poll_prediction,
)


TOPAZ_VIDEO_VERSION = os.getenv("TOPAZ_VIDEO_VERSION", "").strip()
TOPAZ_VIDEO_POLL_TIMEOUT_SEC = int(os.getenv("TOPAZ_VIDEO_POLL_TIMEOUT_SEC", "1800"))
TOPAZ_VIDEO_POLL_SEC = float(os.getenv("TOPAZ_VIDEO_POLL_SEC", "6"))

RESOLUTION_720P = "720p"
RESOLUTION_1080P = "1080p"
RESOLUTION_4K = "4k"

KNOWN_TARGET_RESOLUTIONS = {RESOLUTION_720P, RESOLUTION_1080P, RESOLUTION_4K}
DEFAULT_TARGET_RESOLUTION = RESOLUTION_1080P
DEFAULT_TARGET_FPS = 30


@dataclass(slots=True)
class TopazVideoParams:
    video_url: str
    target_resolution: str = DEFAULT_TARGET_RESOLUTION
    target_fps: int = DEFAULT_TARGET_FPS

    def validate(self) -> None:
        if not str(self.video_url or "").strip():
            raise ReplicateError("Topaz Video: video_url is empty")
        if self.target_resolution not in KNOWN_TARGET_RESOLUTIONS:
            raise ReplicateError(
                f"Topaz Video: unsupported target_resolution={self.target_resolution!r}. "
                f"Allowed: {sorted(KNOWN_TARGET_RESOLUTIONS)}"
            )
        fps = int(self.target_fps)
        if fps < 15 or fps > 60:
            raise ReplicateError("Topaz Video: target_fps must be in range 15..60")


@dataclass(slots=True)
class TopazVideoResult:
    prediction_id: str
    status: str
    output_url: str
    raw: dict[str, Any]


def build_topaz_video_input(params: TopazVideoParams) -> dict[str, Any]:
    params.validate()
    return {
        "video": params.video_url,
        "target_resolution": params.target_resolution,
        "target_fps": int(params.target_fps),
    }


def _require_version(version: Optional[str] = None) -> str:
    v = str(version or TOPAZ_VIDEO_VERSION or "").strip()
    if not v:
        raise ReplicateError(
            "TOPAZ_VIDEO_VERSION is not set. "
            "Set exact Replicate version hash in env or pass version explicitly."
        )
    return v


async def create_topaz_video_prediction(
    params: TopazVideoParams,
    *,
    version: Optional[str] = None,
    webhook: Optional[str] = None,
    webhook_events_filter: Optional[list[str]] = None,
) -> ReplicatePrediction:
    return await create_prediction(
        version=_require_version(version),
        input_data=build_topaz_video_input(params),
        webhook=webhook,
        webhook_events_filter=webhook_events_filter,
    )


async def wait_topaz_video_prediction(
    prediction_id: str,
    *,
    timeout_sec: int = TOPAZ_VIDEO_POLL_TIMEOUT_SEC,
    sleep_sec: float = TOPAZ_VIDEO_POLL_SEC,
) -> TopazVideoResult:
    done = await poll_prediction(prediction_id, timeout_sec=timeout_sec, sleep_sec=sleep_sec)
    if done.status != "succeeded":
        raise ReplicateError(done.error or f"Topaz Video prediction failed with status={done.status}")
    output_url = extract_output_url(done)
    if not output_url:
        raise ReplicateError("Topaz Video finished but output URL is empty")
    return TopazVideoResult(
        prediction_id=done.id,
        status=done.status,
        output_url=output_url,
        raw=done.raw,
    )


async def run_topaz_video_upscale(
    params: TopazVideoParams,
    *,
    version: Optional[str] = None,
    webhook: Optional[str] = None,
    webhook_events_filter: Optional[list[str]] = None,
    timeout_sec: int = TOPAZ_VIDEO_POLL_TIMEOUT_SEC,
    sleep_sec: float = TOPAZ_VIDEO_POLL_SEC,
) -> TopazVideoResult:
    done = await create_and_poll_prediction(
        version=_require_version(version),
        input_data=build_topaz_video_input(params),
        webhook=webhook,
        webhook_events_filter=webhook_events_filter,
        poll_timeout_sec=timeout_sec,
        poll_sleep_sec=sleep_sec,
    )
    if done.status != "succeeded":
        raise ReplicateError(done.error or f"Topaz Video prediction failed with status={done.status}")
    output_url = extract_output_url(done)
    if not output_url:
        raise ReplicateError("Topaz Video finished but output URL is empty")
    return TopazVideoResult(
        prediction_id=done.id,
        status=done.status,
        output_url=output_url,
        raw=done.raw,
    )
