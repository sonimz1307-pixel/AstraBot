from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, List, Optional

import httpx

from nano_banana_2_piapi import handle_nano_banana_2
from nano_banana_pro_new_kie import handle_nano_banana_pro_new
from gpt_image_2_kie import handle_gpt_image_2_kie
from billing_db import add_tokens
from grok_video_replicate import (
    GROK_LEGACY_MODEL,
    GrokVideoError,
    is_grok15_model,
    normalize_grok15_aspect_ratio,
    normalize_grok15_duration,
    normalize_grok15_resolution,
    normalize_grok_aspect_ratio,
    normalize_grok_duration,
    normalize_grok_model,
    normalize_grok_provider_mode,
    normalize_grok_resolution,
    run_grok15_image_to_video,
    run_grok_image_to_video,
    run_grok_text_to_video,
)
from gemini_omni_video import (
    GeminiOmniVideoError,
    KIE_OMNI_VIDEO_EDIT_MAX_DURATION_SEC,
    normalize_gemini_omni_aspect_ratio,
    normalize_gemini_omni_duration,
    normalize_gemini_omni_mode,
    normalize_gemini_omni_resolution,
    run_gemini_omni_video,
)
from veo31_fast_relax_kie import (
    Veo31FastRelaxError,
    normalize_veo31_fast_relax_aspect_ratio,
    normalize_veo31_fast_relax_duration,
    normalize_veo31_fast_relax_resolution,
    run_veo31_fast_relax,
)
from kling3_turbo_kie import (
    normalize_kling3_turbo_aspect_ratio,
    normalize_kling3_turbo_duration,
    normalize_kling3_turbo_mode,
    normalize_kling3_turbo_resolution,
    run_kling3_turbo_task_and_wait,
)
from app.routers import web_workspace_api as ww
from app.services.legnext_midjourney import (
    LegnextMidjourneyError,
    create_midjourney_diffusion,
    create_midjourney_reroll,
    create_midjourney_variation,
    get_midjourney_job,
)
from app.services.video_editor_service import (
    build_workspace_video_access_urls,
    get_workspace_upload_row,
)

TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
_TELEGRAM_API_BASE_RAW = (os.getenv("TELEGRAM_API_BASE") or "").strip()


def _telegram_method_url(method: str) -> str:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    raw = _TELEGRAM_API_BASE_RAW.rstrip("/")
    if raw:
        if TELEGRAM_BOT_TOKEN in raw:
            return f"{raw}/{method}"
        if raw.endswith("/bot"):
            return f"{raw}{TELEGRAM_BOT_TOKEN}/{method}"
        if raw.endswith("/bot/"):
            return f"{raw}{TELEGRAM_BOT_TOKEN}/{method}"
        if raw.endswith("/bot" + TELEGRAM_BOT_TOKEN):
            return f"{raw}/{method}"
    return f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"


async def _tg_post(method: str, *, json_payload: Optional[Dict[str, Any]] = None, data: Optional[Dict[str, Any]] = None) -> None:
    url = _telegram_method_url(method)
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(url, json=json_payload, data=data)
    if resp.status_code >= 400:
        raise RuntimeError(f"Telegram {method} HTTP {resp.status_code}: {resp.text[:800]}")
    try:
        payload = resp.json()
    except Exception:
        return
    if isinstance(payload, dict) and not payload.get("ok", False):
        raise RuntimeError(f"Telegram {method} error: {payload}")


async def _tg_send_message(chat_id: int, text: str) -> None:
    await _tg_post("sendMessage", json_payload={"chat_id": int(chat_id), "text": str(text or "")})


async def _tg_send_video_url(chat_id: int, video_url: str, caption: Optional[str] = None) -> None:
    payload: Dict[str, Any] = {"chat_id": int(chat_id), "video": str(video_url or "").strip()}
    if caption:
        payload["caption"] = str(caption)
    try:
        await _tg_post("sendVideo", json_payload=payload)
    except Exception:
        await _tg_send_message(chat_id, f"✅ Видео готово: {video_url}")


async def _download_bytes(url: str, *, timeout: float = 300.0) -> bytes:
    target = str(url or "").strip()
    if not target:
        raise RuntimeError("Empty file url")
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(target)
        resp.raise_for_status()
        return resp.content


async def _download_optional_bytes(url: Optional[str], *, timeout: float = 300.0) -> Optional[bytes]:
    if not str(url or "").strip():
        return None
    return await _download_bytes(str(url), timeout=timeout)


def _workspace_upload_url(user_id: int, upload_id: str) -> str:
    row = get_workspace_upload_row(int(user_id), str(upload_id))
    if not row:
        raise RuntimeError(f"Workspace upload not found: {upload_id}")
    access = build_workspace_video_access_urls(
        storage_path=row.get("storage_path"),
        fallback_url=row.get("download_url") or row.get("video_url"),
        expires_in=3600,
    )
    url = str(access.get("download_url") or access.get("video_url") or "").strip()
    if not url:
        raise RuntimeError(f"Workspace upload URL missing: {upload_id}")
    return url


