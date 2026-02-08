# nano_banana.py
import os
import time
import base64
from typing import Tuple, Optional, Any, Dict, List

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
    # Replicate output can be:
    # - string URL
    # - list of URLs
    # - dict with url fields
    if isinstance(output, str) and output.startswith("http"):
        return output
    if isinstance(output, list):
        for x in output:
            if isinstance(x, str) and x.startswith("http"):
                return x
            if isinstance(x, dict):
                for k in ("url", "image", "image_url", "output", "file"):
                    v = x.get(k)
                    if isinstance(v, str) and v.startswith("http"):
                        return v
    if isinstance(output, dict):
        for k in ("url", "image", "image_url", "output", "file"):
            v = output.get(k)
            if isinstance(v, str) and v.startswith("http"):
                return v
        # deep scan
        for v in output.values():
            u = _pick_output_url(v)
            if u:
                return u
    return None


async def run_nano_banana(
    image_bytes: bytes,
    prompt: str,
    aspect_ratio: str = "match_input_image",
    output_format: str = "jpg",
    timeout_sec: float = REPLICATE_TIMEOUT_SEC,
) -> Tuple[bytes, str]:
    """
    Runs google/nano-banana on Replicate (image editing).
    Returns: (result_image_bytes, ext)
    """
    if not REPLICATE_API_TOKEN:
        raise RuntimeError("REPLICATE_API_TOKEN is not set")

    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("Empty prompt")

    model_owner, model_name = REPLICATE_MODEL.split("/", 1)
    url = f"https://api.replicate.com/v1/models/{model_owner}/{model_name}/predictions"

    headers = {
        "Authorization": f"Bearer {REPLICATE_API_TOKEN}",
        "Content-Type": "application/json",
        # Replicate supports Prefer: wait, but we still poll safely
        "Prefer": "wait",
    }

    payload: Dict[str, Any] = {
        "input": {
            "prompt": prompt,
            "image_input": [{"value": _data_url_from_bytes(image_bytes)}],
            "aspect_ratio": aspect_ratio,
            "output_format": output_format,
        }
    }

    async with httpx.AsyncClient(timeout=timeout_sec) as client:
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        pred = r.json()
        pred_url = pred.get("urls", {}).get("get") or pred.get("url")
        if not pred_url:
            # fallback: Replicate usually returns "id"
            pred_id = pred.get("id")
            if pred_id:
                pred_url = f"https://api.replicate.com/v1/predictions/{pred_id}"
        if not pred_url:
            raise RuntimeError("Replicate: cannot find prediction URL")

        t0 = time.time()
        last = pred

        while True:
            status = (last.get("status") or "").lower()
            if status in ("succeeded", "failed", "canceled"):
                break
            if time.time() - t0 > timeout_sec:
                raise TimeoutError("Replicate: timeout waiting for prediction")

            rr = await client.get(pred_url, headers={"Authorization": f"Bearer {REPLICATE_API_TOKEN}"})
            rr.raise_for_status()
            last = rr.json()
            await httpx.AsyncClient().aclose()  # safety no-op in some envs

        if (last.get("status") or "").lower() != "succeeded":
            err = last.get("error") or "unknown error"
            raise RuntimeError(f"Replicate failed: {err}")

        out_url = _pick_output_url(last.get("output"))
        if not out_url:
            raise RuntimeError("Replicate: succeeded but no output url found")

        img_resp = await client.get(out_url)
        img_resp.raise_for_status()
        result_bytes = img_resp.content

    ext = "jpg"
    of = (output_format or "").lower().strip()
    if of in ("jpg", "jpeg"):
        ext = "jpg"
    elif of in ("png",):
        ext = "png"
    elif of in ("webp",):
        ext = "webp"

    return result_bytes, ext
