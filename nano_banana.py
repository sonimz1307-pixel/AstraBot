# nano_banana.py
import os
import time
import base64
import asyncio
from typing import Optional, Any, Tuple

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


def _guess_ext_from_mime(mime: str) -> str:
    if mime == "image/png":
        return "png"
    if mime == "image/jpeg":
        return "jpg"
    if mime == "image/webp":
        return "webp"
    return "bin"


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


def _ext_from_url(url: str) -> Optional[str]:
    u = (url or "").lower()
    for ext in ("png", "jpg", "jpeg", "webp"):
        if f".{ext}" in u:
            return "jpg" if ext == "jpeg" else ext
    return None


def _convert_image_bytes(image_bytes: bytes, out_format: str) -> bytes:
    """
    Convert image bytes to out_format using Pillow if available.
    If Pillow isn't installed or conversion fails, returns original bytes.
    """
    out_format = (out_format or "").lower().strip()
    if not out_format:
        return image_bytes

    fmt_map = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP"}
    pil_fmt = fmt_map.get(out_format)
    if not pil_fmt:
        return image_bytes

    try:
        from io import BytesIO
        from PIL import Image

        im = Image.open(BytesIO(image_bytes))
        # JPEG doesn't support alpha
        if pil_fmt == "JPEG" and im.mode in ("RGBA", "LA"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        elif pil_fmt == "JPEG" and im.mode != "RGB":
            im = im.convert("RGB")

        buf = BytesIO()
        im.save(buf, format=pil_fmt, quality=95)
        return buf.getvalue()
    except Exception:
        return image_bytes


async def run_nano_banana(
    image_bytes: bytes,
    prompt: str,
    *,
    output_format: Optional[str] = None,
    timeout_sec: float = REPLICATE_TIMEOUT_SEC,
) -> Tuple[bytes, str]:
    """
    Sends image+prompt to Replicate nano-banana and returns (edited image bytes, ext).
    output_format: optional ("jpg"/"png"/"webp"). If specified, tries to convert locally.
    """
    if not REPLICATE_API_TOKEN:
        raise RuntimeError("REPLICATE_API_TOKEN is not set")

    pred_url = "https://api.replicate.com/v1/predictions"
    headers = {
        "Authorization": f"Bearer {REPLICATE_API_TOKEN}",
        "Content-Type": "application/json",
    }

    payload = {
        "version": None,
        "model": REPLICATE_MODEL,
        "input": {
            "image": _data_url_from_bytes(image_bytes),
            "prompt": prompt,
        },
    }

    async with httpx.AsyncClient(timeout=timeout_sec) as client:
        r = await client.post(pred_url, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()

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
        out_bytes = img.content

        # ext from output_format OR URL OR headers OR bytes sniff
        if output_format:
            out_bytes = _convert_image_bytes(out_bytes, output_format)
            ext = output_format.lower().replace("jpeg", "jpg")
        else:
            ext = _ext_from_url(out_url) or _ext_from_url(img.headers.get("content-type", "")) or _guess_ext_from_mime(_guess_mime(out_bytes))

        return out_bytes, ext
