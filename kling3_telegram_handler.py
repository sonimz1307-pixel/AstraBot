import time
from typing import Any, Dict, Callable, Awaitable

from kling3_pricing import calculate_kling3_price
from kling3_runner import run_kling3_task_and_wait, Kling3RunnerError
from billing_db import ensure_user_row, get_balance, add_tokens


def _friendly_kling3_error(err: Exception) -> str:
    """Map provider/runner errors to user-friendly Russian messages."""
    msg = (str(err) or "").strip()
    low = msg.lower()

    # Typical PiAPI/Kling failure strings
    overload_markers = [
        "task failed",
        "failed",
        "server busy",
        "too many requests",
        "rate limit",
        "overloaded",
        "timeout",
        "timed out",
        "temporarily unavailable",
        "service unavailable",
        "bad gateway",
        "gateway timeout",
        "upstream",
    ]
    if any(m in low for m in overload_markers):
        return (
            "⚠️ Сервер Kling 3.0 сейчас перегружен или временно недоступен.\n"
            "Попробуйте ещё раз через пару минут."
        )

    # Default: show brief error without scary prefix
    return "⚠️ Не получилось сгенерировать видео. Попробуйте ещё раз чуть позже."


async def handle_kling3_wait_prompt(
    *,
    chat_id: int,
    user_id: int,
    incoming_text: str,
    st: Dict[str, Any],
    deps: Dict[str, Any],
) -> bool:
    """
    Обработка режима kling3_wait_prompt.
    Возвращает True, если сообщение обработано.
    """

    if st.get("mode") != "kling3_wait_prompt":
        return False

    tg_send_message: Callable[..., Awaitable[Any]] = deps["tg_send_message"]
    _main_menu_for = deps["_main_menu_for"]
    _is_nav_or_menu_text = deps["_is_nav_or_menu_text"]
    _set_mode = deps["_set_mode"]
    _now = deps["_now"]
    sb_clear_user_state = deps["sb_clear_user_state"]

    text = (incoming_text or "").strip()

    if not text:
        await tg_send_message(
            chat_id,
            "Пришли текст (промпт) для Kling PRO 3.0.",
            reply_markup=_main_menu_for(user_id),
        )
        return True

    # Если нажали кнопку меню — выходим из режима
    if _is_nav_or_menu_text(text):
        _set_mode(chat_id, user_id, "chat")
        st.pop("kling3_settings", None)
        st["ts"] = _now()
        sb_clear_user_state(user_id)
        await tg_send_message(
            chat_id,
            "Главное меню.",
            reply_markup=_main_menu_for(user_id),
        )
        return True

    settings = st.get("kling3_settings") or {}
    resolution = str(settings.get("resolution") or "720")
    enable_audio = bool(settings.get("enable_audio"))
    duration = int(settings.get("duration") or 5)
    aspect_ratio = str(settings.get("aspect_ratio") or "16:9")

    # 1) Расчёт токенов
    try:
        tokens_required = calculate_kling3_price(
            resolution,
            enable_audio,
            duration,
        )
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

    # 2) Проверка баланса
    ensure_user_row(user_id)
    bal = get_balance(user_id) or 0

    if bal < tokens_required:
        await tg_send_message(
            chat_id,
            f"❌ Недостаточно токенов.\nНужно: {tokens_required}\nБаланс: {bal}",
            reply_markup=_main_menu_for(user_id),
        )
        return True

    # 3) Списание
    ref_id = f"kling3_{user_id}_{int(time.time() * 1000)}"

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

    try:
        task_id, final_task, video_url = await run_kling3_task_and_wait(
            prompt=text,
            duration=duration,
            resolution=resolution,
            enable_audio=enable_audio,
            aspect_ratio=aspect_ratio,
            poll_interval_sec=deps.get("poll_interval_sec", 2.0),
            timeout_sec=deps.get("timeout_sec", 300),
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
        # Refund при ошибке
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

    # 4) Очистка состояния
    _set_mode(chat_id, user_id, "chat")
    st.pop("kling3_settings", None)
    st["ts"] = _now()
    sb_clear_user_state(user_id)

    return True
