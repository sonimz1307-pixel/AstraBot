import time
from typing import Any, Dict, Callable, Optional, Awaitable, Tuple

from kling3_pricing import calculate_kling3_price
from kling3_runner import run_kling3_task_and_wait, Kling3RunnerError
from billing_db import ensure_user_row, get_balance, add_tokens


async def handle_kling3_wait_prompt(
    *,
    chat_id: int,
    user_id: int,
    incoming_text: str,
    st: Dict[str, Any],
    deps: Dict[str, Any],
) -> bool:
    """Handle Telegram text when user is in mode 'kling3_wait_prompt'.

    Returns True if handled (caller should stop further processing).
    deps must provide:
      - tg_send_message(chat_id, text, reply_markup=...)
      - _main_menu_for(user_id)
      - _is_nav_or_menu_text(text) -> bool
      - _set_mode(chat_id, user_id, mode)
      - _now() -> any
      - sb_clear_user_state(user_id)
    Optional:
      - poll_interval_sec (float)
      - timeout_sec (int)
    """
    mode = st.get("mode")
    if mode != "kling3_wait_prompt":
        return False

    tg_send_message: Callable[..., Awaitable[Any]] = deps["tg_send_message"]
    _main_menu_for: Callable[[int], Any] = deps["_main_menu_for"]
    _is_nav_or_menu_text: Callable[[str], bool] = deps["_is_nav_or_menu_text"]
    _set_mode: Callable[[int, int, str], Any] = deps["_set_mode"]
    _now: Callable[[], Any] = deps["_now"]
    sb_clear_user_state: Callable[[int], Any] = deps["sb_clear_user_state"]

    text = (incoming_text or "").strip()
    if not text:
        await tg_send_message(chat_id, "Пришли текст (промпт) для Kling PRO 3.0.", reply_markup=_main_menu_for(user_id))
        return True

    # Menu/navigation messages should exit the flow cleanly
    if _is_nav_or_menu_text(text):
        _set_mode(chat_id, user_id, "chat")
        st.pop("kling3_settings", None)
        st["ts"] = _now()
        sb_clear_user_state(user_id)
        await tg_send_message(chat_id, "Главное меню.", reply_markup=_main_menu_for(user_id))
        return True

    settings = st.get("kling3_settings") or {}
    resolution = str(settings.get("resolution") or "720")
    enable_audio = bool(settings.get("enable_audio"))
    duration = int(settings.get("duration") or 5)
    aspect_ratio = str(settings.get("aspect_ratio") or "16:9")

    # 1) calculate tokens
    try:
        tokens_required = calculate_kling3_price(resolution, enable_audio, duration)
    except Exception as e:
        await tg_send_message(chat_id, f"❌ Ошибка настроек Kling 3.0: {e}", reply_markup=_main_menu_for(user_id))
        _set_mode(chat_id, user_id, "chat")
        st.pop("kling3_settings", None)
        st["ts"] = _now()
        sb_clear_user_state(user_id)
        return True

    # 2) balance check
    ensure_user_row(user_id)
    bal = get_balance(user_id) or 0
    if bal < tokens_required:
        await tg_send_message(
            chat_id,
            f"❌ Недостаточно токенов. Нужно: {tokens_required} • Баланс: {bal}",
            reply_markup=_main_menu_for(user_id),
        )
        return True

    # 3) charge
    ref_id = f"kling3_{user_id}_{int(time.time()*1000)}"
    add_tokens(
        user_id,
        -tokens_required,
        reason="kling3_create",
        ref_id=ref_id,
        meta={
            "duration": duration,
            "resolution": resolution,
            "enable_audio": enable_audio,
            "aspect_ratio": aspect_ratio,
        },
    )

    await tg_send_message(chat_id, "⏳ Генерирую Kling PRO 3.0…")

    poll_interval_sec = float(deps.get("poll_interval_sec", 2.0))
    timeout_sec = int(deps.get("timeout_sec", 300))

    try:
        task_id, _final_task, video_url = await run_kling3_task_and_wait(
            prompt=text,
            duration=duration,
            resolution=resolution,
            enable_audio=enable_audio,
            aspect_ratio=aspect_ratio,
            poll_interval_sec=poll_interval_sec,
            timeout_sec=timeout_sec,
        )

        if not video_url:
            await tg_send_message(
                chat_id,
                f"✅ Готово, но PiAPI не вернул ссылку на видео.
Task: {task_id}",
                reply_markup=_main_menu_for(user_id),
            )
        else:
            await tg_send_message(
                chat_id,
                f"✅ Kling PRO 3.0 готово!\n🎬 MP4: {video_url}",
                reply_markup=_main_menu_for(user_id),
            )

    except (Kling3RunnerError, Exception) as e:
        # refund on failure
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
            f"❌ Ошибка Kling PRO 3.0: {e}",
            reply_markup=_main_menu_for(user_id),
        )

    # 4) clear state
    _set_mode(chat_id, user_id, "chat")
    st.pop("kling3_settings", None)
    st["ts"] = _now()
    sb_clear_user_state(user_id)

    return True
