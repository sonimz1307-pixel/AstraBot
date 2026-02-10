# nano_banana.py
import os
import time
import asyncio
from typing import Optional, Any, Tuple

import httpx

REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN", "").strip()
# ожидается "owner/model", например "google/nano-banana"
REPLICATE_MODEL = os.getenv("REPLICATE_NANO_BANANA_MODEL", "google/nano-banana").strip()
REPLICATE_TIMEOUT_SEC = float(os.getenv("REPLICATE_TIMEOUT_SEC", "180"))


def _normalize_output_format(fmt: Optional[str]) -> Optional[str]:
    if not fmt:
        return None
    fmt = fmt.strip().lower()
    if fmt == "jpeg":
        fmt = "jpg"
    if fmt not in ("jpg", "png", "webp"):
        return None
    return fmt


def _detect_image_mime_and_ext(image_bytes: bytes) -> Tuple[str, str]:
    """Best-effort detect mime/ext from magic bytes."""
    b = image_bytes or b""
    if b.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if b[:3] == b"\xff\xd8\xff":
        return "image/jpeg", "jpg"
    if b.startswith(b"RIFF") and b[8:12] == b"WEBP":
        return "image/webp", "webp"
    # fallback
    return "application/octet-stream", "jpg"


def _pick_output_url(output: Any) -> Optional[str]:
    if isinstance(output, str) and output.startswith("http"):
        return output
    if isinstance(output, list):
        for x in output:
            u = _pick_output_url(x)
            if u:
                return u
    if isinstance(output, dict):
        for k in ("url", "image", "image_url", "output", "file"):
            v = output.get(k)
            if isinstance(v, str) and v.startswith("http"):
                return v
        for v in output.values():
            u = _pick_output_url(v)
            if u:
                return u
    return None


def _ext_from_url(url: str) -> Optional[str]:
    u = (url or "").lower()
    for ext in ("png", "jpg", "jpeg", "webp"):
        if f".{ext}" in u:
            return "jpg" if ext == "jpeg" else ext
    return None


def _split_owner_model(model: str) -> Tuple[str, str]:
    model = (model or "").strip().strip("/")
    if "/" not in model:
        # fallback: treat whole as model name (owner unknown)
        return "google", model
    owner, name = model.split("/", 1)
    return owner, name


def _pick_public_file_url(file_json: dict) -> Optional[str]:
    """
    Replicate Files API can return multiple URLs.
    Нам нужен именно публичный delivery/download URL вида https://replicate.delivery/...
    потому что раннер модели должен скачать файл без твоего Bearer токена.
    """
    urls = (file_json or {}).get("urls") or {}
    # приоритет: download/delivery (публичный), потом get (иногда тоже delivery, но не всегда)
    for k in ("download", "delivery", "get"):
        u = urls.get(k)
        if isinstance(u, str) and u.startswith("http"):
            # если это delivery — идеально
            if "replicate.delivery" in u:
                return u
            # если не delivery — всё равно сохраним как fallback
            # (на некоторых аккаунтах get тоже может быть публичным)
            fallback = u
            # не возвращаем сразу — вдруг ниже будет delivery
            # но если других не будет, вернём fallback
    # если delivery не нашли, попробуем любой url из urls
    for u in urls.values():
        if isinstance(u, str) and u.startswith("http"):
            return u
    return None


async def _upload_to_replicate_files(
    client: httpx.AsyncClient,
    image_bytes: bytes,
    *,
    filename: Optional[str] = None,
) -> str:
    """Upload image bytes to Replicate Files API and return a public URL (replicate.delivery/...).

    Replicate HTTP API (files.create) expects multipart field name `content`.
    """
    if not image_bytes:
        raise RuntimeError("Replicate Files: empty image bytes")

    url = "https://api.replicate.com/v1/files"
    headers = {
        "Authorization": f"Bearer {REPLICATE_API_TOKEN}",
        "Accept": "application/json",
    }

    mime, ext = _detect_image_mime_and_ext(image_bytes)
    if not filename:
        filename = f"input.{ext}"

    # IMPORTANT: field name must be 'content' (not 'file')
    files = {"content": (filename, image_bytes, mime)}

    r = await client.post(url, headers=headers, files=files)
    if r.status_code >= 400:
        raise RuntimeError(f"Replicate Files API error {r.status_code}: {r.text}")

    data = r.json()

    uploaded_url = _pick_public_file_url(data)
    if not uploaded_url:
        raise RuntimeError(f"Replicate Files: unexpected response: {data}")

    return uploaded_url


