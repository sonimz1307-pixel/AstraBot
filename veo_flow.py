import os
from typing import Any, Dict, List, Optional, Literal

import aiohttp

from replicate_http import (
    ReplicateHTTPError,
    post_prediction,
    get_prediction_get_url,
    wait_for_result_url,
)


# ============================================================
# Replicate Files API upload helpers
# ============================================================

REPLICATE_API_TOKEN = (os.getenv("REPLICATE_API_TOKEN") or os.getenv("REPLICATE_TOKEN") or "").strip()
REPLICATE_FILES_ENDPOINT = "https://api.replicate.com/v1/files"


class ReplicateUploadError(RuntimeError):
    pass


async def _replicate_upload_bytes(
    data: bytes,
    *,
    filename: str,
    content_type: str,
    session: Optional[aiohttp.ClientSession] = None,
) -> str:
    """Upload bytes to Replicate Files API and return a public download URL."""
    if not REPLICATE_API_TOKEN:
        raise ReplicateUploadError("REPLICATE_API_TOKEN is not set")

    close_session = False
    if session is None:
        session = aiohttp.ClientSession()
        close_session = True

    try:
        form = aiohttp.FormData()
        form.add_field("file", data, filename=filename, content_type=content_type)

        headers = {"Authorization": f"Bearer {REPLICATE_API_TOKEN}"}

        async with session.post(REPLICATE_FILES_ENDPOINT, data=form, headers=headers) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise ReplicateUploadError(f"Replicate files upload failed: HTTP {resp.status} {text}")

            try:
                js = await resp.json()
            except Exception:
                raise ReplicateUploadError(f"Replicate files upload returned non-JSON: {text}")

            urls = js.get("urls") or {}
            download_url = urls.get("download") or urls.get("get")
            if not download_url:
                raise ReplicateUploadError(f"Replicate files upload response missing urls.download: {js}")

            return str(download_url)
    finally:
        if close_session:
            await session.close()

# ============================================================
# Replicate model slugs
# ============================================================

REPLICATE_VEO_FAST_MODEL = (os.getenv("REPLICATE_VEO_FAST_MODEL") or "google/veo-3-fast").strip()
REPLICATE_VEO_31_MODEL = (os.getenv("REPLICATE_VEO_31_MODEL") or "google/veo-3.1").strip()

# ============================================================
# MVP limits (зафиксированы)
# ============================================================

VEO_ALLOWED_DURATIONS = (4, 6, 8)
VEO_ALLOWED_ASPECT_RATIOS = ("16:9", "9:16")

VEO_FAST_RESOLUTION = "720p"
VEO_PRO_ALLOWED_RESOLUTIONS = ("720p", "1080p")

# Polling
VEO_MAX_WAIT_SECONDS = int(os.getenv("VEO_MAX_WAIT", os.getenv("REPLICATE_MAX_WAIT", "900")))
VEO_POLL_INTERVAL_SECONDS = float(os.getenv("VEO_POLL_INTERVAL", os.getenv("REPLICATE_POLL_INTERVAL", "2.0")))

# ============================================================
# Types
# ============================================================

Tier = Literal["fast", "pro"]
Mode = Literal["text", "image"]


class VeoFlowError(RuntimeError):
    pass


# ============================================================
# Validators
# ============================================================

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


def _validate_pro(
    *,
    resolution: str,
    reference_images: Optional[List[str]],
    image_url: Optional[str],
    last_frame_url: Optional[str],
) -> None:
    if resolution not in VEO_PRO_ALLOWED_RESOLUTIONS:
        raise VeoFlowError(f"PRO resolution must be one of {VEO_PRO_ALLOWED_RESOLUTIONS}")

    if reference_images:
        if not isinstance(reference_images, list):
            raise VeoFlowError("reference_images must be a list")
        if len(reference_images) > 4:
            raise VeoFlowError("reference_images max is 4")
        for u in reference_images:
            if not isinstance(u, str) or not u.strip():
                raise VeoFlowError("reference_images must contain non-empty strings")

    if last_frame_url and not image_url:
        raise VeoFlowError("last_frame requires image (start frame)")


# ============================================================
# Input builders
# ============================================================

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

    if reference_images:
        inp["reference_images"] = reference_images

    if last_frame_url:
        inp["last_frame"] = last_frame_url

    return inp


# ============================================================
# Low-level runners
# ============================================================

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
    google/veo-3-fast
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

        return await wait_for_result_url(
            session,
            get_url,
            max_wait_seconds=VEO_MAX_WAIT_SECONDS,
            poll_interval_seconds=VEO_POLL_INTERVAL_SECONDS,
        )
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
    google/veo-3.1
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

        return await wait_for_result_url(
            session,
            get_url,
            max_wait_seconds=VEO_MAX_WAIT_SECONDS,
            poll_interval_seconds=VEO_POLL_INTERVAL_SECONDS,
        )
    except ReplicateHTTPError as e:
        raise VeoFlowError(str(e)) from e
    finally:
        if close_session:
            await session.close()


# ============================================================
# COMPAT LAYER — то, что вызывает main.py
# ============================================================

