from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from db_supabase import supabase

WORKSPACE_VIDEO_GENERATIONS_TABLE = "workspace_video_generations"
WORKSPACE_VIDEO_UPLOADS_TABLE = "workspace_video_uploads"
WORKSPACE_VIDEO_EDIT_JOBS_TABLE = "workspace_video_edit_jobs"

WORKSPACE_VIDEOS_BUCKET = (os.getenv("WORKSPACE_VIDEOS_BUCKET", "workspace-videos") or "workspace-videos").strip() or "workspace-videos"
VIDEO_EDIT_QUEUE_NAME = (os.getenv("WORKSPACE_VIDEO_EDIT_QUEUE", "video_edit") or "video_edit").strip() or "video_edit"

MAX_VIDEO_UPLOAD_BYTES = int(os.getenv("WORKSPACE_VIDEO_UPLOAD_LIMIT_BYTES", str(300 * 1024 * 1024)))
MAX_AUDIO_UPLOAD_BYTES = int(os.getenv("WORKSPACE_AUDIO_UPLOAD_LIMIT_BYTES", str(50 * 1024 * 1024)))
MAX_MERGE_ITEMS = int(os.getenv("WORKSPACE_VIDEO_MAX_MERGE_ITEMS", "10"))
MAX_AUDIO_CLIPS = int(os.getenv("WORKSPACE_VIDEO_MAX_AUDIO_CLIPS", "3"))
MAX_OUTPUT_DURATION_SEC = int(os.getenv("WORKSPACE_VIDEO_MAX_DURATION_SEC", "300"))
UPLOAD_TTL_HOURS = int(os.getenv("WORKSPACE_VIDEO_UPLOAD_TTL_HOURS", "24"))

