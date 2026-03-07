from __future__ import annotations

import math
import os
from typing import Any, Awaitable, Callable, Dict, Optional
from uuid import uuid4

from switchx_types import (
    DEFAULT_ALPHA_MODE,
    RESOLUTION_720,
    RESOLUTION_1080,
    STATE_NEED_PROMPT,
    STATE_NEED_REFERENCE,
    STATE_NEED_RESOLUTION,
    STATE_NEED_VIDEO,
    SWITCHX_BUSY_KIND,
    SWITCHX_MODE,
)

FlowDeps = Dict[str, Callable[..., Any]]


class SwitchXFlowError(RuntimeError):
    pass


def build_switchx_resolution_keyboard(duration_sec: int, cost_720: int, cost_1080: int) -> dict:
    return {
        "inline_keyboard": [
            [{"text": f"720p • {duration_sec}с • {cost_720} токенов", "callback_data": "switchx_res:720"}],
            [{"text": f"1080p • {duration_sec}с • {cost_1080} токенов", "callback_data": "switchx_res:1080"}],
        ]
    }


def start_switchx_mode(state: dict[str, Any]) -> None:
    state[SWITCHX_MODE] = {
        "step": STATE_NEED_VIDEO,
        "video_file_id": None,
        "video_duration_sec": 0,
        "max_resolution": None,
        "reference_file_id": None,
        "prompt": None,
        "alpha_mode": DEFAULT_ALPHA_MODE,
    }
    state["mode"] = SWITCHX_MODE


def get_rate_for_resolution(resolution: int) -> int:
    if int(resolution) == RESOLUTION_720:
        return int(os.getenv("SWITCHX_TOKENS_PER_SEC_720", "1") or 1)
    return int(os.getenv("SWITCHX_TOKENS_PER_SEC_1080", "2") or 2)


def compute_cost_tokens(duration_sec: int, resolution: int) -> int:
    duration = max(1, int(duration_sec or 0))
    rate = max(0, int(get_rate_for_resolution(int(resolution))))
    return int(math.ceil(duration * rate))


async def handle_switchx_resolution_callback(
    *,
    st: dict[str, Any],
    resolution: int,
    chat_id: int,
    user_id: int,
    tg_send_message: Callable[..., Awaitable[Any]],
    help_menu_for: Callable[[int], dict],
) -> dict[str, Any]:
    sx = st.get(SWITCHX_MODE) or {}
    if not sx or (sx.get("step") != STATE_NEED_RESOLUTION):
        await tg_send_message(chat_id, "SwitchX: сначала отправь видео.", reply_markup=help_menu_for(user_id))
        return {"ok": True}

    if int(resolution) not in (RESOLUTION_720, RESOLUTION_1080):
        await tg_send_message(chat_id, "Некорректное качество SwitchX.", reply_markup=help_menu_for(user_id))
        return {"ok": True}

    sx["max_resolution"] = int(resolution)
    sx["step"] = STATE_NEED_REFERENCE
    st[SWITCHX_MODE] = sx
    await tg_send_message(
        chat_id,
        f"✅ SwitchX: качество выбрано — {resolution}p.\n\nТеперь пришли reference image.",
        reply_markup=help_menu_for(user_id),
    )
    return {"ok": True}