async def process_workspace_video_job(job: Dict[str, Any]) -> None:
    generation_id = str(job.get("generation_id") or "").strip()
    user_id = int(job.get("user_id") or 0)
    if not generation_id or not user_id:
        raise RuntimeError("workspace_video_run job missing generation_id/user_id")

    ww._update_workspace_generation(generation_id, {"status": "processing", "error_message": None, "updated_at": ww._utc_now_iso()})

    start_frame = await _download_optional_bytes(job.get("start_frame_url"))
    end_frame = await _download_optional_bytes(job.get("end_frame_url"))
    last_frame = await _download_optional_bytes(job.get("last_frame_url"))
    avatar_image = await _download_optional_bytes(job.get("avatar_image_url"))

    motion_video = None
    motion_video_upload_id = str(job.get("motion_video_upload_id") or "").strip()
    if motion_video_upload_id:
        motion_video = await _download_bytes(_workspace_upload_url(user_id, motion_video_upload_id), timeout=600.0)

    provider_name = str(job.get("provider") or "").strip()
    reference_image_urls_direct = [str(url or "").strip() for url in (job.get("reference_image_urls") or []) if str(url or "").strip()]
    reference_images: List[bytes] = []
    if provider_name != "google":
        for url in reference_image_urls_direct:
            reference_images.append(await _download_bytes(url))

    reference_audio_clips: List[bytes] = []
    for upload_id in job.get("reference_audio_upload_ids") or []:
        target_id = str(upload_id or "").strip()
        if not target_id:
            continue
        reference_audio_clips.append(await _download_bytes(_workspace_upload_url(user_id, target_id), timeout=600.0))

    reference_video_clips: List[bytes] = []
    for upload_id in job.get("reference_video_upload_ids") or []:
        target_id = str(upload_id or "").strip()
        if not target_id:
            continue
        reference_video_clips.append(await _download_bytes(_workspace_upload_url(user_id, target_id), timeout=600.0))

    print("[switchx worker job]", {
        "generation_id": generation_id,
        "provider": str(job.get("provider") or "").strip(),
        "alpha_mode": str(job.get("switchx_alpha_mode") or "").strip() or None,
        "select_mask_url": str(job.get("switchx_select_mask_url") or "").strip() or None,
        "reference_image_url": str(job.get("reference_image_url") or "").strip() or None,
        "source_video_upload_id": str(job.get("source_video_upload_id") or "").strip() or None,
    }, flush=True)

    await ww._run_workspace_video_job(
        generation_id=generation_id,
        user_id=user_id,
        provider=provider_name,
        model=str(job.get("model") or "").strip(),
        mode=str(job.get("mode") or "").strip(),
        prompt=str(job.get("prompt") or ""),
        duration=int(job.get("duration") or 0),
        resolution=str(job.get("resolution") or "").strip(),
        aspect_ratio=str(job.get("aspect_ratio") or "").strip() or "16:9",
        enable_audio=bool(job.get("enable_audio")),
        quality=str(job.get("quality") or "pro").strip().lower() or "pro",
        provider_mode=str(job.get("provider_mode") or "normal").strip().lower() or "normal",
        start_frame=start_frame,
        end_frame=end_frame,
        last_frame=last_frame,
        avatar_image=avatar_image,
        motion_video=motion_video,
        reference_images=reference_images,
        reference_audio_clips=reference_audio_clips,
        reference_video_clips=reference_video_clips,
        source_video_upload_id=str(job.get("source_video_upload_id") or "").strip() or None,
        reference_image_url=str(job.get("reference_image_url") or "").strip() or None,
        reference_image_urls_direct=reference_image_urls_direct if provider_name == "google" else None,
        switchx_alpha_mode=str(job.get("switchx_alpha_mode") or "").strip() or None,
        switchx_select_mask_url=str(job.get("switchx_select_mask_url") or "").strip() or None,
        charge_tokens=int(job.get("charge_tokens") or 0),
        charge_ref_id=str(job.get("charge_ref_id") or ""),
        refund_reason=str(job.get("refund_reason") or "workspace_video_refund"),
    )


