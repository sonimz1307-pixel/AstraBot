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


TOPAZ_IMAGE_VERSION = os.getenv("TOPAZ_IMAGE_VERSION", "").strip()
TOPAZ_IMAGE_POLL_TIMEOUT_SEC = int(os.getenv("TOPAZ_IMAGE_POLL_TIMEOUT_SEC", "1800"))
TOPAZ_IMAGE_POLL_SEC = float(os.getenv("TOPAZ_IMAGE_POLL_SEC", "5"))

ENHANCE_MODEL_STANDARD_V2 = "Standard V2"
ENHANCE_MODEL_LOW_RES_V2 = "Low Resolution V2"
ENHANCE_MODEL_CGI = "CGI"
ENHANCE_MODEL_HIGH_FIDELITY_V2 = "High Fidelity V2"
ENHANCE_MODEL_TEXT_REFINE = "Text Refine"

KNOWN_ENHANCE_MODELS = {
    ENHANCE_MODEL_STANDARD_V2,
    ENHANCE_MODEL_LOW_RES_V2,
    ENHANCE_MODEL_CGI,
    ENHANCE_MODEL_HIGH_FIDELITY_V2,
    ENHANCE_MODEL_TEXT_REFINE,
}

# NOTE:
# Replicate page examples show strings like "2x", "4x", "jpg", "Foreground", "None".
# Keep this layer permissive. Final button choices should be controlled by your bot UI.
DEFAULT_OUTPUT_FORMAT = "jpg"
DEFAULT_ENHANCE_MODEL = ENHANCE_MODEL_STANDARD_V2
DEFAULT_SUBJECT_DETECTION = "None"
DEFAULT_UPSCALE_FACTOR = "2x"


@dataclass(slots=True)
class TopazImageParams:
    image_url: str
    enhance_model: str = DEFAULT_ENHANCE_MODEL
    upscale_factor: str = DEFAULT_UPSCALE_FACTOR
    output_format: str = DEFAULT_OUTPUT_FORMAT
    subject_detection: str = DEFAULT_SUBJECT_DETECTION
    face_enhancement: bool = False
    face_enhancement_creativity: float = 0.0
    face_enhancement_strength: float = 0.8

    def validate(self) -> None:
        if not str(self.image_url or "").strip():
            raise ReplicateError("Topaz Image: image_url is empty")

        if self.enhance_model not in KNOWN_ENHANCE_MODELS:
            raise ReplicateError(
                f"Topaz Image: unsupported enhance_model={self.enhance_model!r}. "
                f"Allowed: {sorted(KNOWN_ENHANCE_MODELS)}"
            )

        if not str(self.upscale_factor or "").strip():
            raise ReplicateError("Topaz Image: upscale_factor is empty")

        if not str(self.output_format or "").strip():
            raise ReplicateError("Topaz Image: output_format is empty")

        if not str(self.subject_detection or "").strip():
            raise ReplicateError("Topaz Image: subject_detection is empty")

        if not (0.0 <= float(self.face_enhancement_creativity) <= 1.0):
            raise ReplicateError("Topaz Image: face_enhancement_creativity must be in range 0..1")

        if not (0.0 <= float(self.face_enhancement_strength) <= 1.0):
            raise ReplicateError("Topaz Image: face_enhancement_strength must be in range 0..1")


@dataclass(slots=True)
class TopazImageResult:
    prediction_id: str
    status: str
    output_url: str
    raw: dict[str, Any]


def build_topaz_image_input(params: TopazImageParams) -> dict[str, Any]:
    params.validate()
    return {
        "image": params.image_url,
        "enhance_model": params.enhance_model,
        "upscale_factor": params.upscale_factor,
        "output_format": params.output_format,
        "subject_detection": params.subject_detection,
        "face_enhancement": bool(params.face_enhancement),
        "face_enhancement_creativity": float(params.face_enhancement_creativity),
        "face_enhancement_strength": float(params.face_enhancement_strength),
    }


def _require_version(version: Optional[str] = None) -> str:
    v = str(version or TOPAZ_IMAGE_VERSION or "").strip()
    if not v:
        raise ReplicateError(
            "TOPAZ_IMAGE_VERSION is not set. "
            "Set exact Replicate version hash in env or pass version explicitly."
        )
    return v


async def create_topaz_image_prediction(
    params: TopazImageParams,
    *,
    version: Optional[str] = None,
    webhook: Optional[str] = None,
    webhook_events_filter: Optional[list[str]] = None,
) -> ReplicatePrediction:
    return await create_prediction(
        version=_require_version(version),
        input_data=build_topaz_image_input(params),
        webhook=webhook,
        webhook_events_filter=webhook_events_filter,
    )


async def wait_topaz_image_prediction(
    prediction_id: str,
    *,
    timeout_sec: int = TOPAZ_IMAGE_POLL_TIMEOUT_SEC,
    sleep_sec: float = TOPAZ_IMAGE_POLL_SEC,
) -> TopazImageResult:
    done = await poll_prediction(prediction_id, timeout_sec=timeout_sec, sleep_sec=sleep_sec)
    if done.status != "succeeded":
        raise ReplicateError(done.error or f"Topaz Image prediction failed with status={done.status}")
    output_url = extract_output_url(done)
    if not output_url:
        raise ReplicateError("Topaz Image finished but output URL is empty")
    return TopazImageResult(
        prediction_id=done.id,
        status=done.status,
        output_url=output_url,
        raw=done.raw,
    )


async def run_topaz_image_upscale(
    params: TopazImageParams,
    *,
    version: Optional[str] = None,
    webhook: Optional[str] = None,
    webhook_events_filter: Optional[list[str]] = None,
    timeout_sec: int = TOPAZ_IMAGE_POLL_TIMEOUT_SEC,
    sleep_sec: float = TOPAZ_IMAGE_POLL_SEC,
) -> TopazImageResult:
    done = await create_and_poll_prediction(
        version=_require_version(version),
        input_data=build_topaz_image_input(params),
        webhook=webhook,
        webhook_events_filter=webhook_events_filter,
        poll_timeout_sec=timeout_sec,
        poll_sleep_sec=sleep_sec,
    )
    if done.status != "succeeded":
        raise ReplicateError(done.error or f"Topaz Image prediction failed with status={done.status}")
    output_url = extract_output_url(done)
    if not output_url:
        raise ReplicateError("Topaz Image finished but output URL is empty")
    return TopazImageResult(
        prediction_id=done.id,
        status=done.status,
        output_url=output_url,
        raw=done.raw,
    )
