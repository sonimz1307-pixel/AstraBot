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


async def _upload_to_replicate_files(
    client: httpx.AsyncClient,
    image_bytes: bytes,
    *,
    filename: str = "input.jpg",
) -> str:
    """
    Upload image bytes to Replicate Files API and return a public URL (replicate.delivery/...).
    """
    url = "https://api.replicate.com/v1/files"
    headers = {"Authorization": f"Bearer {REPLICATE_API_TOKEN}"}

    # multipart upload
    files = {"file": (filename, image_bytes, "application/octet-stream")}

    r = await client.post(url, headers=headers, files=files)
    r.raise_for_status()
    data = r.json()

    # Replicate returns urls.get for files
    uploaded_url = (data.get("urls") or {}).get("get")
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
    """
    Google Nano Banana via Replicate:
    - uploads image to Replicate Files API
    - calls /v1/models/{owner}/{model}/predictions
    - polls until succeeded
    Returns (edited_image_bytes, ext).
    """
    if not REPLICATE_API_TOKEN:
        raise RuntimeError("REPLICATE_API_TOKEN is not set")

    out_fmt = _normalize_output_format(output_format) or "jpg"
    owner, model_name = _split_owner_model(REPLICATE_MODEL)

    # model-specific endpoint (ВАЖНО для nano-banana)
    pred_url = f"https://api.replicate.com/v1/models/{owner}/{model_name}/predictions"

    headers_json = {
        "Authorization": f"Bearer {REPLICATE_API_TOKEN}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=timeout_sec) as client:
        # 1) upload file -> get replicate.delivery URL
        uploaded_url = await _upload_to_replicate_files(client, image_bytes)

        # 2) create prediction with correct schema
        payload = {
            "input": {
                "prompt": prompt,
                "image_input": [{"value": uploaded_url}],
                "aspect_ratio": "match_input_image",
                "output_format": out_fmt,
            }
        }

        r = await client.post(pred_url, headers=headers_json, json=payload)
        # Если тут 422 — значит схема/ключи не совпали, но для nano-banana это корректно.
        r.raise_for_status()
        data = r.json()

        get_url = (data.get("urls") or {}).get("get") or data.get("url")
        if not get_url:
            raise RuntimeError(f"Replicate: unexpected response: {data}")

        # 3) poll
        t0 = time.time()
        last = data
        while True:
            status = (last.get("status") or "").lower()
            if status in ("succeeded", "failed", "canceled"):
                break
            if time.time() - t0 > timeout_sec:
                raise TimeoutError("Replicate: timeout waiting for prediction")

            rr = await client.get(get_url, headers={"Authorization": f"Bearer {REPLICATE_API_TOKEN}"})
            rr.raise_for_status()
            last = rr.json()
            await asyncio.sleep(0.8)

        if (last.get("status") or "").lower() != "succeeded":
            raise RuntimeError(f"Replicate: prediction failed: {last}")

        out_url = _pick_output_url(last.get("output"))
        if not out_url:
            raise RuntimeError(f"Replicate: succeeded but no output url: {last}")

        img = await client.get(out_url)
        img.raise_for_status()

        ext = out_fmt or _ext_from_url(out_url) or "jpg"
        return img.content, ext