async def process_workspace_switchx_ref_job(job: Dict[str, Any]) -> None:
    generation_id = str(job.get("generation_id") or "").strip()
    user_id = int(job.get("user_id") or 0)
    upload_id = str(job.get("source_video_upload_id") or "").strip()
    prompt = str(job.get("prompt") or "").strip()
    safety_level = str(job.get("safety_level") or "high").strip().lower() or "high"
    charge_tokens = int(job.get("charge_tokens") or 0)
    charge_ref_id = str(job.get("charge_ref_id") or "")
    refund_reason = str(job.get("refund_reason") or "nano_banana_pro_refund")
    if not generation_id or not user_id or not upload_id:
        raise RuntimeError("workspace_switchx_ref_run job missing generation_id/user_id/source_video_upload_id")

    try:
        ww._update_workspace_image_generation(
            generation_id,
            {"status": "processing", "error_message": None, "error_code": None, "updated_at": ww._utc_now_iso()},
        )
        source_url = _workspace_upload_url(user_id, upload_id)
        local_source_path = ""
        try:
            local_source_path, _, _ = await ww._download_video_to_tempfile(source_url)
            frame_bytes = ww.extract_first_frame_bytes(local_source_path, format="png")
        finally:
            if local_source_path:
                try:
                    os.remove(local_source_path)
                except Exception:
                    pass
        before_url = ww._upload_workspace_input_image(
            user_id,
            frame_bytes,
            filename="switchx_first_frame.png",
            slot="switchx_first_frame",
        )
        ww._update_workspace_image_generation(
            generation_id,
            {"source_image_url": before_url, "before_image_url": before_url, "updated_at": ww._utc_now_iso()},
        )
        out_bytes, ext = await ww._workspace_run_nano_banana_pro_site(
            user_id=user_id,
            prompt=prompt,
            source_image_bytes=frame_bytes,
            source_filename="switchx_first_frame.png",
            resolution=str(job.get("resolution") or "2K"),
            aspect_ratio=str(job.get("aspect_ratio") or "match_input_image"),
            safety_level=safety_level,
        )
        output_path = ww._workspace_image_output_path(user_id, ext)
        image_url = ww.upload_bytes_to_supabase(output_path, out_bytes, ww._workspace_image_content_type(ext))
        now_iso = ww._utc_now_iso()
        ww._update_workspace_image_generation(
            generation_id,
            {
                "status": "completed",
                "storage_path": output_path,
                "image_url": image_url,
                "download_url": image_url,
                "file_size_bytes": len(out_bytes or b""),
                "mime_type": ww._workspace_image_content_type(ext),
                "error_code": None,
                "error_message": None,
                "updated_at": now_iso,
                "completed_at": now_iso,
            },
        )
    except Exception as e:
        if charge_tokens > 0:
            try:
                add_tokens(user_id, charge_tokens, reason=refund_reason, ref_id=charge_ref_id or None, meta={"origin": "workspace_switchx_ref", "generation_id": generation_id, "error": str(e)[:300]})
            except TypeError:
                add_tokens(user_id, charge_tokens, reason=refund_reason)
            except Exception:
                pass
        ww._mark_workspace_image_generation_failed(generation_id, str(e), error_code="provider_error")



async def process_tg_veo_relax_video_job(job: Dict[str, Any]) -> None:
    chat_id = int(job.get("chat_id") or 0)
    user_id = int(job.get("user_id") or 0)
    mode_raw = str(job.get("mode") or "text_to_video").strip().lower()
    mode = "image_to_video" if mode_raw in {"image", "image_to_video", "i2v", "image2video"} else "text_to_video"
    prompt = str(job.get("prompt") or "")
    duration = normalize_veo31_fast_relax_duration(job.get("duration") or 8)
    resolution = normalize_veo31_fast_relax_resolution(job.get("resolution") or "1080p")
    aspect_ratio = normalize_veo31_fast_relax_aspect_ratio(job.get("aspect_ratio") or "16:9")
    charge_tokens = int(job.get("charge_tokens") or 0)
    charge_ref_id = str(job.get("charge_ref_id") or "")
    refund_reason = str(job.get("refund_reason") or "veo31_fast_relax_video_refund")

    if not chat_id or not user_id:
        raise RuntimeError("tg_veo_relax_video_run job missing chat_id/user_id")

    try:
        frame_urls = []
        start_url = str(job.get("start_frame_url") or "").strip()
        last_url = str(job.get("last_frame_url") or "").strip()
        if mode == "image_to_video":
            if not start_url:
                raise Veo31FastRelaxError("Для Veo 3.1 Fast Relax Image → Video нужен первый кадр")
            frame_urls.append(start_url)
            if last_url:
                frame_urls.append(last_url)
        video_url = await run_veo31_fast_relax(
            user_id=user_id,
            prompt=prompt,
            mode=mode,
            duration=duration,
            resolution=resolution,
            aspect_ratio=aspect_ratio,
            image_urls=frame_urls,
        )
        if not video_url:
            raise Veo31FastRelaxError("Veo 3.1 Fast Relax did not return video url")
        await _tg_send_video_url(chat_id, video_url, caption="✅ Veo 3.1 Fast Relax готов")
    except Exception as e:
        if charge_tokens > 0:
            try:
                add_tokens(
                    int(user_id),
                    int(charge_tokens),
                    reason=refund_reason,
                    ref_id=charge_ref_id or None,
                    meta={"origin": "tg_veo31_fast_relax_video", "error": str(e)[:300]},
                )
            except TypeError:
                add_tokens(int(user_id), int(charge_tokens), reason=refund_reason)
            except Exception:
                pass
        try:
            await _tg_send_message(chat_id, f"❌ Ошибка Veo 3.1 Fast Relax: {e}")
        except Exception:
            pass


