"""
nano_banana_pro_piapi.py

Nano Banana Pro (PiAPI) feature handler — designed to keep main.py changes minimal.

State in user session:
st["nano_banana_pro"] = {
  "step": "need_photo" | "need_prompt",
  "image_url": str | None,
  "resolution": "1K"|"2K" (optional, default "1K")
}

Product rules:
- Only 1K / 2K
- Cost: 2 tokens per generation
- Mode: i2i via input.image_urls
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Callable, List, Literal
import os
import json
import time

import httpx


PIAPI_BASE_URL = os.getenv("PIAPI_BASE_URL") or "https://api.piapi.ai"
PIAPI_API_KEY = os.getenv("PIAPI_API_KEY") or ""

ALLOWED_RESOLUTIONS = ("1K", "2K")
TOKENS_COST = 2.0


class NanoBananaProError(RuntimeError):
    pass


def _piapi_headers() -> Dict[str, str]:
    if not PIAPI_API_KEY:
        raise NanoBananaProError("PIAPI_API_KEY is not set")
    return {"X-API-Key": PIAPI_API_KEY, "Content-Type": "application/json"}


def _status_lower(task_json: Dict[str, Any]) -> str:
    data = task_json.get("data") if isinstance(task_json, dict) else None
    s = ""
    if isinstance(data, dict):
        s = str(data.get("status") or "")
    return s.strip().lower()


def _extract_output_urls(task_json: Dict[str, Any]) -> List[str]:
    data = task_json.get("data") if isinstance(task_json, dict) else None
    if not isinstance(data, dict):
        return []
    out = data.get("output")
    if not isinstance(out, dict):
        return []
    urls: List[str] = []
    one = out.get("image_url")
    many = out.get("image_urls")
    if isinstance(one, str) and one.strip():
        urls.append(one.strip())
    if isinstance(many, list):
        for u in many:
            if isinstance(u, str) and u.strip():
                urls.append(u.strip())
    # de-dup
    seen=set()
    uniq=[]
    for u in urls:
        if u not in seen:
            uniq.append(u); seen.add(u)
    return uniq


async def _piapi_create_task(*, prompt: str, image_urls: List[str], resolution: str = "1K") -> Dict[str, Any]:
    if resolution not in ALLOWED_RESOLUTIONS:
        raise NanoBananaProError(f"resolution must be one of {ALLOWED_RESOLUTIONS}")
    if not image_urls:
        raise NanoBananaProError("image_urls is required for i2i")
    payload: Dict[str, Any] = {
        "model": "gemini",
        "task_type": "nano-banana-pro",
        "input": {
            "prompt": prompt,
            "image_urls": image_urls,
            "output_format": "png",
            "resolution": resolution,
            "safety_level": "high",
        },
    }
    url = f"{PIAPI_BASE_URL}/api/v1/task"
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(url, headers=_piapi_headers(), json=payload)
    data = r.json() if r.headers.get("content-type","").startswith("application/json") else {"raw": r.text}
    if r.status_code >= 400:
        raise NanoBananaProError(f"PiAPI HTTP {r.status_code}: {json.dumps(data, ensure_ascii=False)[:1200]}")
    if isinstance(data, dict) and data.get("code") not in (None, 200):
        raise NanoBananaProError(f"PiAPI error code={data.get('code')}: {json.dumps(data, ensure_ascii=False)[:1200]}")
    return data


async def _piapi_get_task(task_id: str) -> Dict[str, Any]:
    url = f"{PIAPI_BASE_URL}/api/v1/task/{task_id}"
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(url, headers=_piapi_headers())
    data = r.json() if r.headers.get("content-type","").startswith("application/json") else {"raw": r.text}
    if r.status_code >= 400:
        raise NanoBananaProError(f"PiAPI HTTP {r.status_code}: {json.dumps(data, ensure_ascii=False)[:1200]}")
    if isinstance(data, dict) and data.get("code") not in (None, 200):
        raise NanoBananaProError(f"PiAPI error code={data.get('code')}: {json.dumps(data, ensure_ascii=False)[:1200]}")
    return data


async def _piapi_wait(task_id: str, *, timeout_s: float = 240.0, poll_s: float = 2.0) -> Dict[str, Any]:
    deadline = time.time() + timeout_s
    last: Dict[str, Any] = {}
    while time.time() < deadline:
        last = await _piapi_get_task(task_id)
        s = _status_lower(last)
        if s in ("completed", "failed", "canceled", "cancelled"):
            return last
        await _sleep(poll_s)
    raise NanoBananaProError("Timeout waiting PiAPI task")


async def _download_bytes(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.content


async def _sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)


# ---------- Public handler (keep main.py minimal) ----------

async def handle_update(
    *,
    kind: Literal["photo", "text"],
    chat_id: int,
    user_id: int,
    st: Dict[str, Any],
    # inputs:
    image_url: Optional[str] = None,
    text: Optional[str] = None,
    # callbacks from main:
    send_message: Callable[..., Any],
    send_photo_bytes: Callable[..., Any],
    # billing callbacks:
    ensure_user_row: Callable[[int], Any],
    get_balance: Callable[[int], Any],
    add_tokens: Callable[..., Any],
    topup_keyboard: Callable[[], dict],
    # ui:
    menu_keyboard: Callable[[], dict],
) -> bool:
    """
    Returns True if the update was handled (and main should return).
    """

    if st.get("mode") != "nano_banana_pro":
        return False

    nb = st.get("nano_banana_pro") or {"step": "need_photo", "image_url": None, "resolution": "1K"}
    step = (nb.get("step") or "need_photo")

    if kind == "photo":
        if not image_url:
            await send_message(chat_id, "Не вижу ссылку на фото. Пришли фото ещё раз.", reply_markup=menu_keyboard())
            return True
        # accept photo
        nb["image_url"] = image_url
        nb["step"] = "need_prompt"
        st["nano_banana_pro"] = nb
        await send_message(
            chat_id,
            "🍌 Nano Banana Pro ✅ Фото принял.\nТеперь напиши одним сообщением, что изменить.\n\nСтоимость: 2 токена.",
            reply_markup=menu_keyboard(),
        )
        return True

    # kind == "text"
    nav_text = (text or "").strip()
    if not nav_text:
        await send_message(chat_id, "Напиши текстом, что изменить (фон/стиль/детали).", reply_markup=menu_keyboard())
        return True

    # Ignore nav/commands
    if nav_text in ("⬅ Назад", "Назад") or nav_text.startswith("/"):
        return False

    if step != "need_prompt":
        await send_message(
            chat_id,
            "Сначала пришли ФОТО для Nano Banana Pro.\nОткрой «Фото будущего» → «🍌 Nano Banana Pro».",
            reply_markup=menu_keyboard(),
        )
        return True

    src_url = nb.get("image_url")
    if not src_url:
        await send_message(
            chat_id,
            "Не хватает фото. Открой «Фото будущего» → «🍌 Nano Banana Pro» и пришли фото заново.",
            reply_markup=menu_keyboard(),
        )
        return True

    # billing
    ensure_user_row(user_id)
    try:
        bal = float(get_balance(user_id) or 0)
    except Exception:
        bal = 0.0

    cost = TOKENS_COST
    if bal < cost:
        await send_message(
            chat_id,
            f"Недостаточно токенов 😕\nНужно: {int(cost)} токена для Nano Banana Pro.",
            reply_markup=topup_keyboard(),
        )
        return True

    # deduct before request
    try:
        add_tokens(user_id, -cost, reason="nano_banana_pro")
    except TypeError:
        add_tokens(user_id, -int(cost), reason="nano_banana_pro")

    await send_message(chat_id, "🍌 Nano Banana Pro — генерирую…", reply_markup=menu_keyboard())

    try:
        resolution = str(nb.get("resolution") or "1K")
        if resolution not in ALLOWED_RESOLUTIONS:
            resolution = "1K"

        created = await _piapi_create_task(prompt=nav_text, image_urls=[src_url], resolution=resolution)
        task_id = ((created.get("data") or {}) if isinstance(created, dict) else {}).get("task_id")
        if not task_id:
            raise NanoBananaProError(f"PiAPI didn't return task_id: {json.dumps(created, ensure_ascii=False)[:800]}")

        done = await _piapi_wait(task_id, timeout_s=240, poll_s=2)
        if _status_lower(done) == "failed":
            err = (((done.get("data") or {}) if isinstance(done, dict) else {}) or {}).get("error") or {}
            msg = ""
            if isinstance(err, dict):
                msg = err.get("message") or ""
            raise NanoBananaProError(msg or "PiAPI task failed")

        urls = _extract_output_urls(done)
        if not urls:
            raise NanoBananaProError("PiAPI completed but no output image_url(s)")

        out_bytes = await _download_bytes(urls[0])
        await send_photo_bytes(chat_id, out_bytes, caption="🍌 Nano Banana Pro — готово", reply_markup=menu_keyboard())

    except Exception as e:
        # refund
        try:
            try:
                add_tokens(user_id, cost, reason="nano_banana_pro_refund")
            except TypeError:
                add_tokens(user_id, int(cost), reason="nano_banana_pro_refund")
        except Exception:
            pass

        await send_message(chat_id, f"Ошибка Nano Banana Pro: {e}", reply_markup=menu_keyboard())
        return True

    # reset after success
    st["nano_banana_pro"] = {"step": "need_photo", "image_url": None, "resolution": "1K"}
    return True
