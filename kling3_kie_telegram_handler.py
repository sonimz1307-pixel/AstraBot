from __future__ import annotations

import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional
from uuid import uuid4

from billing_db import add_tokens, ensure_user_row, get_balance
from kling3_kie_flow import Kling3KieError, normalize_kling3_kie_elements, upload_kling3_kie_input_bytes
from kling3_kie_pricing import (
    calculate_kling3_kie_price,
    kling3_kie_billable_seconds,
    normalize_kling3_kie_aspect_ratio,
    normalize_kling3_kie_duration,
    normalize_kling3_kie_generation_mode,
    normalize_kling3_kie_mode,
    normalize_kling3_kie_shots,
)
from queue_redis import enqueue_job


def _parse_multishot_prompt(text: str) -> List[Dict[str, Any]]:
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    shots: List[Dict[str, Any]] = []
    pat = re.compile(r"^(?:shot\s*)?(\d+)[\)\.\-:]?\s*(?:\(?\s*(\d+)\s*s?\s*\)?\s*[:\-]?)?\s*(.+)$", re.I)
    for line in lines:
        m = pat.match(line)
        if not m:
            if shots:
                shots[-1]["prompt"] = (shots[-1]["prompt"] + " " + line).strip()
            continue
        dur = int(m.group(2) or 3)
        prompt = (m.group(3) or "").strip()
        if prompt:
            shots.append({"prompt": prompt, "duration": max(1, min(12, dur))})
    return normalize_kling3_kie_shots(shots)


def _friendly_kling3_kie_error(err: Exception) -> str:
    raw = str(err or "").strip()
    msg = raw.lower()
    if "kie_api_token" in msg or "kie api token" in msg:
        return "⚠️ Не настроен KIE_API_TOKEN на Render."
    if "supabase" in msg or "storage" in msg:
        return "⚠️ Не удалось загрузить кадр/референс в Supabase Storage. Проверь bucket и публичные ссылки."
    if "timeout" in msg:
        return "⚠️ Kling 3.0 - New долго отвечает. Токены возвращены, попробуй позже."
    return f"⚠️ Не получилось запустить Kling 3.0 - New.\nДетали: {raw[:1200]}"