_VIDEO_EXTS = {"mp4", "mov", "webm"}
_AUDIO_EXTS = {"mp3", "wav", "m4a"}
_VIDEO_CTYPES = {"video/mp4", "video/quicktime", "video/webm"}
_AUDIO_CTYPES = {"audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav", "audio/mp4", "audio/m4a", "audio/aac"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _trim_text(value: Any, limit: int = 180) -> str:
    text = str(value or "").strip()
    return text[:limit] if text else ""


def sanitize_filename(filename: str) -> str:
    value = Path(filename or "file").name
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return value or "file"


def _detect_file_kind(filename: str, content_type: str) -> tuple[str, str, str]:
    ctype = str(content_type or "").split(";", 1)[0].strip().lower()
    ext = Path(filename or "").suffix.lower().lstrip(".")
    if ext in _VIDEO_EXTS or ctype in _VIDEO_CTYPES or ctype.startswith("video/"):
        return "video", ext or "mp4", ctype or mimetypes.guess_type(filename)[0] or "video/mp4"
    if ext in _AUDIO_EXTS or ctype in _AUDIO_CTYPES or ctype.startswith("audio/"):
        guessed = ctype or mimetypes.guess_type(filename)[0] or "audio/mpeg"
        if ext == "wav":
            guessed = "audio/wav"
        elif ext == "m4a":
            guessed = "audio/mp4"
        elif ext == "mp3":
            guessed = "audio/mpeg"
        return "audio", ext or "mp3", guessed
    raise ValueError("Неподдерживаемый формат. Разрешены: mp4, mov, webm, mp3, wav, m4a.")


def _assert_size(kind: str, raw_bytes: bytes) -> None:
    size = len(raw_bytes or b"")
    if kind == "video" and size > MAX_VIDEO_UPLOAD_BYTES:
        raise ValueError(f"Видео слишком большое. Лимит: {MAX_VIDEO_UPLOAD_BYTES // (1024 * 1024)} МБ.")
    if kind == "audio" and size > MAX_AUDIO_UPLOAD_BYTES:
        raise ValueError(f"Аудио слишком большое. Лимит: {MAX_AUDIO_UPLOAD_BYTES // (1024 * 1024)} МБ.")
    if size <= 0:
        raise ValueError("Пустой файл загрузить нельзя.")


def _storage_content_type_to_ext(content_type: Optional[str], url: Optional[str] = None) -> str:
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    mapping = {
        "video/mp4": "mp4",
        "video/quicktime": "mov",
        "video/webm": "webm",
        "audio/mpeg": "mp3",
        "audio/mp3": "mp3",
        "audio/wav": "wav",
        "audio/x-wav": "wav",
        "audio/mp4": "m4a",
        "audio/m4a": "m4a",
    }
    if ctype in mapping:
        return mapping[ctype]
    guessed = mimetypes.guess_extension(ctype) or ""
    guessed = guessed.lstrip(".").lower()
    if guessed:
        return guessed
    parsed_path = urlparse(url or "").path
    suffix = Path(parsed_path).suffix.lstrip(".").lower()
    if suffix:
        return suffix
    return "bin"


def _extract_storage_public_url(public_result: Any) -> Optional[str]:
    if isinstance(public_result, str):
        return public_result
    if isinstance(public_result, dict):
        return public_result.get("publicUrl") or public_result.get("public_url")
    return None


def _extract_storage_signed_url(signed_result: Any) -> Optional[str]:
    if isinstance(signed_result, str):
        return signed_result
    if isinstance(signed_result, dict):
        return (
            signed_result.get("signedURL")
            or signed_result.get("signedUrl")
            or signed_result.get("signed_url")
            or signed_result.get("url")
        )
    return None


def _absolutize_supabase_url(url: Optional[str]) -> Optional[str]:
    value = str(url or "").strip()
    if not value:
        return None
    if value.startswith("http://") or value.startswith("https://"):
        return value
    base = (os.getenv("SUPABASE_URL", "") or "").strip().rstrip("/")
    if base and value.startswith("/"):
        return f"{base}{value}"
    return value


def build_workspace_video_access_urls(*, storage_path: Optional[str], fallback_url: Optional[str], expires_in: int = 3600) -> Dict[str, Optional[str]]:
    storage_path_text = str(storage_path or "").strip()
    fallback_text = str(fallback_url or "").strip() or None
    signed_url: Optional[str] = None

    if storage_path_text and supabase is not None:
        try:
            signed_result = supabase.storage.from_(WORKSPACE_VIDEOS_BUCKET).create_signed_url(storage_path_text, expires_in)
            signed_url = _absolutize_supabase_url(_extract_storage_signed_url(signed_result))
        except Exception:
            signed_url = None
        if not signed_url:
            try:
                public_result = supabase.storage.from_(WORKSPACE_VIDEOS_BUCKET).get_public_url(storage_path_text)
                signed_url = _absolutize_supabase_url(_extract_storage_public_url(public_result))
            except Exception:
                signed_url = None

    access_url = signed_url or fallback_text
    return {
        "video_url": access_url,
        "download_url": access_url,
        "signed_url": signed_url,
    }


def _storage_path_for_upload(user_id: int, upload_id: str, filename: str, ext: str) -> str:
    now = datetime.now(timezone.utc)
    safe_name = sanitize_filename(Path(filename or "upload").stem)
    safe_ext = (ext or "bin").lstrip(".").lower() or "bin"
    return f"{user_id}/uploads/{now:%Y/%m/%d}/{upload_id}_{safe_name}.{safe_ext}"


def _storage_path_for_edited_video(user_id: int, generation_id: str, ext: str = "mp4") -> str:
    now = datetime.now(timezone.utc)
    safe_ext = (ext or "mp4").lstrip(".").lower() or "mp4"
    return f"{user_id}/edited/{now:%Y/%m/%d}/{generation_id}.{safe_ext}"


def _upload_storage_bytes(storage_path: str, raw_bytes: bytes, content_type: str) -> None:
    if supabase is None:
        raise RuntimeError("Supabase client is not configured")
    supabase.storage.from_(WORKSPACE_VIDEOS_BUCKET).upload(
        path=storage_path,
        file=raw_bytes,
        file_options={"content-type": content_type or "application/octet-stream", "upsert": "true"},
    )


def _download_storage_bytes(storage_path: str) -> bytes:
    if supabase is None:
        raise RuntimeError("Supabase client is not configured")
    data = supabase.storage.from_(WORKSPACE_VIDEOS_BUCKET).download(storage_path)
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if hasattr(data, "read"):
        return data.read()
    raise RuntimeError("Supabase download returned empty data")


def _run(cmd: List[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "ffmpeg/ffprobe error").strip()
        raise RuntimeError(err[:4000])


def _parse_ffprobe_rate(value: Any) -> float:
    text = str(value or "").strip()
    if not text or text in {"0/0", "N/A"}:
        return 0.0
    if "/" in text:
        num, den = text.split("/", 1)
        try:
            num_f = float(num)
            den_f = float(den)
            return (num_f / den_f) if den_f else 0.0
        except Exception:
            return 0.0
    try:
        return float(text)
    except Exception:
        return 0.0


def probe_media(local_path: str) -> Dict[str, Any]:
    cmd = ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(local_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "ffprobe failed").strip()
        raise RuntimeError(err[:4000])

    payload = json.loads(proc.stdout or "{}")
    streams = payload.get("streams") or []
    fmt = payload.get("format") or {}
    try:
        duration = float(fmt.get("duration") or 0.0)
    except Exception:
        duration = 0.0
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), {}) or {}
    fps = _parse_ffprobe_rate(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate"))
    frame_count = 0
    try:
        frame_count = int(video_stream.get("nb_frames") or 0)
    except Exception:
        frame_count = 0
    if frame_count <= 0 and duration > 0 and fps > 0:
        frame_count = int(round(duration * fps))
    return {
        "duration": duration,
        "has_video": any(s.get("codec_type") == "video" for s in streams),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
        "width": int(video_stream.get("width") or 0),
        "height": int(video_stream.get("height") or 0),
        "fps": round(float(fps or 0.0), 3),
        "frame_count": int(frame_count or 0),
    }


def extract_first_frame_bytes(local_path: str, *, format: str = "png") -> bytes:
    fmt = str(format or "png").strip().lower() or "png"
    if fmt not in {"png", "jpg", "jpeg", "webp"}:
        fmt = "png"
    cmd = [
        "ffmpeg", "-y", "-i", str(local_path), "-frames:v", "1", "-f", "image2pipe", "-vcodec",
        ("png" if fmt == "png" else ("mjpeg" if fmt in {"jpg", "jpeg"} else "webp")), "-"
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        err = (proc.stderr.decode('utf-8', 'ignore') if proc.stderr else 'ffmpeg failed').strip()
        raise RuntimeError(err[:4000])
    return bytes(proc.stdout)


def _download_remote_bytes(url: str) -> bytes:
    timeout = httpx.Timeout(connect=20.0, read=300.0, write=60.0, pool=60.0)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.content


def create_workspace_upload_record(*, user_id: int, filename: str, content_type: str, raw_bytes: bytes) -> Dict[str, Any]:
    kind, ext, normalized_ctype = _detect_file_kind(filename, content_type)
    _assert_size(kind, raw_bytes)
    upload_id = str(uuid4())
    storage_path = _storage_path_for_upload(user_id, upload_id, filename, ext)
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}") as tmp:
            tmp.write(raw_bytes)
            tmp_path = tmp.name
        meta = probe_media(tmp_path)
        if kind == "video" and not meta["has_video"]:
            raise ValueError("Файл не содержит видеодорожку.")
        if kind == "audio" and not meta["has_audio"]:
            raise ValueError("Файл не содержит аудиодорожку.")
        if meta["duration"] <= 0:
            raise ValueError("Не удалось определить длительность файла.")
        _upload_storage_bytes(storage_path, raw_bytes, normalized_ctype)
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    row = {
        "id": upload_id,
        "user_id": str(user_id),
        "file_type": kind,
        "filename": sanitize_filename(filename),
        "storage_path": storage_path,
        "mime_type": normalized_ctype,
        "size_bytes": len(raw_bytes),
        "duration_sec": round(float(meta["duration"]), 3),
        "status": "ready",
        "is_used": False,
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=UPLOAD_TTL_HOURS)).isoformat(),
    }
    if supabase is not None:
        supabase.table(WORKSPACE_VIDEO_UPLOADS_TABLE).insert(row).execute()
    access = build_workspace_video_access_urls(storage_path=storage_path, fallback_url=None, expires_in=3600)
    out = dict(row)
    out.update(access)
    out.update({
        "width": int(meta.get("width") or 0),
        "height": int(meta.get("height") or 0),
        "fps": float(meta.get("fps") or 0.0),
        "frame_count": int(meta.get("frame_count") or 0),
    })
    return out


