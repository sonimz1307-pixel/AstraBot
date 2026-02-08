# nano_banana.py
import os
import time
import base64
import asyncio
from typing import Optional, Any

import httpx

REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN", "").strip()
REPLICATE_MODEL = os.getenv("REPLICATE_NANO_BANANA_MODEL", "google/nano-banana").strip()
REPLICATE_TIMEOUT_SEC = float(os.getenv("REPLICATE_TIMEOUT_SEC", "180"))


def _guess_mime(image_bytes: bytes) -> str:
    b = image_bytes[:16]
    if b.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if b.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def _data_url_from_bytes(image_bytes: bytes) -> str:
    mime = _guess_mime(image_bytes)
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    return f"data:{mime};base64,{b64}"


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


async def run_nano_banana(
    image_bytes: bytes,
    prompt: str,
    *,
    timeout_sec: float = REPLICATE_TIMEOUT_SEC,
) -> bytes:
    """
    Sends image+prompt to Replicate nano-banana and returns edited image bytes.
    """
    if not REPLICATE_API_TOKEN:
        raise RuntimeError("REPLICATE_API_TOKEN is not set")

    # Replicate predictions endpoint
    pred_url = "https://api.replicate.com/v1/predictions"
    headers = {
        "Authorization": f"Bearer {REPLICATE_API_TOKEN}",
        "Content-Type": "application/json",
    }

    # NOTE: model schema can differ; this is a safe generic payload for many image-edit models.
    payload = {
        "version": None,  # optional; if you use version-based calls, set it here
        "model": REPLICATE_MODEL,  # many wrappers accept model, some accept version only
        "input": {
            "image": _data_url_from_bytes(image_bytes),
            "prompt": prompt,
        },
    }

    async with httpx.AsyncClient(timeout=timeout_sec) as client:
        r = await client.post(pred_url, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()

        # If Replicate returns direct urls in "urls.get"
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
        return img.content
