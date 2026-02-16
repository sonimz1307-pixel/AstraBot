import time
from typing import Any, Dict, Callable, Awaitable, Optional, List

from kling3_pricing import calculate_kling3_price
from kling3_runner import run_kling3_task_and_wait, Kling3RunnerError
from billing_db import ensure_user_row, get_balance, add_tokens


def _friendly_kling3_error(err: Exception) -> str:
    msg = (str(err) or "").strip().lower()

    # Common PiAPI/Kling runner patterns
    if "timeout" in msg:
        return "⚠️ Сервер Kling долго отвечает. Попробуй ещё раз через пару минут."
    if "task failed" in msg or "failed" in msg:
        return "⚠️ Сервер Kling сейчас перегружен или временно недоступен. Попробуй ещё раз через пару минут."
    if "rate" in msg and "limit" in msg:
        return "⚠️ Лимит запросов. Попробуй ещё раз через пару минут."
    if "supabase upload failed" in msg:
        return "⚠️ Не удалось загрузить кадр (хранилище). Попробуй ещё раз или пришли другое фото."
    return f"⚠️ Не получилось выполнить генерацию. Попробуй ещё раз через пару минут.\n(детали: {str(err)})"


async def handle_kling3_wait_prompt(
    *,
    chat_id: int,
    user_id: int,
    incoming_text: str,
    st: Dict[str, Any],
    deps: Dict[str, Any],
) -> bool:
    """Handle Kling PRO 3.0 prompt step.

    Expects st['kling3_settings'] prepared by WebApp/main.py.
    Supports:
    - text->video
    - image->video (start_image_bytes, optional end_image_bytes)
    - multi_shots (list of {prompt,duration})
    """

    if st.get("mode") != "kling3_wait_prompt":
        return False

    # ignore navigation/menu text while waiting prompt
    if deps.get("_is_nav_or_menu_text") and deps["_is_nav_or_menu_text"](incoming_text):
        return True

    text = (incoming_text or "").strip()
    if not text:
        return True

    tg_send_message: Callable[[int, str], Awaitable[Any]] = deps["tg_send_message"]
    _main_menu_for = deps["_main_menu_for"]
    _set_mode = deps["_set_mode"]
    _now = deps["_now"]
    sb_clear_user_state = deps["sb_clear_user_state"]

    settings = st.get("kling3_settings") or {}

    resolution = str(settings.get("resolution") or "720")
    enable_audio = bool(settings.get("enable_audio"))
    duration = int(settings.get("duration") or 5)
    aspect_ratio = str(settings.get("aspect_ratio") or "16:9")

    gen_mode = str(settings.get("gen_mode") or settings.get("flow") or settings.get("mode") or "t2v").lower().strip()

    # нормализация синонимов из WebApp
    if gen_mode in ("image_to_video", "image2video", "image->video", "img2vid", "img2video"):
        gen_mode = "i2v"
    elif gen_mode in ("multi_shots", "multishots", "multi-shot", "multi_shot"):
        gen_mode = "multishot"

    if gen_mode not in ("t2v", "i2v", "multishot"):
        gen_mode = "t2v"

    # HARD GUARD: в i2v без 1-го кадра не запускаем вообще
    if gen_mode == "i2v" and not settings.get("start_image_bytes"):
        await tg_send_message(
            chat_id,
            "❗Сначала пришли фото (1-й кадр).\n"
            "Потом (опционально) ещё фото (последний кадр).\n"
            "И только затем — промт.",
            reply_markup=_main_menu_for(user_id),
        )
        return True

    flow = gen_mode  # backward compat
    prefer_multi_shots = bool(settings.get("prefer_multi_shots"))

    # image bytes (optional)
    start_image_bytes: Optional[bytes] = settings.get("start_image_bytes")
    end_image_bytes: Optional[bytes] = settings.get("end_image_bytes")

    # multi-shots
    multi_shots = settings.get("multi_shots") or None
    if isinstance(multi_shots, list):
        ms_clean: List[Dict[str, Any]] = []
        for it in multi_shots:
            if not isinstance(it, dict):
                continue
            p = (it.get("prompt") or "").strip()
            if not p:
                continue
            try:
                d = int(it.get("duration") or 3)
            except Exception:
                d = 3
            ms_clean.append({"prompt": p, "duration": d})
        multi_shots = ms_clean
    else:
        multi_shots = None

    # 1) Billing duration:
    # - for multi-shots: sum durations
    # - else: regular duration
    bill_seconds = duration
    if multi_shots:
        try:
            bill_seconds = int(sum(int(x.get("duration") or 0) for x in multi_shots))
        except Exception:
            bill_seconds = duration

    # 2) token calc
    try:
        tokens_required = calculate_kling3_price(resolution, enable_audio, bill_seconds)
    except Exception as e:
        await tg_send_message(
            chat_id,
            f"❌ Ошибка настроек Kling 3.0: {e}",
            reply_markup=_main_menu_for(user_id),
        )
        _set_mode(chat_id, user_id, "chat")
        st.pop("kling3_settings", None)
        st["ts"] = _now()
        sb_clear_user_state(user_id)
        return True

    # 3) balance check
    ensure_user_row(user_id)
    bal = get_balance(user_id) or 0
    if bal < tokens_required:
        await tg_send_message(
            chat_id,
            f"❌ Недостаточно токенов.\nНужно: {tokens_required}\nБаланс: {bal}",
            reply_markup=_main_menu_for(user_id),
        )
        return True

    # 4) charge
    ref_id = f"kling3_{user_id}_{int(time.time() * 1000)}"
    add_tokens(
        user_id,
        -tokens_required,
        reason="kling3_create",
        ref_id=ref_id,
        meta={
            "bill_seconds": bill_seconds,
            "duration": duration,
            "resolution": resolution,
            "enable_audio": enable_audio,
            "aspect_ratio": aspect_ratio,
            "flow": flow,
            "multi_shots": bool(multi_shots),
            "has_start_image": bool(start_image_bytes),
            "has_end_image": bool(end_image_bytes),
        },
    )

    await tg_send_message(chat_id, "⏳ Генерирую Kling PRO 3.0…")

    try:
        task_id, final_task, video_url = await run_kling3_task_and_wait(
            prompt=text,
            duration=duration,
            resolution=resolution,
            enable_audio=enable_audio,
            aspect_ratio=aspect_ratio,
            prefer_multi_shots=prefer_multi_shots,
            multi_shots=multi_shots,
            start_image_bytes=start_image_bytes,
            end_image_bytes=end_image_bytes,
            poll_interval_sec=deps.get("poll_interval_sec", 2.0),
            timeout_sec=deps.get("timeout_sec", 1200),
        )

        if not video_url:
            await tg_send_message(
                chat_id,
                f"✅ Готово, но PiAPI не вернул ссылку на видео.\nTask: {task_id}",
                reply_markup=_main_menu_for(user_id),
            )
        else:
            await tg_send_message(
                chat_id,
                f"✅ Kling PRO 3.0 готово!\n🎬 MP4: {video_url}",
                reply_markup=_main_menu_for(user_id),
            )

    except (Kling3RunnerError, Exception) as e:
        # Refund on error
        try:
            add_tokens(
                user_id,
                tokens_required,
                reason="kling3_refund",
                ref_id=ref_id,
                meta={"error": str(e)},
            )
        except Exception:
            pass

        await tg_send_message(
            chat_id,
            _friendly_kling3_error(e),
            reply_markup=_main_menu_for(user_id),
        )

    # 5) cleanup
    _set_mode(chat_id, user_id, "chat")
    st.pop("kling3_settings", None)
    st["ts"] = _now()
    sb_clear_user_state(user_id)
    return True