def get_workspace_generation_row(user_id: int, generation_id: str) -> Optional[Dict[str, Any]]:
    if supabase is None:
        return None
    resp = supabase.table(WORKSPACE_VIDEO_GENERATIONS_TABLE).select("*").eq("id", str(generation_id)).eq("user_id", str(user_id)).limit(1).execute()
    rows = getattr(resp, "data", None) or []
    return rows[0] if rows and isinstance(rows[0], dict) else None


def get_workspace_upload_row(user_id: int, upload_id: str) -> Optional[Dict[str, Any]]:
    if supabase is None:
        return None
    resp = supabase.table(WORKSPACE_VIDEO_UPLOADS_TABLE).select("*").eq("id", str(upload_id)).eq("user_id", str(user_id)).limit(1).execute()
    rows = getattr(resp, "data", None) or []
    return rows[0] if rows and isinstance(rows[0], dict) else None


def get_workspace_edit_job_row(user_id: int, job_id: str) -> Optional[Dict[str, Any]]:
    if supabase is None:
        return None
    resp = supabase.table(WORKSPACE_VIDEO_EDIT_JOBS_TABLE).select("*").eq("id", str(job_id)).eq("user_id", str(user_id)).limit(1).execute()
    rows = getattr(resp, "data", None) or []
    return rows[0] if rows and isinstance(rows[0], dict) else None