def _tier_from_params(tier: Optional[str], model: Optional[str]) -> Tier:
    """
    Определяем fast/pro по тому, что обычно приходит из main/webapp.
    Поддерживаем:
      - tier="fast|pro"
      - model="fast|pro" или "veo-3-fast" / "veo-3.1" / "google/veo-3.1"
    """
    t = (tier or "").strip().lower()
    if t in ("pro", "premium", "31", "3.1"):
        return "pro"
    if t in ("fast", "std", "standard", "3", "3-fast"):
        return "fast"

    m = (model or "").strip().lower()
    if "3.1" in m or m.endswith("pro") or "veo-3.1" in m:
        return "pro"
    return "fast"


async def run_veo_text_to_video(
    *,
    user_id: int,  # <-- ВАЖНО: main передаёт user_id
    model: str = "fast",
    prompt: str,
    duration: int = 8,
    resolution: str = "1080p",
    aspect_ratio: str = "16:9",
    generate_audio: bool = False,
    negative_prompt: Optional[str] = None,  # сейчас не используем, но не падаем
    tier: Optional[str] = None,             # поддержка старого интерфейса
    reference_images: Optional[List[str]] = None,  # если main вдруг пошлёт URL-ы
    session: Optional[aiohttp.ClientSession] = None,
    **_ignore: Any,  # <-- чтобы больше никогда не падало на лишних kwargs
) -> str:
    """
    Text → Video (совместимо с main.py)
    Для твоего теста (Veo fast, text->video) — сработает сразу.
    """
    _ = user_id  # пока не нужен (нужен будет для загрузки refs/lastframe по bytes, если добавим позже)

    chosen_tier = _tier_from_params(tier, model)

    if chosen_tier == "pro":
        # PRO: можно передавать reference_images как URL (если main даст)
        return await run_veo_31(
            prompt=prompt,
            mode="text",
            duration=duration,
            resolution=resolution,
            aspect_ratio=aspect_ratio,
            generate_audio=generate_audio,
            reference_images=reference_images,
            session=session,
        )

    return await run_veo_fast(
        prompt=prompt,
        mode="text",
        duration=duration,
        aspect_ratio=aspect_ratio,
        generate_audio=generate_audio,
        session=session,
    )


async def run_veo_image_to_video(
    *,
    user_id: int,  # <-- ВАЖНО: main передаёт user_id
    model: str = "fast",
    # Можно передать либо URL, либо bytes (bytes будут загружены в Replicate Files API внутри этого модуля)
    image_url: Optional[str] = None,
    image_bytes: Optional[bytes] = None,
    prompt: str,
    duration: int = 8,
    resolution: str = "1080p",
    aspect_ratio: str = "16:9",
    generate_audio: bool = False,
    negative_prompt: Optional[str] = None,  # сейчас не используем, но не падаем
    tier: Optional[str] = None,             # поддержка старого интерфейса
    last_frame_url: Optional[str] = None,
    last_frame_bytes: Optional[bytes] = None,
    reference_images: Optional[List[str]] = None,
    reference_images_bytes: Optional[List[bytes]] = None,
    session: Optional[aiohttp.ClientSession] = None,
    **_ignore: Any,
) -> str:
    """
    Image → Video (совместимо с main.py)
    """
    _ = user_id
    _ = negative_prompt


# ---- Resolve inputs: bytes -> upload to Replicate Files API ----
session_to_use = session
close_session = False
if session_to_use is None:
    session_to_use = aiohttp.ClientSession()
    close_session = True

try:
    if not image_url:
        if not image_bytes:
            raise ValueError("Either image_url or image_bytes must be provided")
        image_url = await _replicate_upload_bytes(
            image_bytes,
            filename="veo_start.jpg",
            content_type="image/jpeg",
            session=session_to_use,
        )

    if (not last_frame_url) and last_frame_bytes:
        last_frame_url = await _replicate_upload_bytes(
            last_frame_bytes,
            filename="veo_last_frame.jpg",
            content_type="image/jpeg",
            session=session_to_use,
        )

    if (not reference_images) and reference_images_bytes:
        uploaded: List[str] = []
        for i, b in enumerate(reference_images_bytes):
            uploaded.append(
                await _replicate_upload_bytes(
                    b,
                    filename=f"veo_ref_{i+1}.jpg",
                    content_type="image/jpeg",
                    session=session_to_use,
                )
            )
        reference_images = uploaded

    chosen_tier = _tier_from_params(tier, model)

    if chosen_tier == "pro":
        return await run_veo_31(
            prompt=prompt,
            mode="image",
            image_url=image_url,
            last_frame_url=last_frame_url,
            reference_images=reference_images,
            duration=duration,
            resolution=resolution,
            aspect_ratio=aspect_ratio,
            generate_audio=generate_audio,
            session=session_to_use,
        )

    return await run_veo_fast(
        prompt=prompt,
        mode="image",
        image_url=image_url,
        duration=duration,
        aspect_ratio=aspect_ratio,
        generate_audio=generate_audio,
        session=session_to_use,
    )
finally:
    if close_session:
        await session_to_use.close()

