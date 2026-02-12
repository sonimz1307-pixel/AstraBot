# veo_flow.py (PIAPI-only)
import os
import time
from typing import Any, Dict, List, Optional, Literal

import aiohttp

from kling_flow import upload_bytes_to_supabase  # у тебя уже есть и работает :contentReference[oaicite:3]{index=3}
from piapi_veo import PiAPIError, piapi_run_and_wait_for_url


# ============================================================
# Limits (фиксируем как в WebApp/main)
# ============================================================

VEO_ALLOWED_DURATIONS = (4, 6, 8)
VEO_ALLOWED_ASPECT_RATIOS = ("16:9", "9:16")
VEO_PRO_ALLOWED_RESOLUTIONS = ("720p", "1080p")

# PiAPI polling
VEO_MAX_WAIT_SECONDS = int(os.getenv("VEO_MAX_WAIT", os.getenv("PIAPI_MAX_WAIT", "900")))
VEO_POLL_INTERVAL_SECONDS = float(os.getenv("VEO_POLL_INTERVAL", os.getenv("PIAPI_POLL_INTERVAL", "2.0")))

# PiAPI task types mapping
# fast в твоём UI -> veo3.1-video-fast
# pro  в твоём UI -> veo3.1-video
PIAPI_TASK_TYPE_FAST = (os.getenv("PIAPI_VEO_FAST_TASK_TYPE") or "veo3.1-video-fast").strip()
PIAPI_TASK_TYPE_PRO = (os.getenv("PIAPI_VEO_PRO_TASK_TYPE") or "veo3.1-video").strip()

# Storage paths
VEO_BUCKET_PREFIX = (os.getenv("VEO_BUCKET_PREFIX") or "veo_inputs").strip()


Tier = Literal["fast", "pro"]
Mode = Literal["text", "image"]


class VeoFlowError(RuntimeError):
    pass


def _validate_common(*, duration: int, aspect_ratio: str, generate_audio: bool) -> None:
    if duration not in VEO_ALLOWED_DURATIONS:
        raise VeoFlowError(f"Invalid duration={duration}. Allowed: {VEO_ALLOWED_DURATIONS}")
    if aspect_ratio not in VEO_ALLOWED_ASPECT_RATIOS:
        raise VeoFlowError(f"Invalid aspect_ratio={aspect_ratio}. Allowed: {VEO_ALLOWED_ASPECT_RATIOS}")
    if not isinstance(generate_audio, bool):
        raise VeoFlowError("generate_audio must be boolean")


def _validate_pro(
    *,
    resolution: str,
    reference_images: Optional[List[str]],
    image_url: Optional[str],
    last_frame_url: Optional[str],
) -> None:
    if resolution not in VEO_PRO_ALLOWED_RESOLUTIONS:
        raise VeoFlowError(f"PRO resolution must be one of {VEO_PRO_ALLOWED_RESOLUTIONS}")

    # В PiAPI reference_image_urls: 1..3 (а не 4)
    if reference_images:
        if not isinstance(reference_images, list):
            raise VeoFlowError("reference_images must be a list")
        if len(reference_images) > 3:
            # безопасно обрежем до 3, чтобы не падать
            del reference_images[3:]
        for u in reference_images:
            if not isinstance(u, str) or not u.strip():
                raise VeoFlowError("reference_images must contain non-empty strings")

    # tail image (last frame) — можно только если есть стартовый кадр
    if last_frame_url and not image_url:
        raise VeoFlowError("last_frame requires image (start frame)")


def _tier_from_params(tier: Optional[str], model: Optional[str]) -> Tier:
    t = (tier or "").strip().lower()
    if t in ("pro", "premium", "31", "3.1"):
        return "pro"
    if t in ("fast", "std", "standard", "3", "3-fast"):
        return "fast"

    m = (model or "").strip().lower()
    if "3.1" in m or m.endswith("pro") or "veo-3.1" in m:
        return "pro"
    return "fast"


def _sec_to_str(duration: int) -> str:
    # PiAPI ждёт "4s"/"6s"/"8s"
    return f"{int(duration)}s"


def _make_path(user_id: int, name: str, ext: str = "jpg") -> str:
    ts = int(time.time())
    return f"{VEO_BUCKET_PREFIX}/{user_id}/{ts}_{name}.{ext}"


async def _ensure_url_from_bytes(
    *,
    user_id: int,
    bytes_data: Optional[bytes],
    url: Optional[str],
    name: str,
    content_type: str = "image/jpeg",
) -> Optional[str]:
    if url:
        return url
    if not bytes_data:
        return None
    path = _make_path(user_id, name, "jpg")
    return upload_bytes_to_supabase(path, bytes_data, content_type)


def _build_piapi_payload(
    *,
    task_type: str,
    prompt: str,
    image_url: Optional[str],
    tail_image_url: Optional[str],
    reference_image_urls: Optional[List[str]],
    aspect_ratio: str,
    duration: int,
    resolution: str,
    generate_audio: bool,
) -> Dict[str, Any]:
    # Важно: в их спеке model фикс "veo3.1" и task_type "veo3.1-video(-fast)" (у них с точкой, но часто принимают и без)
    payload: Dict[str, Any] = {
        "model": "veo3.1",
        "task_type": task_type,
        "input": {
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "duration": _sec_to_str(duration),
            "resolution": resolution,
            "generate_audio": bool(generate_audio),
        },
    }

    # Если PiAPI реально поддерживает text->video без image_url — ок.
    # Если нет — они вернут понятную ошибку, main покажет.
    if image_url:
        payload["input"]["image_url"] = image_url

    if reference_image_urls:
        # По спеке: refs игнорят tail, и доступны только при 16:9 и 8s
        payload["input"]["reference_image_urls"] = reference_image_urls[:3]
    elif tail_image_url:
        payload["input"]["tail_image_url"] = tail_image_url

    # Если хочешь потом webhook — добавишь config.webhook_config сюда.
    return payload