def insert_workspace_edit_job_row(row: Dict[str, Any]) -> str:
    job_id = str(row.get("id") or uuid4())
    payload = dict(row)
    payload["id"] = job_id
    if supabase is not None:
        supabase.table(WORKSPACE_VIDEO_EDIT_JOBS_TABLE).insert(payload).execute()
    return job_id


def update_workspace_edit_job_row(job_id: str, patch: Dict[str, Any]) -> None:
    if not job_id or not patch or supabase is None:
        return
    patch = dict(patch)
    patch["updated_at"] = utc_now_iso()
    supabase.table(WORKSPACE_VIDEO_EDIT_JOBS_TABLE).update(patch).eq("id", str(job_id)).execute()


def update_workspace_generation_row(generation_id: str, patch: Dict[str, Any]) -> None:
    if not generation_id or not patch or supabase is None:
        return
    supabase.table(WORKSPACE_VIDEO_GENERATIONS_TABLE).update(patch).eq("id", str(generation_id)).execute()


def mark_uploads_used(upload_ids: List[str]) -> None:
    if not upload_ids or supabase is None:
        return
    ids = [str(x).strip() for x in upload_ids if str(x or "").strip()]
    if ids:
        supabase.table(WORKSPACE_VIDEO_UPLOADS_TABLE).update({"is_used": True, "last_used_at": utc_now_iso(), "updated_at": utc_now_iso()}).in_("id", ids).execute()


