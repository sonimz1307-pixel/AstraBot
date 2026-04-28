from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

import httpx

from db_supabase import supabase as sb
from kling3_kie_pricing import (
    normalize_kling3_kie_aspect_ratio,
    normalize_kling3_kie_duration,
    normalize_kling3_kie_mode,
    normalize_kling3_kie_shots,
)

ulog = logging.getLogger("uvicorn.error")

KIE_API_TOKEN = (
    os.getenv("KIE_API_TOKEN")
    or os.getenv("KIE_API_KEY")
    or os.getenv("KIE_TOKEN")
    or ""
).strip()
KIE_API_BASE = (os.getenv("KIE_API_BASE") or "https://api.kie.ai").strip().rstrip("/")
KIE_KLING3_MODEL = (os.getenv("KIE_KLING3_MODEL") or "kling-3.0/video").strip() or "kling-3.0/video"
KIE_KLING3_MAX_SHOTS = max(2, min(5, int(os.getenv("KIE_KLING3_MAX_SHOTS", "5") or "5")))

CREATE_PATH = "/api/v1/jobs/createTask"
STATUS_PATH = "/api/v1/jobs/recordInfo"


class Kling3KieError(Exception):
    pass


def _headers() -> Dict[str, str]:
    if not KIE_API_TOKEN:
        raise Kling3KieError("KIE_API_TOKEN is not set")
    return {
        "Authorization": f"Bearer {KIE_API_TOKEN}",
        "Content-Type": "application/json",
    }


def _detect_content_type_and_ext(data: bytes, filename: Optional[str] = None, content_type: Optional[str] = None) -> Tuple[str, str]:
    ct = str(content_type or "").strip().lower()
    ext = Path(str(filename or "")).suffix.lower().lstrip(".")
    if not ct and filename:
        ct = mimetypes.guess_type(filename)[0] or ""
    if data:
        if data.startswith(b"\x89PNG"):
            return "image/png", "png"
        if data[:3] == b"\xff\xd8\xff":
            return "image/jpeg", "jpg"
        if data[:12].startswith(b"RIFF") and data[8:12] == b"WEBP":
            return "image/webp", "webp"
        if data[4:8] == b"ftyp":
            return (ct if ct in {"video/mp4", "video/quicktime"} else "video/mp4"), (ext if ext in {"mp4", "mov"} else "mp4")
    if ct:
        guessed = mimetypes.guess_extension(ct) or ""
        return ct, (ext or guessed.lstrip(".") or "bin")
    return "application/octet-stream", (ext or "bin")


def upload_kling3_kie_input_bytes(
    data: bytes,
    *,
    filename: Optional[str] = None,
    content_type: Optional[str] = None,
    prefix: str = "kling3-kie/inputs",
) -> str:
    if not data:
        raise Kling3KieError("Empty upload data")
    if sb is None:
        raise Kling3KieError("Supabase client is not configured")
    ct, ext = _detect_content_type_and_ext(data, filename=filename, content_type=content_type)
    if ct == "image/webp" or ext == "webp":
        raise Kling3KieError("Kling 3.0 - New supports JPG/PNG images only. WEBP is not supported by KIE for image references/elements.")
    bucket = (os.getenv("SB_MEDIA_BUCKET") or os.getenv("SUPABASE_MEDIA_BUCKET") or "media").strip() or "media"
    safe_prefix = str(prefix or "kling3-kie/inputs").strip("/") or "kling3-kie/inputs"
    path = f"{safe_prefix}/{int(time.time())}_{uuid4().hex[:12]}.{ext}"
    try:
        sb.storage.from_(bucket).upload(
            path=path,
            file=data,
            file_options={"content-type": ct, "upsert": "true"},
        )
        public = sb.storage.from_(bucket).get_public_url(path)
        if isinstance(public, str):
            return public
        if isinstance(public, dict):
            url = public.get("publicUrl") or public.get("public_url") or ""
            if url:
                return str(url)
    except Exception as exc:
        raise Kling3KieError(f"Supabase upload failed: {exc}")
    raise Kling3KieError("Supabase upload failed: public url missing")


