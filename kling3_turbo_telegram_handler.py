from __future__ import annotations

import time
from typing import Any, Dict, Optional
from uuid import uuid4

from billing_db import add_tokens, get_balance
from queue_redis import enqueue_job
from kling3_turbo_kie import (
    KLING3_TURBO_DISPLAY_NAME,
    calculate_kling3_turbo_price,
    normalize_kling3_turbo_aspect_ratio,
    normalize_kling3_turbo_duration,
    normalize_kling3_turbo_mode,
    normalize_kling3_turbo_resolution,
    upload_kling3_turbo_input_bytes,
)


async def handle_kling3_turbo_wait_prompt(
    *,
    chat_id: int,
    user_id: int,
    incoming_text: str,
    st: Dict[str, Any],
    deps: Dict[str, Any],
) -> bool:
    current_mode = str(st.get("mode") or "")
    if current_mode != "kling3_turbo_wait_prompt":
        return False

    tg_send_message = deps["tg_send_message"]
    _main_menu_for = deps["_main_menu_for"]
    _is_nav_or_menu_text = deps["_is_nav_or_menu_text"]
    _set_mode = deps["_set_mode"]
    _now = deps["_now"]
    sb_clear_user_state = deps["sb_clear_user_state"]
    queue_name = str(deps.get("queue_name") or "workspace_media")

    text = str(incoming_text or "").strip()
    if not text or _is_nav_or_menu_text(text):
        return False

    settings = dict(st.get("kling3_turbo_settings") or {})
    gen_mode = normalize_kling3_turbo_mode(settings.get("gen_mode") or settings.get("mode") or "text_to_video")
    resolution = normalize_kling3_turbo_resolution(settings.get("resolution") or "720p")
    duration = normalize_kling3_turbo_duration(settings.get("duration") or 5)
    aspect_ratio = normalize_kling3_turbo_aspect_ratio(settings.get("aspect_ratio") or "16:9")

    start_image_bytes: Optional[bytes] = settings.get("start_image_bytes")
    start_image_url: Optional[str] = str(settings.get("start_image_url") or "").strip() or None
    if gen_mode == "image_to_video" and not (start_image_bytes or start_image_url):
        await tg_send_message(
            chat_id,
            f"❗Для {KLING3_TURBO_DISPLAY_NAME} Image → Video сначала пришли стартовое фото.",
            reply_markup=_main_menu_for(user_id),
        )
        return True

    tokens_required = int(calculate_kling3_turbo_price(resolution, duration))
    balance = int(get_balance(user_id) or 0)
    if balance < tokens_required:
        await tg_send_message(
            chat_id,
            f"❌ Недостаточно токенов. Нужно {tokens_required}, на балансе {balance}.",
            reply_markup=_main_menu_for(user_id),
        )
        return True

    if gen_mode == "image_to_video" and start_image_bytes and not start_image_url:
        try:
            start_image_url = upload_kling3_turbo_input_bytes(
                start_image_bytes,
                filename="telegram_start_frame.jpg",
                prefix="kling3-turbo/tg-frames",
            )
        except Exception as exc:
            await tg_send_message(chat_id, f"⚠️ Не удалось загрузить фото для Kling 3.0 Turbo: {exc}", reply_markup=_main_menu_for(user_id))
            return True

    ref_id = f"kling3_turbo_{user_id}_{int(time.time() * 1000)}"
    try:
        try:
            add_tokens(
                user_id,
                -tokens_required,
                reason="kling3_turbo_create",
                ref_id=ref_id,
                meta={
                    "origin": "telegram",
                    "model": KLING3_TURBO_DISPLAY_NAME,
                    "provider_model": "kling-3.0-turbo",
                    "generation_mode": gen_mode,
                    "duration": duration,
                    "resolution": resolution,
                    "aspect_ratio": aspect_ratio,
                },
            )
        except TypeError:
            add_tokens(user_id, -tokens_required, reason="kling3_turbo_create")
    except Exception as exc:
        await tg_send_message(chat_id, f"❌ Не удалось списать токены: {exc}", reply_markup=_main_menu_for(user_id))
        return True

    job = {
        "job_id": uuid4().hex,
        "kind": "tg_kling3_turbo_video_run",
        "chat_id": int(chat_id),
        "user_id": int(user_id),
        "provider": "kling",
        "model": "kling-3.0-turbo",
        "mode": gen_mode,
        "prompt": text,
        "duration": duration,
        "resolution": resolution,
        "aspect_ratio": aspect_ratio,
        "start_frame_url": start_image_url if gen_mode == "image_to_video" else None,
        "charge_tokens": int(tokens_required),
        "charge_ref_id": ref_id,
        "refund_reason": "kling3_turbo_refund",
        "origin": "telegram",
    }

    try:
        await enqueue_job(job, queue_name=queue_name)
    except Exception as exc:
        try:
            add_tokens(user_id, tokens_required, reason="kling3_turbo_refund", ref_id=ref_id, meta={"stage": "enqueue_failed", "error": str(exc)[:300]})
        except TypeError:
            add_tokens(user_id, tokens_required, reason="kling3_turbo_refund")
        except Exception:
            pass
        await tg_send_message(chat_id, f"❌ Не удалось поставить Kling 3.0 Turbo в очередь: {exc}\nТокены возвращены.", reply_markup=_main_menu_for(user_id))
        return True

    st.pop("kling3_turbo_settings", None)
    _set_mode(chat_id, user_id, "chat")
    st["ts"] = _now()
    try:
        sb_clear_user_state(user_id)
    except Exception:
        pass

    await tg_send_message(
        chat_id,
        f"⏳ {KLING3_TURBO_DISPLAY_NAME} поставлен в очередь.\nРежим: {gen_mode} • {resolution} • {duration} сек • {tokens_required} ток.",
        reply_markup=_main_menu_for(user_id),
    )
    return True