def resolve_operation_type(payload: Dict[str, Any]) -> str:
    timeline = (payload or {}).get("timeline") or {}
    trim_enabled = bool(((timeline.get("trim") or {}).get("enabled")))
    mute = bool(((timeline.get("original_audio") or {}).get("mute")))
    volume = int((timeline.get("original_audio") or {}).get("volume") or 100)
    audio_clips = [x for x in (timeline.get("audio_clips") or []) if isinstance(x, dict)]
    merge_items = [x for x in (timeline.get("merge_items") or []) if isinstance(x, dict)]
    flags = []
    if merge_items:
        flags.append("merge")
    if trim_enabled:
        flags.append("trim")
    if mute or volume != 100:
        flags.append("mute" if mute or volume <= 0 else "audio_mix")
    if audio_clips:
        flags.append("audio_mix")
    uniq = sorted(set(flags))
    if not uniq:
        return "composite"
    return uniq[0] if len(uniq) == 1 else "composite"


def _write_temp_file(raw_bytes: bytes, suffix: str, temp_dir: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix, dir=temp_dir)
    os.close(fd)
    with open(path, "wb") as fh:
        fh.write(raw_bytes)
    return path


def _download_generation_to_temp(row: Dict[str, Any], temp_dir: str) -> str:
    storage_path = str(row.get("storage_path") or "").strip()
    provider_url = str(row.get("provider_video_url") or "").strip()
    mime_type = str(row.get("mime_type") or "video/mp4")
    ext = _storage_content_type_to_ext(mime_type, provider_url or storage_path)
    if storage_path:
        return _write_temp_file(_download_storage_bytes(storage_path), f".{ext}", temp_dir)
    if provider_url:
        return _write_temp_file(_download_remote_bytes(provider_url), f".{ext}", temp_dir)
    raise RuntimeError("У исходного видео нет файла в storage и нет provider URL.")


def _download_upload_to_temp(row: Dict[str, Any], temp_dir: str) -> str:
    storage_path = str(row.get("storage_path") or "").strip()
    if not storage_path:
        raise RuntimeError("Upload storage_path is empty.")
    ext = _storage_content_type_to_ext(str(row.get("mime_type") or ""), storage_path)
    return _write_temp_file(_download_storage_bytes(storage_path), f".{ext}", temp_dir)


def _normalize_video_for_concat(src_path: str, out_path: str) -> None:
    meta = probe_media(src_path)
    if meta["has_audio"]:
        cmd = ["ffmpeg", "-y", "-i", src_path, "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p", "-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", out_path]
    else:
        cmd = ["ffmpeg", "-y", "-i", src_path, "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000", "-shortest", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p", "-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", out_path]
    _run(cmd)


def _concat_videos(paths: List[str], out_path: str, temp_dir: str) -> None:
    if not paths:
        raise RuntimeError("Нет видео для склейки.")
    if len(paths) == 1:
        shutil.copyfile(paths[0], out_path)
        return
    list_path = Path(temp_dir) / "concat_list.txt"
    with open(list_path, "w", encoding="utf-8") as fh:
        for p in paths:
            fh.write(f"file '{str(p).replace("'", "'\\''")}'\n")
    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path), "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", out_path]
    _run(cmd)


