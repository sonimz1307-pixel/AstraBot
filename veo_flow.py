# veo_flow.py
import os
from typing import Any, Dict, List, Optional, Literal

import aiohttp

from replicate_http import (
    ReplicateHTTPError,
    post_prediction,
    get_prediction_get_url,
    wait_for_result_url,
)

# Replicate model slugs
REPLICATE_VEO_FAST_MODEL = (os.getenv("REPLICATE_VEO_FAST_MODEL") or "google/veo-3-fast").strip()
REPLICATE_VEO_31_MODEL = (os.getenv("REPLICATE_VEO_31_MODEL") or "google/veo-3.1").strip()

# Ограничения MVP (ты их утвердил)
VEO_ALLOWED_DURATIONS = (4, 6, 8)
VEO_ALLOWED_ASPECT_RATIOS = ("16:9", "9:16")
VEO_FAST_RESOLUTION = "720p"
VEO_PRO_ALLOWED_RESOLUTIONS = ("720p", "1080p")

# Replicate polling
VEO_MAX_WAIT_SECONDS = int(os.getenv("VEO_MAX_WAIT", os.getenv("REPLICATE_MAX_WAIT", "900")))
VEO_POLL_INTERVAL_SECONDS = float(os.getenv("VEO_POLL_INTERVAL", os.getenv("REPLICATE_POLL_INTERVAL", "2.0")))


class VeoFlowError(RuntimeError):
    pass


Tier = Literal["fast", "pro"]
Mode = Literal["text", "image"]


def _validate_common(
    *,
    duration: int,
    aspect_ratio: str,
    generate_audio: bool,
) -> None:
    if duration not in VEO_ALLOWED_DURATIONS:
        raise VeoFlowError(f"Invalid duration={duration}. Allowed: {VEO_ALLOWED_DURATIONS}")
    if aspect_ratio not in VEO_ALLOWED_ASPECT_RATIOS:
        raise VeoFlowError(f"Invalid aspect_ratio={aspect_ratio}. Allowed: {VEO_ALLOWED_ASPECT_RATIOS}")
    if not isinstance(generate_audio, bool):
        raise VeoFlowError("generate_audio must be boolean")


def _validate_fast(*, resolution: str) -> None:
    if resolution != VEO_FAST_RESOLUTION:
        raise VeoFlowError(f"FAST resolution must be {VEO_FAST_RESOLUTION}")


def _validate_pro(*, resolution: str, reference_images: Optional[List[str]], image_url: Optional[str], last_frame_url: Optional[str]) -> None:
    if resolution not in VEO_PRO_ALLOWED_RESOLUTIONS:
        raise VeoFlowError(f"PRO resolution must be one of {VEO_PRO_ALLOWED_RESOLUTIONS}")
    if reference_images:
        if not isinstance(reference_images, list):
            raise VeoFlowError("reference_images must be a list of URLs")
        if len(reference_images) > 4:
            raise VeoFlowError("reference_images max is 4")
        for u in reference_images:
            if not isinstance(u, str) or not u.strip():
                raise VeoFlowError("reference_images must contain non-empty strings")
    if last_frame_url:
        # last_frame only makes sense with a start image (A->B)
        if not image_url:
            raise VeoFlowError("last_frame requires image (start frame).")


def _build_input_fast(
    *,
    prompt: str,
    mode: Mode,
    image_url: Optional[str],
    duration: int,
    aspect_ratio: str,
    generate_audio: bool,
) -> Dict[str, Any]:
    if not prompt or not isinstance(prompt, str):
        raise VeoFlowError("prompt is required and must be a string")

    _validate_common(duration=duration, aspect_ratio=aspect_ratio, generate_audio=generate_audio)
    _validate_fast(resolution=VEO_FAST_RESOLUTION)

    inp: Dict[str, Any] = {
        "prompt": prompt,
        "duration": duration,
        "resolution": VEO_FAST_RESOLUTION,
        "aspect_ratio": aspect_ratio,
        "generate_audio": generate_audio,
    }

    if mode == "image":
        if not image_url:
            raise VeoFlowError("image_url is required for image mode")
        inp["image"] = image_url

    return inp