async def process_tg_kling3_turbo_video_job(job: Dict[str, Any]) -> None:
    chat_id = int(job.get("chat_id") or 0)
    user_id = int(job.get("user_id") or 0)
    mode = normalize_kling3_turbo_mode(job.get("mode") or "text_to_video")
    prompt = str(job.get("prompt") or "")
    duration = normalize_kling3_turbo_duration(job.get("duration") or 5)
    resolution = normalize_kling3_turbo_resolution(job.get("resolution") or "720p")
    aspect_ratio = normalize_kling3_turbo_aspect_ratio(job.get("aspect_ratio") or "16:9")
    start_frame_url = str(job.get("start_frame_url") or "").strip()
    charge_tokens = int(job.get("charge_tokens") or 0)
    charge_ref_id = str(job.get("charge_ref_id") or "")
    refund_reason = str(job.get("refund_reason") or "kling3_turbo_refund")

    if not chat_id or not user_id:
        raise RuntimeError("tg_kling3_turbo_video_run job missing chat_id/user_id")

    try:
        if mode == "image_to_video" and not start_frame_url:
            raise RuntimeError("Для Kling 3.0 Turbo Image → Video нужен start frame")

        _task_id, _raw_task, video_url = await run_kling3_turbo_task_and_wait(
            prompt=prompt,
            duration=duration,
            resolution=resolution,
            aspect_ratio=aspect_ratio,
            mode=mode,
            image_url=start_frame_url if mode == "image_to_video" else None,
            request_id=str(job.get("job_id") or charge_ref_id or ""),
        )
        if not video_url:
            raise RuntimeError("Kling 3.0 Turbo did not return video url")
        await _tg_send_video_url(chat_id, video_url, caption="✅ Kling 3.0 Turbo готов")
    except Exception as e:
        if charge_tokens > 0:
            try:
                add_tokens(
                    int(user_id),
                    int(charge_tokens),
                    reason=refund_reason,
                    ref_id=charge_ref_id or None,
                    meta={"origin": "tg_kling3_turbo_video", "error": str(e)[:300]},
                )
            except TypeError:
                add_tokens(int(user_id), int(charge_tokens), reason=refund_reason)
            except Exception:
                pass
        try:
            await _tg_send_message(chat_id, f"❌ Ошибка Kling 3.0 Turbo: {e}")
        except Exception:
            pass


async def process_tg_grok_video_job(job: Dict[str, Any]) -> None:
    chat_id = int(job.get("chat_id") or 0)
    user_id = int(job.get("user_id") or 0)
    model = normalize_grok_model(job.get("model") or GROK_LEGACY_MODEL)
    mode = str(job.get("mode") or "text_to_video").strip().lower()
    prompt = str(job.get("prompt") or "")
    if is_grok15_model(model):
        duration = normalize_grok15_duration(job.get("duration") or 5)
        resolution = normalize_grok15_resolution(job.get("resolution") or "480p")
        aspect_ratio = normalize_grok15_aspect_ratio(job.get("aspect_ratio") or "16:9")
        provider_mode = ""
        display_name = "Grok 1.5 Preview"
    else:
        duration = normalize_grok_duration(job.get("duration") or 6)
        resolution = normalize_grok_resolution(job.get("resolution") or "480p")
        aspect_ratio = normalize_grok_aspect_ratio(job.get("aspect_ratio") or "16:9")
        provider_mode = normalize_grok_provider_mode(job.get("provider_mode") or "normal")
        display_name = "Grok"
    charge_tokens = int(job.get("charge_tokens") or 0)
    charge_ref_id = str(job.get("charge_ref_id") or "")
    refund_reason = str(job.get("refund_reason") or "grok_video_refund")

    if not chat_id or not user_id:
        raise RuntimeError("tg_grok_video_run job missing chat_id/user_id")

    try:
        video_url: Optional[str] = None
        if is_grok15_model(model):
            if mode != "image_to_video":
                raise GrokVideoError("Grok 1.5 Preview поддерживает только Image → Video")
            start_frame = await _download_optional_bytes(job.get("start_frame_url"), timeout=600.0)
            if not start_frame:
                raise GrokVideoError("Для Grok 1.5 Image → Video нужен start frame")
            video_url = await run_grok15_image_to_video(
                user_id=user_id,
                image_bytes=start_frame,
                image_url=str(job.get("start_frame_url") or "").strip() or None,
                prompt=prompt,
                duration=duration,
                resolution=resolution,
                aspect_ratio=aspect_ratio,
            )
        elif mode == "image_to_video":
            start_frame = await _download_optional_bytes(job.get("start_frame_url"), timeout=600.0)
            if not start_frame:
                raise GrokVideoError("Для Grok Image → Video нужен start frame")
            video_url = await run_grok_image_to_video(
                user_id=user_id,
                image_bytes=start_frame,
                image_url=str(job.get("start_frame_url") or "").strip() or None,
                prompt=prompt,
                duration=duration,
                resolution=resolution,
                aspect_ratio=aspect_ratio,
                provider_mode=provider_mode,
            )
        else:
            video_url = await run_grok_text_to_video(
                prompt=prompt,
                duration=duration,
                resolution=resolution,
                aspect_ratio=aspect_ratio,
                provider_mode=provider_mode,
            )
        if not video_url:
            raise GrokVideoError("Grok did not return video url")
        await _tg_send_video_url(chat_id, video_url, caption=f"✅ {display_name} готов")
    except Exception as e:
        if charge_tokens > 0:
            try:
                add_tokens(
                    int(user_id),
                    int(charge_tokens),
                    reason=refund_reason,
                    ref_id=charge_ref_id or None,
                    meta={"origin": "tg_grok_video", "model": model, "error": str(e)[:300]},
                )
            except TypeError:
                add_tokens(int(user_id), int(charge_tokens), reason=refund_reason)
            except Exception:
                pass
        try:
            await _tg_send_message(chat_id, f"❌ Ошибка {display_name}: {e}")
        except Exception:
            pass