def _trim_video(src_path: str, out_path: str, start_sec: float, end_sec: float) -> None:
    cmd = ["ffmpeg", "-y", "-ss", f"{max(0.0, float(start_sec)):.3f}", "-to", f"{max(0.0, float(end_sec)):.3f}", "-i", src_path, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", out_path]
    _run(cmd)


def _copy_video(src_path: str, out_path: str) -> None:
    shutil.copyfile(src_path, out_path)


def _apply_audio_pipeline(*, video_path: str, output_path: str, original_audio: Dict[str, Any], audio_clips: List[Dict[str, Any]]) -> None:
    meta = probe_media(video_path)
    has_original_audio = bool(meta.get("has_audio"))
    mute = bool((original_audio or {}).get("mute"))
    volume_pct = max(0, min(100, int((original_audio or {}).get("volume") or 100)))
    if volume_pct == 0:
        mute = True

    if not audio_clips:
        if mute:
            if has_original_audio:
                _run(["ffmpeg", "-y", "-i", video_path, "-map", "0:v:0", "-c:v", "copy", "-an", "-movflags", "+faststart", output_path])
            else:
                _copy_video(video_path, output_path)
            return
        if has_original_audio and volume_pct != 100:
            _run(["ffmpeg", "-y", "-i", video_path, "-filter:a", f"volume={volume_pct / 100.0:.4f}", "-c:v", "copy", "-c:a", "aac", "-movflags", "+faststart", output_path])
            return
        _copy_video(video_path, output_path)
        return

    inputs = ["ffmpeg", "-y", "-i", video_path]
    filter_parts: List[str] = []
    mix_labels: List[str] = []

    if has_original_audio and not mute:
        filter_parts.append(f"[0:a]volume={volume_pct / 100.0:.4f}[orig]")
        mix_labels.append("orig")

    for idx, clip in enumerate(audio_clips, start=1):
        inputs.extend(["-i", str(clip["local_path"])])
        audio_start = max(0.0, float(clip.get("audio_start") or 0.0))
        audio_end = max(audio_start + 0.05, float(clip.get("audio_end") or audio_start + 0.05))
        video_start = max(0.0, float(clip.get("video_start") or 0.0))
        clip_volume = max(0, min(100, int(clip.get("volume") or 100)))
        delay_ms = int(round(video_start * 1000.0))
        label = f"clip{idx}"
        filter_parts.append(f"[{idx}:a]atrim=start={audio_start:.3f}:end={audio_end:.3f},asetpts=PTS-STARTPTS,adelay={delay_ms}|{delay_ms},volume={clip_volume / 100.0:.4f}[{label}]")
        mix_labels.append(label)

    if len(mix_labels) == 1:
        filter_parts.append(f"[{mix_labels[0]}]anull[mixout]")
    else:
        joined = "".join(f"[{label}]" for label in mix_labels)
        filter_parts.append(f"{joined}amix=inputs={len(mix_labels)}:duration=longest:dropout_transition=0[mixout]")

    cmd = inputs + ["-filter_complex", ";".join(filter_parts), "-map", "0:v:0", "-map", "[mixout]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", "-shortest", output_path]
    _run(cmd)


def _load_job_payload(job_row: Dict[str, Any]) -> Dict[str, Any]:
    value = job_row.get("payload_json")
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return {}
    return {}


def _safe_aspect_ratio(width: int, height: int, fallback: str = "16:9") -> str:
    if not width or not height:
        return fallback
    ratios = {"16:9": 16 / 9, "9:16": 9 / 16, "1:1": 1.0}
    current = width / height
    return min(ratios.items(), key=lambda x: abs(x[1] - current))[0]


def _build_generation_prompt(source_row: Dict[str, Any], operation_type: str) -> str:
    base = _trim_text(source_row.get("prompt") or "", 120)
    suffix_map = {"trim": "Trim", "mute": "Mute", "audio_mix": "Audio mix", "merge": "Merge", "composite": "Montage"}
    suffix = suffix_map.get(operation_type, "Edit")
    return f"{suffix} · {base}" if base else f"{suffix} · Edited video"


def process_workspace_video_edit_job(job_id: str) -> None:
    if supabase is None:
        raise RuntimeError("Supabase client is not configured")
    resp = supabase.table(WORKSPACE_VIDEO_EDIT_JOBS_TABLE).select("*").eq("id", str(job_id)).limit(1).execute()
    rows = getattr(resp, "data", None) or []
    if not rows or not isinstance(rows[0], dict):
        raise RuntimeError("Edit job not found.")
    job_row = rows[0]
    user_id = int(job_row["user_id"])
    generation_id = str(job_row.get("result_generation_id") or "").strip()
    payload = _load_job_payload(job_row)
    timeline = payload.get("timeline") or {}
    source_generation_id = str(payload.get("source_generation_id") or job_row.get("source_generation_id") or "").strip()
    source_row = get_workspace_generation_row(user_id, source_generation_id)
    if not source_row:
        raise RuntimeError("Source video not found.")

    update_workspace_edit_job_row(job_id, {"status": "processing", "started_at": utc_now_iso(), "error_message": None})
    if generation_id:
        update_workspace_generation_row(generation_id, {"status": "processing", "error_message": None, "error_code": None})

    temp_dir = tempfile.mkdtemp(prefix="workspace_video_edit_")
    used_upload_ids: List[str] = []
    try:
        merge_items = [x for x in (timeline.get("merge_items") or []) if isinstance(x, dict)]
        if merge_items:
            if len(merge_items) > MAX_MERGE_ITEMS:
                raise RuntimeError(f"Максимум {MAX_MERGE_ITEMS} видео в очереди склейки.")
            normalized_paths: List[str] = []
            for index, item in enumerate(merge_items):
                item_type = str(item.get("type") or "").strip().lower()
                item_id = str(item.get("id") or "").strip()
                if item_type == "generation":
                    row = get_workspace_generation_row(user_id, item_id)
                    if not row:
                        raise RuntimeError(f"Видео из библиотеки не найдено: {item_id}")
                    local_src = _download_generation_to_temp(row, temp_dir)
                elif item_type == "upload":
                    row = get_workspace_upload_row(user_id, item_id)
                    if not row or str(row.get("file_type") or "") != "video":
                        raise RuntimeError(f"Загруженное видео не найдено: {item_id}")
                    local_src = _download_upload_to_temp(row, temp_dir)
                    used_upload_ids.append(item_id)
                else:
                    raise RuntimeError("Неверный элемент merge_items.")
                normalized = str(Path(temp_dir) / f"merge_norm_{index}.mp4")
                _normalize_video_for_concat(local_src, normalized)
                normalized_paths.append(normalized)
            base_video = str(Path(temp_dir) / "base_merge.mp4")
            _concat_videos(normalized_paths, base_video, temp_dir)
        else:
            base_video = _download_generation_to_temp(source_row, temp_dir)

        base_meta = probe_media(base_video)
        base_duration = float(base_meta.get("duration") or 0.0)
        if base_duration <= 0:
            raise RuntimeError("Не удалось определить длительность исходного видео.")
        if base_duration > MAX_OUTPUT_DURATION_SEC:
            raise RuntimeError(f"Длительность исходного видео превышает лимит {MAX_OUTPUT_DURATION_SEC} сек.")

        trim_cfg = timeline.get("trim") or {}
        current_video = base_video
        if bool(trim_cfg.get("enabled")):
            start_sec = float(trim_cfg.get("start_sec") or 0.0)
            end_sec = float(trim_cfg.get("end_sec") or 0.0)
            if start_sec < 0 or end_sec <= start_sec:
                raise RuntimeError("Неверный диапазон trim: start должен быть меньше end.")
            if (end_sec - start_sec) < 0.5:
                raise RuntimeError("Минимальная длина результата после trim — 0.5 сек.")
            if end_sec > base_duration + 0.01:
                raise RuntimeError("Trim выходит за пределы длительности ролика.")
            trimmed = str(Path(temp_dir) / "trimmed.mp4")
            _trim_video(base_video, trimmed, start_sec, end_sec)
            current_video = trimmed

        current_meta = probe_media(current_video)
        current_duration = float(current_meta.get("duration") or 0.0)
        if current_duration <= 0:
            raise RuntimeError("После обработки длительность ролика стала некорректной.")
        if current_duration > MAX_OUTPUT_DURATION_SEC:
            raise RuntimeError(f"Итоговая длительность превышает лимит {MAX_OUTPUT_DURATION_SEC} сек.")

        audio_clips_payload = [x for x in (timeline.get("audio_clips") or []) if isinstance(x, dict)]
        if len(audio_clips_payload) > MAX_AUDIO_CLIPS:
            raise RuntimeError(f"Максимум {MAX_AUDIO_CLIPS} аудио-куска.")
        audio_clips: List[Dict[str, Any]] = []
        for item in audio_clips_payload:
            upload_id = str(item.get("upload_id") or "").strip()
            row = get_workspace_upload_row(user_id, upload_id)
            if not row or str(row.get("file_type") or "") != "audio":
                raise RuntimeError(f"Аудиофайл не найден: {upload_id}")
            local_audio = _download_upload_to_temp(row, temp_dir)
            used_upload_ids.append(upload_id)
            audio_meta = probe_media(local_audio)
            audio_duration = float(audio_meta.get("duration") or 0.0)
            audio_start = float(item.get("audio_start") or 0.0)
            audio_end = float(item.get("audio_end") or 0.0)
            video_start = float(item.get("video_start") or 0.0)
            volume = max(0, min(100, int(item.get("volume") or 100)))
            if audio_start < 0 or audio_end <= audio_start:
                raise RuntimeError("Неверный диапазон аудио-куска.")
            if audio_end > audio_duration + 0.01:
                raise RuntimeError("Аудио-кусок выходит за пределы длительности аудиофайла.")
            if video_start < 0 or video_start > current_duration + 0.01:
                raise RuntimeError("Позиция вставки аудио выходит за пределы видео.")
            audio_clips.append({"local_path": local_audio, "audio_start": audio_start, "audio_end": audio_end, "video_start": min(video_start, current_duration), "volume": volume})

        final_video = str(Path(temp_dir) / "final_output.mp4")
        _apply_audio_pipeline(video_path=current_video, output_path=final_video, original_audio=timeline.get("original_audio") or {"mute": False, "volume": 100}, audio_clips=audio_clips)

        final_meta = probe_media(final_video)
        final_duration = float(final_meta.get("duration") or 0.0)
        if final_duration <= 0:
            raise RuntimeError("Не удалось собрать итоговое видео.")
        if final_duration > MAX_OUTPUT_DURATION_SEC + 1:
            raise RuntimeError(f"Итоговое видео превышает лимит {MAX_OUTPUT_DURATION_SEC} сек.")

        with open(final_video, "rb") as fh:
            final_bytes = fh.read()
        output_storage_path = _storage_path_for_edited_video(user_id, generation_id or str(uuid4()), "mp4")
        _upload_storage_bytes(output_storage_path, final_bytes, "video/mp4")
        access = build_workspace_video_access_urls(storage_path=output_storage_path, fallback_url=None, expires_in=3600)
        operation_type = resolve_operation_type(payload)
        if generation_id:
            update_workspace_generation_row(generation_id, {
                "status": "completed",
                "provider": "editor",
                "model": "mini-editor-v1",
                "mode": "edit",
                "prompt": _build_generation_prompt(source_row, operation_type),
                "duration_sec": int(round(final_duration)),
                "aspect_ratio": _safe_aspect_ratio(int(final_meta.get("width") or 0), int(final_meta.get("height") or 0), str(source_row.get("aspect_ratio") or "16:9")),
                "resolution": source_row.get("resolution") or None,
                "enable_audio": bool(final_meta.get("has_audio")),
                "provider_video_url": access.get("video_url"),
                "storage_path": output_storage_path,
                "file_size_bytes": len(final_bytes),
                "mime_type": "video/mp4",
                "origin": "workspace_edit",
                "completed_at": utc_now_iso(),
                "error_code": None,
                "error_message": None,
                "parent_generation_id": source_generation_id or None,
                "operation_type": operation_type,
                "operations_json": payload,
                "edit_job_id": job_id,
            })
        mark_uploads_used(sorted(set(used_upload_ids)))
        update_workspace_edit_job_row(job_id, {"status": "completed", "completed_at": utc_now_iso(), "error_message": None, "result_generation_id": generation_id or None, "result_storage_path": output_storage_path})
    except Exception as exc:
        message = _trim_text(str(exc), 3900) or "Video edit failed"
        if generation_id:
            update_workspace_generation_row(generation_id, {"status": "failed", "error_code": "edit_error", "error_message": message, "edit_job_id": job_id})
        update_workspace_edit_job_row(job_id, {"status": "failed", "completed_at": utc_now_iso(), "error_message": message})
        raise
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