# ============================================================
# Public API — то, что вызывает main.py
# ============================================================

async def run_veo_text_to_video(
    *,
    user_id: int,
    model: str = "fast",
    prompt: str,
    duration: int = 8,
    resolution: str = "1080p",
    aspect_ratio: str = "16:9",
    generate_audio: bool = False,
    negative_prompt: Optional[str] = None,
    tier: Optional[str] = None,
    reference_images: Optional[List[str]] = None,
    session: Optional[aiohttp.ClientSession] = None,
    **_ignore: Any,
) -> str:
    _ = user_id
    _ = negative_prompt

    chosen_tier = _tier_from_params(tier, model)
    _validate_common(duration=duration, aspect_ratio=aspect_ratio, generate_audio=generate_audio)

    task_type = PIAPI_TASK_TYPE_PRO if chosen_tier == "pro" else PIAPI_TASK_TYPE_FAST

    # PiAPI text->video: пробуем без image_url (если их сервер требует image_url — увидишь ошибку в боте)
    payload = _build_piapi_payload(
        task_type=task_type,
        prompt=prompt,
        image_url=None,
        tail_image_url=None,
        reference_image_urls=(reference_images[:3] if reference_images else None),
        aspect_ratio=aspect_ratio,
        duration=duration,
        resolution=("1080p" if chosen_tier == "pro" else "720p"),
        generate_audio=generate_audio,
    )

    close_session = False
    if session is None:
        session = aiohttp.ClientSession()
        close_session = True

    try:
        return await piapi_run_and_wait_for_url(
            session,
            payload,
            max_wait_seconds=VEO_MAX_WAIT_SECONDS,
            poll_interval_seconds=VEO_POLL_INTERVAL_SECONDS,
        )
    except PiAPIError as e:
        raise VeoFlowError(str(e)) from e
    finally:
        if close_session:
            await session.close()


async def run_veo_image_to_video(
    *,
    user_id: int,
    model: str = "fast",
    image_url: Optional[str] = None,
    image_bytes: Optional[bytes] = None,
    prompt: str,
    duration: int = 8,
    resolution: str = "1080p",
    aspect_ratio: str = "16:9",
    generate_audio: bool = False,
    negative_prompt: Optional[str] = None,
    tier: Optional[str] = None,
    last_frame_url: Optional[str] = None,
    last_frame_bytes: Optional[bytes] = None,
    reference_images: Optional[List[str]] = None,
    reference_images_bytes: Optional[List[bytes]] = None,
    session: Optional[aiohttp.ClientSession] = None,
    **_ignore: Any,
) -> str:
    _ = negative_prompt
    _validate_common(duration=duration, aspect_ratio=aspect_ratio, generate_audio=generate_audio)

    chosen_tier = _tier_from_params(tier, model)
    task_type = PIAPI_TASK_TYPE_PRO if chosen_tier == "pro" else PIAPI_TASK_TYPE_FAST

    # 1) bytes -> Supabase public URLs
    image_url = await _ensure_url_from_bytes(
        user_id=user_id,
        bytes_data=image_bytes,
        url=image_url,
        name="start",
        content_type="image/jpeg",
    )
    if not image_url:
        raise VeoFlowError("Для Image→Video нужен стартовый кадр (image_bytes или image_url).")

    # last frame
    last_frame_url = await _ensure_url_from_bytes(
        user_id=user_id,
        bytes_data=last_frame_bytes,
        url=last_frame_url,
        name="tail",
        content_type="image/jpeg",
    )

    # reference images (обрежем до 3 для PiAPI)
    if (not reference_images) and reference_images_bytes:
        uploaded: List[str] = []
        for i, b in enumerate(reference_images_bytes[:3]):
            u = upload_bytes_to_supabase(_make_path(user_id, f"ref{i+1}", "jpg"), b, "image/jpeg")
            uploaded.append(u)
        reference_images = uploaded
    elif reference_images:
        reference_images = reference_images[:3]

    # 2) pro-валидация
    if chosen_tier == "pro":
        _validate_pro(
            resolution=resolution,
            reference_images=reference_images,
            image_url=image_url,
            last_frame_url=last_frame_url,
        )
    else:
        # fast принудительно 720p
        resolution = "720p"

    # 3) PiAPI ограничения на refs (по их спеке)
    refs_to_send = None
    tail_to_send = None

    if reference_images:
        # refs доступны только при 16:9 и 8s
        if aspect_ratio == "16:9" and int(duration) == 8:
            refs_to_send = reference_images[:3]
        else:
            # мягко игнорим refs (чтобы не ломать UX)
            refs_to_send = None

    if (not refs_to_send) and last_frame_url:
        tail_to_send = last_frame_url

    payload = _build_piapi_payload(
        task_type=task_type,
        prompt=prompt,
        image_url=image_url,
        tail_image_url=tail_to_send,
        reference_image_urls=refs_to_send,
        aspect_ratio=aspect_ratio,
        duration=duration,
        resolution=resolution,
        generate_audio=generate_audio,
    )

    close_session = False
    if session is None:
        session = aiohttp.ClientSession()
        close_session = True

    try:
        return await piapi_run_and_wait_for_url(
            session,
            payload,
            max_wait_seconds=VEO_MAX_WAIT_SECONDS,
            poll_interval_seconds=VEO_POLL_INTERVAL_SECONDS,
        )
    except PiAPIError as e:
        raise VeoFlowError(str(e)) from e
    finally:
        if close_session:
            await session.close()