async def handle_switchx_message(
    *,
    st: dict[str, Any],
    message: dict[str, Any],
    chat_id: int,
    user_id: int,
    incoming_text: str,
    tg_send_message: Callable[..., Awaitable[Any]],
    help_menu_for: Callable[[int], dict],
    main_menu_for: Callable[[int], dict],
    ensure_user_row: Callable[[int], Any],
    get_balance: Callable[[int], Any],
    add_tokens: Callable[..., Any],
    enqueue_job: Callable[..., Awaitable[Any]],
    busy_start: Callable[[int, str], None],
    busy_end: Callable[[int], None],
    set_mode: Callable[[int, int, str], None],
    clear_supabase_state: Callable[[int], None],
) -> Optional[dict[str, Any]]:
    sx = st.get(SWITCHX_MODE) or {}
    if st.get("mode") != SWITCHX_MODE or not sx:
        return None

    step = str(sx.get("step") or "")

    if step == STATE_NEED_VIDEO:
        video = message.get("video") or {}
        file_id = str(video.get("file_id") or "").strip()
        duration = int(video.get("duration") or 0)
        if not file_id:
            if incoming_text:
                await tg_send_message(chat_id, "Сначала пришли видео для SwitchX.", reply_markup=help_menu_for(user_id))
                return {"ok": True}
            return {"ok": True}
        if duration <= 0:
            await tg_send_message(chat_id, "Не смог определить длительность видео. Пришли видеофайл ещё раз.", reply_markup=help_menu_for(user_id))
            return {"ok": True}
        sx["video_file_id"] = file_id
        sx["video_duration_sec"] = int(duration)
        sx["step"] = STATE_NEED_RESOLUTION
        st[SWITCHX_MODE] = sx

        cost_720 = compute_cost_tokens(duration, RESOLUTION_720)
        cost_1080 = compute_cost_tokens(duration, RESOLUTION_1080)
        await tg_send_message(
            chat_id,
            f"✅ Видео получено.\nДлительность: {duration} сек.\n\nВыбери качество SwitchX:",
            reply_markup=build_switchx_resolution_keyboard(duration, cost_720, cost_1080),
        )
        return {"ok": True}

    if step == STATE_NEED_REFERENCE:
        photo = message.get("photo") or []
        if photo:
            last = photo[-1] if isinstance(photo, list) else {}
            file_id = str((last or {}).get("file_id") or "").strip()
        else:
            file_id = ""
        if not file_id:
            if incoming_text:
                await tg_send_message(chat_id, "Теперь пришли reference image для SwitchX.", reply_markup=help_menu_for(user_id))
                return {"ok": True}
            return {"ok": True}
        sx["reference_file_id"] = file_id
        sx["step"] = STATE_NEED_PROMPT
        st[SWITCHX_MODE] = sx
        await tg_send_message(
            chat_id,
            "✅ Reference image получен.\n\nТеперь пришли промпт: что нужно изменить в видео.",
            reply_markup=help_menu_for(user_id),
        )
        return {"ok": True}

    if step == STATE_NEED_PROMPT and incoming_text:
        prompt = incoming_text.strip()
        if not prompt:
            await tg_send_message(chat_id, "Промпт пустой. Пришли текстом, что должно измениться в видео.", reply_markup=help_menu_for(user_id))
            return {"ok": True}

        video_file_id = str(sx.get("video_file_id") or "").strip()
        reference_file_id = str(sx.get("reference_file_id") or "").strip()
        duration = int(sx.get("video_duration_sec") or 0)
        resolution = int(sx.get("max_resolution") or 0)
        if not video_file_id or not reference_file_id or duration <= 0 or resolution not in (RESOLUTION_720, RESOLUTION_1080):
            await tg_send_message(chat_id, "SwitchX: данные режима потерялись. Запусти режим заново.", reply_markup=main_menu_for(user_id))
            set_mode(chat_id, user_id, "chat")
            st.pop(SWITCHX_MODE, None)
            return {"ok": True}

        cost_tokens = compute_cost_tokens(duration, resolution)

        busy_start(int(user_id), SWITCHX_BUSY_KIND)
        charged = False
        try:
            try:
                ensure_user_row(user_id)
                bal = int(get_balance(user_id) or 0)
            except Exception:
                bal = 0

            if bal < cost_tokens:
                await tg_send_message(
                    chat_id,
                    f"❌ Недостаточно токенов для SwitchX.\nНужно: {cost_tokens}\nБаланс: {bal}",
                    reply_markup={"inline_keyboard": [[{"text": "➕ Пополнить", "callback_data": "topup:menu"}]]},
                )
                return {"ok": True}

            try:
                add_tokens(
                    user_id,
                    -int(cost_tokens),
                    reason="switchx_video",
                    meta={
                        "duration_sec": int(duration),
                        "max_resolution": int(resolution),
                        "cost_tokens": int(cost_tokens),
                        "alpha_mode": DEFAULT_ALPHA_MODE,
                    },
                )
            except TypeError:
                add_tokens(user_id, -int(cost_tokens), reason="switchx_video")
            charged = True

            job = {
                "job_id": uuid4().hex,
                "type": "switchx",
                "chat_id": int(chat_id),
                "user_id": int(user_id),
                "video_file_id": video_file_id,
                "reference_file_id": reference_file_id,
                "prompt": prompt,
                "max_resolution": int(resolution),
                "duration_sec": int(duration),
                "alpha_mode": DEFAULT_ALPHA_MODE,
                "charge_tokens": int(cost_tokens),
            }

            st.pop(SWITCHX_MODE, None)
            st["ts"] = st.get("ts")
            clear_supabase_state(user_id)
            set_mode(chat_id, user_id, "chat")

            await tg_send_message(
                chat_id,
                f"⏳ SwitchX: поставил в очередь.\nКачество: {resolution}p\nДлительность: {duration} сек\nСтоимость: {cost_tokens} токенов\n\nКак будет готово — пришлю видео.",
                reply_markup=help_menu_for(user_id),
            )
            await enqueue_job(job)
            return {"ok": True}

        except Exception as e:
            if charged:
                try:
                    try:
                        add_tokens(user_id, int(cost_tokens), reason="switchx_video_refund", meta={"stage": "main_exception"})
                    except TypeError:
                        add_tokens(user_id, int(cost_tokens), reason="switchx_video_refund")
                except Exception:
                    pass
            await tg_send_message(chat_id, f"❌ Ошибка SwitchX: {e}", reply_markup=main_menu_for(user_id))
            return {"ok": True}
        finally:
            busy_end(int(user_id))

    if incoming_text:
        await tg_send_message(chat_id, "SwitchX: иду по шагам. Пришли нужный файл или текст по текущему шагу.", reply_markup=help_menu_for(user_id))
        return {"ok": True}

    return {"ok": True}