async def process_tg_omni_flash_video_job(job: Dict[str, Any]) -> None:
    chat_id = int(job.get("chat_id") or 0)
    user_id = int(job.get("user_id") or 0)
    mode = normalize_gemini_omni_mode(job.get("mode") or "text_to_video")
    prompt = str(job.get("prompt") or "")
    duration = normalize_gemini_omni_duration(job.get("duration") or 8)
    resolution = normalize_gemini_omni_resolution(job.get("resolution") or "1080p")
    aspect_ratio = normalize_gemini_omni_aspect_ratio(job.get("aspect_ratio") or "16:9")
    charge_tokens = int(job.get("charge_tokens") or 0)
    charge_ref_id = str(job.get("charge_ref_id") or "")
    refund_reason = str(job.get("refund_reason") or "gemini_omni_video_refund")

    if not chat_id or not user_id:
        raise RuntimeError("tg_omni_flash_video_run job missing chat_id/user_id")

    try:
        refs = [str(url or "").strip() for url in (job.get("reference_image_urls") or []) if str(url or "").strip()]
        source_video_url = ""
        source_video_end = None

        if mode == "video_edit":
            source_video_upload_id = str(job.get("source_video_upload_id") or "").strip()
            source_duration = 0.0

            if source_video_upload_id:
                upload_row = get_workspace_upload_row(user_id, source_video_upload_id)
                if not upload_row:
                    raise GeminiOmniVideoError("Исходное видео Google Omni Flash не найдено в workspace uploads")
                if str(upload_row.get("file_type") or "").strip().lower() != "video":
                    raise GeminiOmniVideoError("Исходный файл Google Omni Flash не является видео")

                source_duration = float(upload_row.get("duration_sec") or 0.0)
                access = build_workspace_video_access_urls(
                    storage_path=str(upload_row.get("storage_path") or "").strip(),
                    fallback_url=str(upload_row.get("download_url") or upload_row.get("video_url") or upload_row.get("signed_url") or "").strip() or None,
                    expires_in=3600,
                )
                source_video_url = str(access.get("download_url") or access.get("video_url") or "").strip()
            else:
                # Backward compatibility for already queued v3 jobs. New Telegram jobs must pass source_video_upload_id.
                source_video_url = str(job.get("source_video_url") or "").strip()

            if not source_video_url:
                raise GeminiOmniVideoError("Для Google Omni Flash Video Edit нужно исходное видео")

            end_seed = job.get("source_video_end") or source_duration or KIE_OMNI_VIDEO_EDIT_MAX_DURATION_SEC
            try:
                source_video_end = int(max(1, min(float(KIE_OMNI_VIDEO_EDIT_MAX_DURATION_SEC), float(end_seed))))
            except Exception:
                source_video_end = int(KIE_OMNI_VIDEO_EDIT_MAX_DURATION_SEC)

        if mode == "image_to_video" and not refs:
            raise GeminiOmniVideoError("Для Google Omni Flash нужен хотя бы один reference image")
        if mode == "video_edit" and len(refs) > 5:
            raise GeminiOmniVideoError("Для Google Omni Flash Video Edit доступно максимум 5 фото-референсов")
        video_url = await run_gemini_omni_video(
            user_id=user_id,
            prompt=prompt,
            duration=duration,
            resolution=resolution,
            aspect_ratio=aspect_ratio,
            reference_image_urls=refs if mode in {"image_to_video", "video_edit"} else None,
            source_video_url=source_video_url if mode == "video_edit" else None,
            source_video_start=0,
            source_video_end=source_video_end if mode == "video_edit" else None,
        )
        if not video_url:
            raise GeminiOmniVideoError("Google Omni Flash did not return video url")
        if resolution == "4k":
            await _tg_send_message(chat_id, f"✅ Google Omni Flash готов:\n{video_url}")
        else:
            await _tg_send_video_url(chat_id, video_url, caption="✅ Google Omni Flash готов")
    except Exception as e:
        if charge_tokens > 0:
            try:
                add_tokens(
                    int(user_id),
                    int(charge_tokens),
                    reason=refund_reason,
                    ref_id=charge_ref_id or None,
                    meta={"origin": "tg_omni_flash_video", "error": str(e)[:300]},
                )
            except TypeError:
                add_tokens(int(user_id), int(charge_tokens), reason=refund_reason)
            except Exception:
                pass
        try:
            await _tg_send_message(chat_id, f"❌ Ошибка Google Omni Flash: {e}")
        except Exception:
            pass


async def process_workspace_music_job(job: Dict[str, Any]) -> None:
    generation_id = str(job.get("generation_id") or "").strip()
    user_id = int(job.get("user_id") or 0)
    payload = ww.MusicGenerateIn(**dict(job.get("payload") or {}))
    await ww._run_workspace_music_job(
        generation_id=generation_id,
        user_id=user_id,
        payload=payload,
        charge_tokens=int(job.get("charge_tokens") or 0),
        charge_ref_id=str(job.get("charge_ref_id") or ""),
    )