async def handle_kling3_kie_wait_prompt(
    *,
    chat_id: int,
    user_id: int,
    incoming_text: str,
    st: Dict[str, Any],
    deps: Dict[str, Any],
) -> bool:
    current_mode = str(st.get("mode") or "")
    if current_mode not in ("kling3_kie_wait_prompt", "kling3_kie_wait_multishot_action"):
        settings0 = dict(st.get("kling3_kie_settings") or {})
        if not settings0:
            return False
        gen_mode0 = normalize_kling3_kie_generation_mode(settings0.get("gen_mode") or settings0.get("mode") or "text_to_video")
        st["mode"] = "kling3_kie_wait_multishot_action" if gen_mode0 == "multi_shot" else "kling3_kie_wait_prompt"
        current_mode = str(st.get("mode") or "")

    if deps.get("_is_nav_or_menu_text") and deps["_is_nav_or_menu_text"](incoming_text):
        return True

    text = str(incoming_text or "").strip()
    if not text:
        return True

    tg_send_message: Callable[..., Awaitable[Any]] = deps["tg_send_message"]
    _main_menu_for = deps["_main_menu_for"]
    _set_mode = deps["_set_mode"]
    _now = deps["_now"]
    sb_clear_user_state = deps["sb_clear_user_state"]

    settings = dict(st.get("kling3_kie_settings") or {})
    gen_mode = normalize_kling3_kie_generation_mode(settings.get("gen_mode") or settings.get("mode") or "text_to_video")
    kie_mode = normalize_kling3_kie_mode(settings.get("kie_mode") or settings.get("resolution") or "pro")
    enable_audio = bool(settings.get("enable_audio"))
    duration = normalize_kling3_kie_duration(settings.get("duration") or 5)
    aspect_ratio = normalize_kling3_kie_aspect_ratio(settings.get("aspect_ratio") or "16:9")

    start_image_bytes: Optional[bytes] = settings.get("start_image_bytes")
    end_image_bytes: Optional[bytes] = settings.get("end_image_bytes")
    start_image_url = str(settings.get("start_image_url") or "").strip() or None
    end_image_url = str(settings.get("end_image_url") or "").strip() or None

    if gen_mode == "image_to_video" and not (start_image_bytes or start_image_url):
        await tg_send_message(
            chat_id,
            "❗Для Kling 3.0 - New Image → Video сначала пришли стартовое фото.",
            reply_markup=_main_menu_for(user_id),
        )
        return True

    multi_shots = normalize_kling3_kie_shots(settings.get("multi_shots") or [])
    prompt_for_job = text
    if gen_mode == "multi_shot":
        if len(multi_shots) < 2:
            if not multi_shots:
                multi_shots = _parse_multishot_prompt(text)
            if len(multi_shots) < 2:
                await tg_send_message(
                    chat_id,
                    "❗Для Multi-shot нужно минимум 2 шота. Заполни их в WebApp и вернись в бот.",
                    reply_markup=_main_menu_for(user_id),
                )
                return True

        trigger_words = {"старт", "запустить", "start", "/start", "run", "go"}
        normalized_text = " ".join(text.lower().split())
        if normalized_text not in trigger_words:
            await tg_send_message(
                chat_id,
                "✅ Multi-shot настройки уже сохранены.\n\n"
                "Основной prompt не нужен.\n"
                "Если хочешь — сначала пришли общий стартовый кадр.\n"
                "Когда всё готово, отправь сообщением: СТАРТ",
                reply_markup=_main_menu_for(user_id),
            )
            return True
        prompt_for_job = " ".join(item["prompt"] for item in multi_shots)[:2500]

    bill_seconds = kling3_kie_billable_seconds(duration=duration, multi_shots=multi_shots if gen_mode == "multi_shot" else None)
    try:
        tokens_required = calculate_kling3_kie_price(kie_mode, enable_audio, bill_seconds)
    except Exception as exc:
        await tg_send_message(chat_id, f"❌ Ошибка цены Kling 3.0 - New: {exc}", reply_markup=_main_menu_for(user_id))
        return True

    ensure_user_row(user_id)
    balance = int(get_balance(user_id) or 0)
    if balance < tokens_required:
        await tg_send_message(
            chat_id,
            f"❌ Недостаточно токенов. Нужно: {tokens_required}. Баланс: {balance}",
            reply_markup=_main_menu_for(user_id),
        )
        return True

    ref_id = f"kling3_kie_{user_id}_{int(time.time() * 1000)}"
    try:
        if start_image_bytes and not start_image_url:
            start_image_url = upload_kling3_kie_input_bytes(
                start_image_bytes,
                filename="telegram_start_frame.jpg",
                prefix="kling3-kie/tg-frames",
            )
        if gen_mode == "image_to_video" and end_image_bytes and not end_image_url:
            end_image_url = upload_kling3_kie_input_bytes(
                end_image_bytes,
                filename="telegram_end_frame.jpg",
                prefix="kling3-kie/tg-frames",
            )
    except Kling3KieError as exc:
        await tg_send_message(chat_id, _friendly_kling3_kie_error(exc), reply_markup=_main_menu_for(user_id))
        return True

    elements = normalize_kling3_kie_elements(settings.get("kling_elements") or [])

    add_tokens(
        user_id,
        -tokens_required,
        reason="kling3_kie_create",
        ref_id=ref_id,
        meta={
            "model": "Kling 3.0 - New",
            "provider": "kie",
            "mode": gen_mode,
            "kie_mode": kie_mode,
            "duration": bill_seconds,
            "enable_audio": enable_audio,
            "aspect_ratio": aspect_ratio,
            "multi_shots": len(multi_shots),
            "elements": [e.get("name") for e in elements],
        },
    )

    job = {
        "job_id": uuid4().hex,
        "kind": "tg_kling3_kie_run",
        "chat_id": int(chat_id),
        "user_id": int(user_id),
        "prompt": prompt_for_job,
        "duration": int(bill_seconds),
        "mode": gen_mode,
        "kie_mode": kie_mode,
        "aspect_ratio": aspect_ratio,
        "enable_audio": bool(enable_audio),
        "start_image_url": start_image_url,
        "end_image_url": end_image_url if gen_mode == "image_to_video" else None,
        "multi_shots": multi_shots if gen_mode == "multi_shot" else [],
        "kling_elements": elements,
        "charge_tokens": int(tokens_required),
        "charge_ref_id": ref_id,
        "refund_reason": "kling3_kie_refund",
        "origin": "telegram",
    }
    queue_name = str(deps.get("queue_name") or "kling3_kie")
    await enqueue_job(job, queue_name=queue_name)

    await tg_send_message(
        chat_id,
        f"⏳ Kling 3.0 - New поставлен в очередь.\nРежим: {gen_mode} • {kie_mode} • {bill_seconds} сек • {tokens_required} ток.",
        reply_markup=_main_menu_for(user_id),
    )
    _set_mode(chat_id, user_id, "chat")
    st.pop("kling3_kie_settings", None)
    st["ts"] = _now()
    sb_clear_user_state(user_id)
    return True