async def run_nano_banana(
    image_bytes: bytes,
    prompt: str,
    *,
    output_format: Optional[str] = "jpg",
    timeout_sec: float = REPLICATE_TIMEOUT_SEC,
) -> Tuple[bytes, str]:
    """Google Nano Banana via Replicate.

    Flow:
    1) upload image to Replicate Files API -> get public URL
    2) create prediction at /v1/models/{owner}/{model}/predictions
    3) poll prediction until succeeded/failed/canceled
    Returns (edited_image_bytes, ext).
    """
    if not REPLICATE_API_TOKEN:
        raise RuntimeError("REPLICATE_API_TOKEN is not set")

    out_fmt = _normalize_output_format(output_format) or "jpg"
    owner, model_name = _split_owner_model(REPLICATE_MODEL)
    pred_url = f"https://api.replicate.com/v1/models/{owner}/{model_name}/predictions"

    headers_json = {
        "Authorization": f"Bearer {REPLICATE_API_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(timeout=timeout_sec) as client:
        uploaded_url = await _upload_to_replicate_files(client, image_bytes)

        # Several schema variants (Replicate models sometimes change field shapes).
        payloads = [
            # 1) image_input as list of objects (UI иногда так показывает)
            {
                "input": {
                    "prompt": prompt,
                    "image_input": [{"value": uploaded_url}],
                    "aspect_ratio": "match_input_image",
                    "output_format": out_fmt,
                }
            },
            # 1b) image_input as list of strings (частая схема)
            {
                "input": {
                    "prompt": prompt,
                    "image_input": [uploaded_url],
                    "aspect_ratio": "match_input_image",
                    "output_format": out_fmt,
                }
            },
            # 2) image_input as plain string
            {
                "input": {
                    "prompt": prompt,
                    "image_input": uploaded_url,
                    "aspect_ratio": "match_input_image",
                    "output_format": out_fmt,
                }
            },
            # 3) sometimes field name is "image" instead of "image_input"
            {
                "input": {
                    "prompt": prompt,
                    "image": uploaded_url,
                    "aspect_ratio": "match_input_image",
                    "output_format": out_fmt,
                }
            },
            # 4) common alt: init_image
            {
                "input": {
                    "prompt": prompt,
                    "init_image": uploaded_url,
                    "aspect_ratio": "match_input_image",
                    "output_format": out_fmt,
                }
            },
        ]

        last_err: Optional[str] = None
        data = None

        for payload in payloads:
            r = await client.post(pred_url, headers=headers_json, json=payload)
            if r.status_code == 422:
                last_err = r.text
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"Replicate create prediction error {r.status_code}: {r.text}")
            data = r.json()
            break

        if data is None:
            raise RuntimeError(
                f"Replicate: 422 Unprocessable Entity for all payload variants. Last response: {last_err}"
            )

        get_url = (data.get("urls") or {}).get("get") or data.get("url")
        if not get_url:
            raise RuntimeError(f"Replicate: unexpected response: {data}")

        t0 = time.time()
        last = data
        while True:
            status = (last.get("status") or "").lower()
            if status in ("succeeded", "failed", "canceled"):
                break
            if time.time() - t0 > timeout_sec:
                raise TimeoutError("Replicate: timeout waiting for prediction")

            rr = await client.get(
                get_url,
                headers={"Authorization": f"Bearer {REPLICATE_API_TOKEN}", "Accept": "application/json"},
            )
            if rr.status_code >= 400:
                raise RuntimeError(f"Replicate poll error {rr.status_code}: {rr.text}")
            last = rr.json()
            await asyncio.sleep(0.8)

        if (last.get("status") or "").lower() != "succeeded":
            raise RuntimeError(f"Replicate: prediction failed: {last}")

        out_url = _pick_output_url(last.get("output"))
        if not out_url:
            raise RuntimeError(f"Replicate: succeeded but no output url: {last}")

        img = await client.get(out_url)
        if img.status_code >= 400:
            raise RuntimeError(f"Replicate output download error {img.status_code}: {img.text}")

        ext = out_fmt or _ext_from_url(out_url) or "jpg"
        return img.content, ext