async def process_workspace_tts_job(job: Dict[str, Any]) -> None:
    generation_id = str(job.get("generation_id") or "").strip()
    user_id = int(job.get("user_id") or 0)
    payload = ww.TTSGenerateIn(**dict(job.get("payload") or {}))
    if not generation_id or not user_id:
        raise RuntimeError("workspace_tts_run job missing generation_id/user_id")

    try:
        ww._update_workspace_voice_generation(
            generation_id,
            {
                "status": "processing",
                "error_message": None,
                "error_code": None,
                "updated_at": ww._utc_now_iso(),
            },
        )
        language_code = ww._workspace_tts_language_code(payload.language_code)
        voice_settings = ww._workspace_tts_voice_settings(payload)
        tts = ww._get_tts()
        audio_bytes = await tts.tts(
            text=payload.text,
            voice_id=payload.voice_id,
            model_id=payload.model_id,
            output_format=payload.output_format,
            language_code=language_code,
            voice_settings=voice_settings,
        )
        ext = ww._workspace_voice_ext(payload.output_format)
        mime_type = ww._workspace_voice_content_type(payload.output_format)
        output_path = ww._workspace_voice_output_path(user_id, ext)
        audio_url = ww.upload_bytes_to_supabase(output_path, audio_bytes, mime_type)
        done_iso = ww._utc_now_iso()
        ww._update_workspace_voice_generation(
            generation_id,
            {
                "status": "completed",
                "storage_path": output_path,
                "audio_url": audio_url,
                "download_url": audio_url,
                "file_size_bytes": len(audio_bytes or b""),
                "mime_type": mime_type,
                "error_code": None,
                "error_message": None,
                "updated_at": done_iso,
                "completed_at": done_iso,
            },
        )
    except Exception as e:
        ww._mark_workspace_voice_generation_failed(generation_id, str(e), error_code="provider_error")


async def _wait_for_midjourney_job(job_id: str, *, poll_interval_sec: float = 3.0, timeout_sec: float = 900.0) -> Dict[str, Any]:
    loop = asyncio.get_running_loop()
    started = loop.time()
    while True:
        payload = await get_midjourney_job(job_id)
        status = str(payload.get("status") or "").strip().lower()
        if status in {"completed", "failed"}:
            return payload
        if (loop.time() - started) >= timeout_sec:
            raise LegnextMidjourneyError(f"Midjourney polling timeout for job {job_id}")
        await asyncio.sleep(poll_interval_sec)


async def _process_midjourney_workspace_image_job(job: Dict[str, Any]) -> Dict[str, Any]:
    action = str(job.get("mj_action") or "generate").strip().lower() or "generate"
    run_prompt = str(job.get("run_prompt") or "").strip()
    source_task_id = str(job.get("source_task_id") or "").strip()
    variation_type_name = str(job.get("variation_type") or "subtle").strip().lower()
    selected_image_no = job.get("selected_image_no")

    if action == "generate":
        created = await create_midjourney_diffusion(text=run_prompt)
    elif action == "reroll":
        if not source_task_id:
            raise LegnextMidjourneyError("Midjourney reroll requires source_task_id")
        created = await create_midjourney_reroll(job_id=source_task_id)
    elif action == "variation":
        if not source_task_id:
            raise LegnextMidjourneyError("Midjourney variation requires source_task_id")
        if selected_image_no is None:
            raise LegnextMidjourneyError("Midjourney variation requires selected_image_no")
        variation_type = 1 if variation_type_name == "strong" else 0
        created = await create_midjourney_variation(
            job_id=source_task_id,
            image_no=int(selected_image_no),
            variation_type=variation_type,
        )
    else:
        raise LegnextMidjourneyError(f"Unsupported Midjourney action: {action}")

    provider_task_id = str(created.get("job_id") or "").strip()
    if not provider_task_id:
        raise LegnextMidjourneyError("Midjourney API did not return job_id")

    final_payload = await _wait_for_midjourney_job(provider_task_id)
    status = str(final_payload.get("status") or "").strip().lower()
    if status != "completed":
        error = final_payload.get("error") or {}
        message = str(error.get("message") or error.get("raw_message") or final_payload.get("detail") or f"Midjourney task finished with status {status}")
        raise LegnextMidjourneyError(message)

    return {
        "provider_task_id": provider_task_id,
        "final_payload": final_payload,
    }