def _build_input_pro(
    *,
    prompt: str,
    mode: Mode,
    image_url: Optional[str],
    last_frame_url: Optional[str],
    reference_images: Optional[List[str]],
    duration: int,
    resolution: str,
    aspect_ratio: str,
    generate_audio: bool,
) -> Dict[str, Any]:
    if not prompt or not isinstance(prompt, str):
        raise VeoFlowError("prompt is required and must be a string")

    _validate_common(duration=duration, aspect_ratio=aspect_ratio, generate_audio=generate_audio)
    _validate_pro(
        resolution=resolution,
        reference_images=reference_images,
        image_url=image_url,
        last_frame_url=last_frame_url,
    )

    inp: Dict[str, Any] = {
        "prompt": prompt,
        "duration": duration,
        "resolution": resolution,
        "aspect_ratio": aspect_ratio,
        "generate_audio": generate_audio,
    }

    if mode == "image":
        if not image_url:
            raise VeoFlowError("image_url is required for image mode")
        inp["image"] = image_url

    # PRO-only features
    if reference_images:
        inp["reference_images"] = reference_images

    if last_frame_url:
        inp["last_frame"] = last_frame_url

    return inp


async def run_veo_fast(
    *,
    prompt: str,
    mode: Mode,
    image_url: Optional[str] = None,
    duration: int = 8,
    aspect_ratio: str = "16:9",
    generate_audio: bool = False,
    session: Optional[aiohttp.ClientSession] = None,
) -> str:
    """
    Returns video URL (mp4) for google/veo-3-fast.
    FAST: resolution fixed at 720p.
    """
    inp = _build_input_fast(
        prompt=prompt,
        mode=mode,
        image_url=image_url,
        duration=duration,
        aspect_ratio=aspect_ratio,
        generate_audio=generate_audio,
    )
    payload = {"input": inp}

    close_session = False
    if session is None:
        session = aiohttp.ClientSession()
        close_session = True

    try:
        pred = await post_prediction(session, REPLICATE_VEO_FAST_MODEL, payload)
        get_url = get_prediction_get_url(pred)
        if not get_url:
            raise VeoFlowError(f"Replicate returned no urls.get: {pred}")
        out_url = await wait_for_result_url(
            session,
            get_url,
            max_wait_seconds=VEO_MAX_WAIT_SECONDS,
            poll_interval_seconds=VEO_POLL_INTERVAL_SECONDS,
        )
        return out_url
    except ReplicateHTTPError as e:
        raise VeoFlowError(str(e)) from e
    finally:
        if close_session:
            await session.close()


async def run_veo_31(
    *,
    prompt: str,
    mode: Mode,
    image_url: Optional[str] = None,
    last_frame_url: Optional[str] = None,
    reference_images: Optional[List[str]] = None,
    duration: int = 8,
    resolution: str = "1080p",
    aspect_ratio: str = "16:9",
    generate_audio: bool = False,
    session: Optional[aiohttp.ClientSession] = None,
) -> str:
    """
    Returns video URL (mp4) for google/veo-3.1.
    PRO: supports 720p/1080p, reference_images (0..4), last_frame (requires image).
    """
    inp = _build_input_pro(
        prompt=prompt,
        mode=mode,
        image_url=image_url,
        last_frame_url=last_frame_url,
        reference_images=reference_images,
        duration=duration,
        resolution=resolution,
        aspect_ratio=aspect_ratio,
        generate_audio=generate_audio,
    )
    payload = {"input": inp}

    close_session = False
    if session is None:
        session = aiohttp.ClientSession()
        close_session = True

    try:
        pred = await post_prediction(session, REPLICATE_VEO_31_MODEL, payload)
        get_url = get_prediction_get_url(pred)
        if not get_url:
            raise VeoFlowError(f"Replicate returned no urls.get: {pred}")
        out_url = await wait_for_result_url(
            session,
            get_url,
            max_wait_seconds=VEO_MAX_WAIT_SECONDS,
            poll_interval_seconds=VEO_POLL_INTERVAL_SECONDS,
        )
        return out_url
    except ReplicateHTTPError as e:
        raise VeoFlowError(str(e)) from e
    finally:
        if close_session:
            await session.close()