def _clean_url_list(values: Any, *, limit: int = 8) -> List[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [line.strip() for line in values.replace(",", "\n").splitlines()]
    if not isinstance(values, list):
        return []
    out: List[str] = []
    for item in values:
        text = str(item or "").strip()
        if text and text.startswith(("http://", "https://")) and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _normalize_element_name(value: Any) -> str:
    text = str(value or "").strip().lstrip("@")
    text = re.sub(r"[^A-Za-z0-9_]", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    if not text:
        raise Kling3KieError("Element name is required")
    if not re.match(r"^[A-Za-z]", text):
        text = f"element_{text}"
    return text[:48]


def normalize_kling3_kie_elements(elements: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    if not isinstance(elements, list):
        return []
    out: List[Dict[str, Any]] = []
    seen = set()
    for item in elements[:3]:
        if not isinstance(item, dict):
            continue
        image_urls = _clean_url_list(
            item.get("element_input_urls") or item.get("image_urls") or item.get("images") or item.get("urls"),
            limit=4,
        )
        video_urls = _clean_url_list(
            item.get("element_input_video_urls") or item.get("video_urls") or item.get("videos"),
            limit=1,
        )
        if not image_urls and not video_urls:
            continue
        name = _normalize_element_name(item.get("name") or item.get("element_name"))
        if image_urls:
            if len(image_urls) < 2:
                raise Kling3KieError(f"Image element @{name} requires 2–4 JPG/PNG images")
            if any(str(url).split("?", 1)[0].lower().endswith(".webp") for url in image_urls):
                raise Kling3KieError(f"Image element @{name} uses WEBP URL. Kling 3.0 - New image elements support JPG/PNG only.")
        if name in seen:
            continue
        seen.add(name)
        element: Dict[str, Any] = {
            "name": name,
            "description": str(item.get("description") or name).strip()[:160] or name,
        }
        if image_urls:
            element["element_input_urls"] = image_urls
        if video_urls:
            element["element_input_video_urls"] = video_urls[:1]
        out.append(element)
    return out


def _extract_task_id(payload: Dict[str, Any]) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    for key in ("taskId", "task_id", "id"):
        value = data.get(key) or payload.get(key)
        if value:
            return str(value)
    return None


def _safe_json(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            return value
    return value


def _first_nonempty(*values: Any) -> Optional[str]:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            text = value.strip()
            if text:
                return text
        elif value:
            return str(value)
    return None


def _extract_video_from_result(result: Any) -> Optional[str]:
    result = _safe_json(result)
    if isinstance(result, str):
        if result.startswith(("http://", "https://")):
            return result
        return None
    if isinstance(result, list):
        for item in result:
            found = _extract_video_from_result(item)
            if found:
                return found
        return None
    if not isinstance(result, dict):
        return None
    output = result.get("output") if isinstance(result.get("output"), dict) else {}
    candidates = [
        result.get("video_url"), result.get("videoUrl"), result.get("url"), result.get("download_url"), result.get("downloadUrl"),
        output.get("video"), output.get("video_url"), output.get("videoUrl"), output.get("url"), output.get("download_url"),
    ]
    videos = result.get("videos") or result.get("video_urls") or result.get("resultUrls") or output.get("videos")
    if isinstance(videos, list):
        candidates.extend(videos)
    return _first_nonempty(*candidates)


def normalize_kling3_kie_task(task: Dict[str, Any]) -> Dict[str, Any]:
    data = task.get("data") if isinstance(task.get("data"), dict) else task
    if not isinstance(data, dict):
        data = {}
    state = str(data.get("state") or data.get("status") or task.get("state") or task.get("status") or "").strip().lower()
    result_json = data.get("resultJson") or data.get("result_json") or data.get("result") or data.get("response") or {}
    fail_msg = _first_nonempty(data.get("failMsg"), data.get("errorMessage"), data.get("error_message"), data.get("msg"), task.get("msg"))
    video_url = _extract_video_from_result(result_json) or _extract_video_from_result(data)
    if state in {"success", "succeeded", "completed", "complete", "done", "finish", "finished"} or video_url:
        status = "succeeded" if video_url else "processing"
    elif state in {"fail", "failed", "error", "cancel", "cancelled", "canceled"}:
        status = "failed"
    elif state in {"queue", "queued", "pending", "waiting"}:
        status = "queued"
    else:
        status = "processing"
    return {
        "task_id": _extract_task_id(task) or _first_nonempty(data.get("taskId"), data.get("task_id")),
        "status": status,
        "provider_status": state or "unknown",
        "video_url": video_url,
        "download_url": video_url,
        "output_url": video_url,
        "error_message": fail_msg,
        "finished": bool(video_url or status == "failed"),
        "raw": task,
    }


def _validate_payload_inputs(*, generation_mode: str, duration: int, shots: List[Dict[str, Any]], start_url: Optional[str], end_url: Optional[str]) -> int:
    if generation_mode == "image_to_video" and not start_url:
        raise Kling3KieError("Image → Video requires start frame")
    if generation_mode == "multi_shot":
        if len(shots) < 2:
            raise Kling3KieError("Multi-shot requires at least 2 shots")
        if len(shots) > KIE_KLING3_MAX_SHOTS:
            raise Kling3KieError(f"Multi-shot supports up to {KIE_KLING3_MAX_SHOTS} shots")
        total = sum(int(s.get("duration") or 0) for s in shots)
        if total < 3 or total > 15:
            raise Kling3KieError("Total multi-shot duration must be 3–15 seconds")
        return total
    duration = normalize_kling3_kie_duration(duration)
    if duration < 3 or duration > 15:
        raise Kling3KieError("Duration must be 3–15 seconds")
    return duration


async def create_kling3_kie_task(
    *,
    prompt: str = "",
    duration: int = 5,
    mode: str = "pro",
    enable_audio: bool = False,
    aspect_ratio: str = "16:9",
    generation_mode: str = "text_to_video",
    start_image_url: Optional[str] = None,
    end_image_url: Optional[str] = None,
    start_image_bytes: Optional[bytes] = None,
    end_image_bytes: Optional[bytes] = None,
    start_filename: Optional[str] = None,
    end_filename: Optional[str] = None,
    multi_shots: Optional[List[Dict[str, Any]]] = None,
    kling_elements: Optional[List[Dict[str, Any]]] = None,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    normalized_mode = normalize_kling3_kie_mode(mode)
    normalized_aspect = normalize_kling3_kie_aspect_ratio(aspect_ratio)
    gen_mode = str(generation_mode or "text_to_video").strip().lower()
    if gen_mode in {"i2v", "image", "image_to_video"}:
        gen_mode = "image_to_video"
    elif gen_mode in {"multi", "multishot", "multi_shot", "multi-shot", "multi_shots"}:
        gen_mode = "multi_shot"
    else:
        gen_mode = "text_to_video"

    if start_image_bytes and not start_image_url:
        start_image_url = upload_kling3_kie_input_bytes(start_image_bytes, filename=start_filename, prefix="kling3-kie/frames")
    if end_image_bytes and not end_image_url:
        end_image_url = upload_kling3_kie_input_bytes(end_image_bytes, filename=end_filename, prefix="kling3-kie/frames")

    shots = normalize_kling3_kie_shots(multi_shots)
    bill_duration = _validate_payload_inputs(
        generation_mode=gen_mode,
        duration=int(duration or 5),
        shots=shots,
        start_url=start_image_url,
        end_url=end_image_url,
    )

    input_obj: Dict[str, Any] = {
        "prompt": str(prompt or "").strip()[:2500],
        "sound": bool(enable_audio),
        "duration": str(int(bill_duration)),
        "aspect_ratio": normalized_aspect,
        "mode": normalized_mode,
        "multi_shots": gen_mode == "multi_shot",
    }

    image_urls: List[str] = []
    if start_image_url:
        image_urls.append(str(start_image_url).strip())
    if gen_mode != "multi_shot" and end_image_url:
        image_urls.append(str(end_image_url).strip())
    if image_urls:
        input_obj["image_urls"] = image_urls[:2]

    if gen_mode == "multi_shot":
        input_obj["multi_prompt"] = [{"prompt": s["prompt"], "duration": int(s["duration"])} for s in shots]
        if not input_obj["prompt"]:
            input_obj["prompt"] = " ".join(s["prompt"] for s in shots)[:2500]

    elements = normalize_kling3_kie_elements(kling_elements)
    if elements:
        input_obj["kling_elements"] = elements

    payload = {"model": KIE_KLING3_MODEL, "input": input_obj}
    headers = _headers()
    if request_id:
        headers["x-request-id"] = str(request_id)

    try:
        safe = json.loads(json.dumps(payload, ensure_ascii=False))
        if safe.get("input", {}).get("image_urls"):
            safe["input"]["image_urls"] = ["<url>" for _ in safe["input"]["image_urls"]]
        if safe.get("input", {}).get("kling_elements"):
            for el in safe["input"]["kling_elements"]:
                if el.get("element_input_urls"):
                    el["element_input_urls"] = ["<url>" for _ in el["element_input_urls"]]
                if el.get("element_input_video_urls"):
                    el["element_input_video_urls"] = ["<url>" for _ in el["element_input_video_urls"]]
        ulog.warning("KIE_KLING3_CREATE -> %s payload=%s", f"{KIE_API_BASE}{CREATE_PATH}", safe)
    except Exception:
        pass

    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            response = await client.post(f"{KIE_API_BASE}{CREATE_PATH}", headers=headers, json=payload)
        except Exception as exc:
            raise Kling3KieError(f"KIE request failed: {exc}")
    if not (200 <= response.status_code < 300):
        raise Kling3KieError(f"KIE error {response.status_code}: {response.text[:2000]}")
    try:
        data = response.json()
    except Exception:
        data = {"raw": response.text}
    if isinstance(data, dict):
        code = data.get("code")
        if code not in (None, 0, 200, "0", "200"):
            raise Kling3KieError(f"KIE create failed: {data}")
    task_id = _extract_task_id(data if isinstance(data, dict) else {})
    if not task_id:
        raise Kling3KieError(f"KIE did not return taskId: {data}")
    return data


async def get_kling3_kie_task(task_id: str) -> Dict[str, Any]:
    task_id = str(task_id or "").strip()
    if not task_id:
        raise Kling3KieError("taskId is required")
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            response = await client.get(f"{KIE_API_BASE}{STATUS_PATH}", headers=_headers(), params={"taskId": task_id})
        except Exception as exc:
            raise Kling3KieError(f"KIE status request failed: {exc}")
    if not (200 <= response.status_code < 300):
        raise Kling3KieError(f"KIE status error {response.status_code}: {response.text[:2000]}")
    try:
        data = response.json()
    except Exception:
        data = {"raw": response.text}
    return data


extract_kling3_kie_task_id = _extract_task_id
extract_kling3_kie_video_url = lambda payload: normalize_kling3_kie_task(payload).get("video_url")