async def process_workspace_image_job(job: Dict[str, Any]) -> None:
    generation_id = str(job.get("generation_id") or "").strip()
    user_id = int(job.get("user_id") or 0)
    provider = str(job.get("provider") or "").strip().lower()
    model = str(job.get("model") or "").strip()
    mode = str(job.get("mode") or "").strip().lower()
    prompt = str(job.get("prompt") or "")
    run_prompt = str(job.get("run_prompt") or "")
    resolution = str(job.get("resolution") or "2K").strip().upper() or "2K"
    aspect_ratio = str(job.get("aspect_ratio") or "match_input_image").strip() or "match_input_image"
    safety_level = str(job.get("safety_level") or "high").strip().lower() or "high"
    preset_slug = str(job.get("preset_slug") or "standard").strip().lower() or "standard"
    refund_reason = str(job.get("refund_reason") or "workspace_image_refund")
    charge_tokens = int(job.get("charge_tokens") or 0)
    charge_ref_id = str(job.get("charge_ref_id") or "")

    source_image_urls = [str(item or "").strip() for item in (job.get("source_image_urls") or []) if str(item or "").strip()]
    source_image = None if (provider in {"nano_banana_pro_new", "gpt_image_2", "gpt_image_2_kie"} and source_image_urls) else await _download_optional_bytes(job.get("source_image_url"))
    base_image = await _download_optional_bytes(job.get("base_image_url"))

    ww._update_workspace_image_generation(
        generation_id,
        {
            "status": "processing",
            "error_code": None,
            "error_message": None,
            "updated_at": ww._utc_now_iso(),
        },
    )

    try:
        before_image_url: Optional[str] = None
        after_image_url: Optional[str] = None
        compare_mode = False

        if provider == "midjourney":
            mj_result = await _process_midjourney_workspace_image_job(job)
            final_payload = mj_result["final_payload"]
            output = final_payload.get("output") or {}
            available_actions = output.get("available_actions") or {}
            provider_task_id = str(mj_result.get("provider_task_id") or "").strip()
            image_urls = [str(item or "").strip() for item in (output.get("image_urls") or []) if str(item or "").strip()]
            single_image_url = str(output.get("image_url") or "").strip()
            if not image_urls and single_image_url:
                image_urls = [single_image_url]
            if not image_urls:
                raise LegnextMidjourneyError("Midjourney completed without image URLs")

            uploaded_urls: List[str] = []
            storage_paths: List[str] = []
            total_size = 0
            mime_type = "image/jpeg"
            for remote_url in image_urls:
                out_bytes, ext = await ww._download_workspace_image_bytes(remote_url)
                total_size += len(out_bytes or b"")
                mime_type = ww._workspace_image_content_type(ext)
                output_path = ww._workspace_image_output_path(user_id, ext)
                uploaded_url = ww.upload_bytes_to_supabase(output_path, out_bytes, mime_type)
                uploaded_urls.append(uploaded_url)
                storage_paths.append(output_path)

            image_url = uploaded_urls[0]
            after_image_url = image_url
            now_iso = ww._utc_now_iso()
            ww._update_workspace_image_generation(
                generation_id,
                {
                    "status": "completed",
                    "provider_task_id": provider_task_id,
                    "storage_path": storage_paths[0] if storage_paths else None,
                    "storage_paths_json": storage_paths,
                    "image_url": image_url,
                    "image_urls_json": uploaded_urls,
                    "download_url": image_url,
                    "file_size_bytes": total_size,
                    "mime_type": mime_type,
                    "error_code": None,
                    "error_message": None,
                    "after_image_url": after_image_url,
                    "available_actions_json": available_actions,
                    "updated_at": now_iso,
                    "completed_at": now_iso,
                },
            )
            return

        if provider == "nano_banana":
            out_bytes, ext = await ww.run_nano_banana(source_image, run_prompt, output_format="jpg", aspect_ratio=aspect_ratio)
        elif provider == "nano_banana_2":
            out_bytes, ext = await handle_nano_banana_2(
                source_image,
                run_prompt,
                resolution=resolution,
                output_format="jpg",
                aspect_ratio=aspect_ratio,
                source_image_url=str(job.get("source_image_url") or "").strip() or None,
            )
        elif provider == "seedream":
            from main import ark_edit_image, ark_text_to_image

            seedream_mode = str(job.get("mode") or "").strip().lower()
            if seedream_mode in {"text_to_image", "t2i"}:
                out_bytes = await ark_text_to_image(run_prompt, size=ww._workspace_ark_size(resolution))
            else:
                input_bytes = source_image
                source_filename = job.get("source_filename")
                upload_slot = "seedream_source_worker"
                if seedream_mode in {"image_to_image", "i2i"}:
                    input_bytes = ww._compose_workspace_pair_image(base_image, source_image)
                    aspect_ratio = "match_input_image"
                    source_filename = "seedream_pair.png"
                    upload_slot = "seedream_pair_worker"
                source_url = ww._upload_workspace_input_image(
                    user_id,
                    input_bytes or b"",
                    filename=source_filename,
                    slot=upload_slot,
                )
                out_bytes = await ark_edit_image(
                    source_image_bytes=b"",
                    prompt=run_prompt,
                    size=ww._workspace_ark_size(resolution),
                    source_image_url=source_url,
                )
            ext = ww._workspace_detect_image_ext(out_bytes, default="jpg")
        elif provider == "photosession":
            from main import ark_edit_image

            source_url = ww._upload_workspace_input_image(
                user_id,
                source_image or b"",
                filename=job.get("source_filename"),
                slot="photosession_source_worker",
            )
            out_bytes = await ark_edit_image(
                source_image_bytes=b"",
                prompt=run_prompt,
                size=ww._workspace_ark_size(resolution),
                source_image_url=source_url,
            )
            ext = ww._workspace_detect_image_ext(out_bytes, default="jpg")
        elif provider == "text_to_image":
            from main import ark_text_to_image

            out_bytes = await ark_text_to_image(run_prompt, size=ww._workspace_ark_size(resolution))
            ext = ww._workspace_detect_image_ext(out_bytes, default="jpg")
        elif provider == "gpt_image_2":
            from main import openai_edit_image_v2, openai_generate_image_v2

            size = ww._workspace_gpt_image_2_size(job.get("aspect_ratio"), job.get("mode"))
            gpt_mode = str(job.get("mode") or "").strip().lower()
            if gpt_mode == "image_to_image":
                gpt_source_images = []
                for source_url in source_image_urls[:4]:
                    try:
                        gpt_source_images.append(await _download_bytes(source_url))
                    except Exception:
                        continue
                if not gpt_source_images and source_image:
                    gpt_source_images = [source_image]
                if not gpt_source_images:
                    raise RuntimeError("GPT Image 2.0 Image→Image requires source image")
                out_bytes = await openai_edit_image_v2(gpt_source_images, run_prompt, size=size, mask_png_bytes=None)
            else:
                out_bytes = await openai_generate_image_v2(prompt=run_prompt, size=size)
            ext = ww._workspace_detect_image_ext(out_bytes, default="png")
        elif provider == "gpt_image_2_kie":
            gpt_mode = str(job.get("mode") or "text_to_image").strip().lower()
            if gpt_mode == "image_to_image" and not source_image_urls:
                raise RuntimeError("Gpt Image 2 Image→Image requires source image")
            out_bytes, ext = await handle_gpt_image_2_kie(
                run_prompt,
                mode=gpt_mode,
                source_image_urls=source_image_urls[:16],
                resolution=resolution,
                aspect_ratio=aspect_ratio,
            )
        elif provider == "topaz_photo":
            preset_settings = ww.get_photo_preset_settings(preset_slug)
            source_url = await ww._upload_workspace_topaz_input_image(
                user_id,
                source_image or b"",
                filename=job.get("source_filename"),
                slot=f"topaz_{preset_slug}_worker",
            )
            topaz_result = await ww._run_workspace_topaz_with_retry(
                ww.TopazImageParams(
                    image_url=source_url,
                    enhance_model=str(preset_settings.get("enhance_model") or "Standard V2"),
                    upscale_factor=str(preset_settings.get("upscale_factor") or "2x"),
                    output_format=str(preset_settings.get("output_format") or "jpg"),
                    subject_detection=str(preset_settings.get("subject_detection") or "Foreground"),
                    face_enhancement=bool(preset_settings.get("face_enhancement")),
                    face_enhancement_creativity=float(preset_settings.get("face_enhancement_creativity") or 0.0),
                    face_enhancement_strength=float(preset_settings.get("face_enhancement_strength") or 0.8),
                )
            )
            out_bytes, ext = await ww._download_workspace_image_bytes(topaz_result.output_url)
            before_image_url = source_url
            compare_mode = True
        elif provider in {"two_images", "nano_banana_pro"}:
            input_image = source_image
            if provider == "two_images":
                input_image = ww._compose_workspace_pair_image(base_image, source_image)
                aspect_ratio = "match_input_image"
            out_bytes, ext = await ww._workspace_run_nano_banana_pro_site(
                user_id=user_id,
                prompt=run_prompt,
                source_image_bytes=input_image,
                source_filename=job.get("source_filename"),
                resolution=resolution,
                aspect_ratio=aspect_ratio,
                safety_level=safety_level,
            )
        elif provider == "nano_banana_pro_new":
            out_bytes, ext = await ww._workspace_run_nano_banana_pro_new_site(
                user_id=user_id,
                prompt=run_prompt,
                source_image_bytes=source_image,
                source_filename=job.get("source_filename"),
                source_image_urls=source_image_urls,
                resolution=resolution,
                aspect_ratio=aspect_ratio,
            )
        else:
            raise RuntimeError(f"Unsupported workspace image provider: {provider}")

        output_path = ww._workspace_image_output_path(user_id, ext)
        image_url = ww.upload_bytes_to_supabase(output_path, out_bytes, ww._workspace_image_content_type(ext))
        after_image_url = image_url
        now_iso = ww._utc_now_iso()
        ww._update_workspace_image_generation(
            generation_id,
            {
                "status": "completed",
                "storage_path": output_path,
                "image_url": image_url,
                "download_url": image_url,
                "file_size_bytes": len(out_bytes or b""),
                "mime_type": ww._workspace_image_content_type(ext),
                "error_code": None,
                "error_message": None,
                "preset_slug": preset_slug if provider == "topaz_photo" else None,
                "source_image_url": before_image_url if provider == "topaz_photo" else None,
                "before_image_url": before_image_url if provider == "topaz_photo" else None,
                "after_image_url": after_image_url if provider == "topaz_photo" else image_url,
                "compare_mode": compare_mode if provider == "topaz_photo" else False,
                "updated_at": now_iso,
                "completed_at": now_iso,
            },
        )
    except Exception as e:
        if charge_tokens > 0:
            try:
                try:
                    ww.add_tokens(user_id, charge_tokens, reason=refund_reason, ref_id=charge_ref_id or ww.uuid4().hex, meta={"origin": "workspace_image", "error": str(e)[:300], "generation_id": generation_id})
                except TypeError:
                    ww.add_tokens(user_id, charge_tokens, reason=refund_reason)
            except Exception:
                pass
        ww._mark_workspace_image_generation_failed(generation_id, str(e), error_code="provider_error")
