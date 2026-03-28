from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import httpx

from app.routers import web_workspace_api as ww
from app.services.video_editor_service import (
    build_workspace_video_access_urls,
    get_workspace_upload_row,
)


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

    reference_images: List[bytes] = []
    for url in job.get("reference_image_urls") or []:
        target = str(url or "").strip()
        if not target:
            continue
        reference_images.append(await _download_bytes(target))

    await ww._run_workspace_video_job(
        generation_id=generation_id,
        user_id=user_id,
        provider=str(job.get("provider") or "").strip(),
        model=str(job.get("model") or "").strip(),
        mode=str(job.get("mode") or "").strip(),
        prompt=str(job.get("prompt") or ""),
        duration=int(job.get("duration") or 0),
        resolution=str(job.get("resolution") or "").strip(),
        aspect_ratio=str(job.get("aspect_ratio") or "").strip() or "16:9",
        enable_audio=bool(job.get("enable_audio")),
        quality=str(job.get("quality") or "pro").strip().lower() or "pro",
        start_frame=start_frame,
        end_frame=end_frame,
        last_frame=last_frame,
        avatar_image=avatar_image,
        motion_video=motion_video,
        reference_images=reference_images,
        charge_tokens=int(job.get("charge_tokens") or 0),
        charge_ref_id=str(job.get("charge_ref_id") or ""),
        refund_reason=str(job.get("refund_reason") or "workspace_video_refund"),
    )


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

    source_image = await _download_optional_bytes(job.get("source_image_url"))
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

        if provider == "nano_banana":
            out_bytes, ext = await ww.run_nano_banana(source_image, run_prompt, output_format="jpg", aspect_ratio=aspect_ratio)
            engine = "nano_banana"
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
            engine = "modelark_seedream"
        elif provider == "text_to_image":
            from main import ark_text_to_image

            out_bytes = await ark_text_to_image(run_prompt, size=ww._workspace_ark_size(resolution))
            ext = ww._workspace_detect_image_ext(out_bytes, default="jpg")
            engine = "modelark_seedream"
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
            engine = "topaz_photo_replicate"
            before_image_url = source_url
            compare_mode = True
        else:
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
            engine = "nano_banana_pro"

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
