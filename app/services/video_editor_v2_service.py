from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from db_supabase import supabase
from app.services.video_editor_service import (
    WORKSPACE_VIDEO_GENERATIONS_TABLE,
    WORKSPACE_VIDEO_UPLOADS_TABLE,
    WORKSPACE_VIDEOS_BUCKET,
    _download_generation_to_temp,
    _download_upload_to_temp,
    _normalize_video_for_concat,
    _concat_videos,
    _trim_video,
    _apply_audio_pipeline,
    _upload_storage_bytes,
    _storage_path_for_edited_video,
    build_workspace_video_access_urls,
    create_workspace_upload_record,
    get_workspace_generation_row,
    get_workspace_upload_row,
    mark_uploads_used,
    probe_media,
    sanitize_filename,
    utc_now_iso,
)

VIDEO_EDITOR_PROJECTS_TABLE = "video_editor_projects"
VIDEO_EDITOR_RENDER_JOBS_TABLE = "video_editor_render_jobs"
VIDEO_EDITOR_QUEUE_NAME = (os.getenv("VIDEO_EDITOR_V2_QUEUE", "video_editor_v2") or "video_editor_v2").strip() or "video_editor_v2"
MAX_PROJECT_CLIPS = int(os.getenv("VIDEO_EDITOR_V2_MAX_CLIPS", "5") or 5)
MAX_PROJECT_AUDIO_TRACKS = int(os.getenv("VIDEO_EDITOR_V2_MAX_AUDIO_TRACKS", "8") or 8)
MAX_RENDER_DURATION_SEC = int(os.getenv("VIDEO_EDITOR_V2_MAX_DURATION_SEC", "300") or 300)

_ALLOWED_TRANSITIONS = {"none", "fade", "dissolve", "slideleft", "slideright", "zoomin"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _project_defaults(title: str = "Новый видеопроект") -> Dict[str, Any]:
    return {
        "title": title,
        "canvas": {"width": 1080, "height": 1920, "fps": 30},
        "video_clips": [],
        "audio_tracks": [],
        "text_overlays": [],
        "stickers": [],
        "updated_at": _now_iso(),
    }


def _normalize_clip(item: Dict[str, Any], index: int) -> Dict[str, Any]:
    source_type = str(item.get("source_type") or "generation").strip().lower()
    source_id = str(item.get("source_id") or "").strip()
    if source_type not in {"generation", "upload"}:
        raise ValueError(f"clip[{index}]: source_type должен быть generation или upload")
    if not source_id:
        raise ValueError(f"clip[{index}]: пустой source_id")

    start_sec = max(0.0, float(item.get("source_start") or 0.0))
    end_sec = float(item.get("source_end") or 0.0)
    if end_sec > 0 and end_sec <= start_sec:
        raise ValueError(f"clip[{index}]: source_end должен быть больше source_start")

    transition = item.get("transition") or {}
    transition_type = str(transition.get("type") or "none").strip().lower() or "none"
    if transition_type not in _ALLOWED_TRANSITIONS:
        transition_type = "none"
    transition_duration = max(0.0, min(1.0, float(transition.get("duration") or 0.0)))

    return {
        "id": str(item.get("id") or uuid4()),
        "source_type": source_type,
        "source_id": source_id,
        "label": str(item.get("label") or f"Клип {index + 1}").strip() or f"Клип {index + 1}",
        "source_start": round(start_sec, 3),
        "source_end": round(end_sec, 3),
        "muted": bool(item.get("muted", False)),
        "volume": max(0, min(100, int(item.get("volume") or 100))),
        "filter": str(item.get("filter") or "none").strip() or "none",
        "effect": str(item.get("effect") or "none").strip() or "none",
        "transition": {"type": transition_type, "duration": round(transition_duration, 3)},
    }


def _normalize_audio_track(item: Dict[str, Any], index: int) -> Dict[str, Any]:
    source_id = str(item.get("source_id") or "").strip()
    if not source_id:
        raise ValueError(f"audio[{index}]: пустой source_id")
    return {
        "id": str(item.get("id") or uuid4()),
        "source_id": source_id,
        "label": str(item.get("label") or f"Аудио {index + 1}").strip() or f"Аудио {index + 1}",
        "timeline_start": max(0.0, float(item.get("timeline_start") or 0.0)),
        "source_start": max(0.0, float(item.get("source_start") or 0.0)),
        "source_end": max(0.0, float(item.get("source_end") or 0.0)),
        "volume": max(0, min(100, int(item.get("volume") or 100))),
    }


def normalize_project_json(project_json: Optional[Dict[str, Any]], title: str = "Новый видеопроект") -> Dict[str, Any]:
    payload = dict(_project_defaults(title))
    if isinstance(project_json, dict):
        payload.update({k: v for k, v in project_json.items() if k in {"title", "canvas", "text_overlays", "stickers"}})

    raw_clips = list((project_json or {}).get("video_clips") or []) if isinstance(project_json, dict) else []
    if len(raw_clips) > MAX_PROJECT_CLIPS:
        raise ValueError(f"Максимум {MAX_PROJECT_CLIPS} видео-клипов в проекте")
    payload["video_clips"] = [_normalize_clip(item, idx) for idx, item in enumerate(raw_clips)]

    raw_audio = list((project_json or {}).get("audio_tracks") or []) if isinstance(project_json, dict) else []
    if len(raw_audio) > MAX_PROJECT_AUDIO_TRACKS:
        raise ValueError(f"Максимум {MAX_PROJECT_AUDIO_TRACKS} музыкальная дорожка")
    payload["audio_tracks"] = [_normalize_audio_track(item, idx) for idx, item in enumerate(raw_audio)]
    payload["updated_at"] = _now_iso()
    return payload


def _table_select_one(table: str, **where: Any) -> Optional[Dict[str, Any]]:
    if supabase is None:
        return None
    query = supabase.table(table).select("*")
    for key, value in where.items():
        query = query.eq(key, value)
    resp = query.limit(1).execute()
    rows = getattr(resp, "data", None) or []
    return rows[0] if rows and isinstance(rows[0], dict) else None


def list_projects(user_id: int) -> List[Dict[str, Any]]:
    if supabase is None:
        return []
    resp = (
        supabase.table(VIDEO_EDITOR_PROJECTS_TABLE)
        .select("id,user_id,title,status,thumbnail_path,project_json,last_render_job_id,created_at,updated_at")
        .eq("user_id", str(user_id))
        .order("updated_at", desc=True)
        .limit(100)
        .execute()
    )
    return getattr(resp, "data", None) or []


def get_project(user_id: int, project_id: str) -> Optional[Dict[str, Any]]:
    return _table_select_one(VIDEO_EDITOR_PROJECTS_TABLE, id=str(project_id), user_id=str(user_id))


def create_project(user_id: int, title: str, project_json: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if supabase is None:
        raise RuntimeError("Supabase client is not configured")
    normalized = normalize_project_json(project_json, title=title)
    project_id = str(uuid4())
    row = {
        "id": project_id,
        "user_id": str(user_id),
        "title": str(title or "Новый видеопроект").strip() or "Новый видеопроект",
        "status": "draft",
        "project_json": normalized,
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
    }
    supabase.table(VIDEO_EDITOR_PROJECTS_TABLE).insert(row).execute()
    return row


def update_project(user_id: int, project_id: str, title: Optional[str], project_json: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if supabase is None:
        raise RuntimeError("Supabase client is not configured")
    current = get_project(user_id, project_id)
    if not current:
        raise ValueError("Project not found")
    normalized = normalize_project_json(project_json or current.get("project_json") or {}, title=title or current.get("title") or "Проект")
    patch = {
        "title": str(title or current.get("title") or "Проект").strip() or "Проект",
        "project_json": normalized,
        "updated_at": utc_now_iso(),
    }
    supabase.table(VIDEO_EDITOR_PROJECTS_TABLE).update(patch).eq("id", str(project_id)).eq("user_id", str(user_id)).execute()
    return get_project(user_id, project_id) or {**current, **patch}


def delete_project(user_id: int, project_id: str) -> None:
    if supabase is None:
        raise RuntimeError("Supabase client is not configured")
    supabase.table(VIDEO_EDITOR_PROJECTS_TABLE).delete().eq("id", str(project_id)).eq("user_id", str(user_id)).execute()


def create_render_job(user_id: int, project_id: str) -> Dict[str, Any]:
    if supabase is None:
        raise RuntimeError("Supabase client is not configured")
    project = get_project(user_id, project_id)
    if not project:
        raise ValueError("Project not found")
    job_id = str(uuid4())
    row = {
        "id": job_id,
        "user_id": str(user_id),
        "project_id": str(project_id),
        "status": "queued",
        "progress": 0,
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
    }
    supabase.table(VIDEO_EDITOR_RENDER_JOBS_TABLE).insert(row).execute()
    supabase.table(VIDEO_EDITOR_PROJECTS_TABLE).update({"status": "rendering", "last_render_job_id": job_id, "updated_at": utc_now_iso()}).eq("id", str(project_id)).eq("user_id", str(user_id)).execute()
    return row


def get_render_job(user_id: int, job_id: str) -> Optional[Dict[str, Any]]:
    return _table_select_one(VIDEO_EDITOR_RENDER_JOBS_TABLE, id=str(job_id), user_id=str(user_id))


def update_render_job(job_id: str, patch: Dict[str, Any]) -> None:
    if not patch or supabase is None:
        return
    payload = dict(patch)
    payload["updated_at"] = utc_now_iso()
    supabase.table(VIDEO_EDITOR_RENDER_JOBS_TABLE).update(payload).eq("id", str(job_id)).execute()


def _resolve_clip_source(user_id: int, clip: Dict[str, Any], temp_dir: str) -> tuple[str, Dict[str, Any], Optional[str]]:
    source_type = str(clip.get("source_type") or "generation")
    source_id = str(clip.get("source_id") or "")
    used_upload_id: Optional[str] = None
    if source_type == "generation":
        row = get_workspace_generation_row(user_id, source_id)
        if not row:
            raise RuntimeError(f"Видео не найдено: {source_id}")
        local_path = _download_generation_to_temp(row, temp_dir)
    else:
        row = get_workspace_upload_row(user_id, source_id)
        if not row or str(row.get("file_type") or "") != "video":
            raise RuntimeError(f"Загруженное видео не найдено: {source_id}")
        local_path = _download_upload_to_temp(row, temp_dir)
        used_upload_id = source_id
    return local_path, row, used_upload_id


def _resolve_audio_source(user_id: int, item: Dict[str, Any], temp_dir: str) -> tuple[str, float, Optional[str]]:
    source_id = str(item.get("source_id") or "")
    row = get_workspace_upload_row(user_id, source_id)
    if not row or str(row.get("file_type") or "") != "audio":
        raise RuntimeError(f"Аудиофайл не найден: {source_id}")
    local_path = _download_upload_to_temp(row, temp_dir)
    meta = probe_media(local_path)
    return local_path, float(meta.get("duration") or 0.0), source_id


def _build_clip_file(user_id: int, clip: Dict[str, Any], index: int, temp_dir: str) -> tuple[str, float, Optional[str]]:
    local_src, _row, used_upload_id = _resolve_clip_source(user_id, clip, temp_dir)
    meta = probe_media(local_src)
    duration = float(meta.get("duration") or 0.0)
    if duration <= 0:
        raise RuntimeError(f"Клип {index + 1}: не удалось определить длительность")

    start_sec = max(0.0, float(clip.get("source_start") or 0.0))
    end_sec = float(clip.get("source_end") or 0.0)
    if end_sec <= 0 or end_sec > duration:
        end_sec = duration
    if end_sec <= start_sec:
        raise RuntimeError(f"Клип {index + 1}: неверный диапазон trim")

    trimmed = str(Path(temp_dir) / f"clip_trim_{index}.mp4")
    _trim_video(local_src, trimmed, start_sec, end_sec)

    normalized = str(Path(temp_dir) / f"clip_norm_{index}.mp4")
    _normalize_video_for_concat(trimmed, normalized)
    meta2 = probe_media(normalized)
    out_duration = float(meta2.get("duration") or 0.0)
    return normalized, out_duration, used_upload_id


def _render_concat_only(paths: List[str], temp_dir: str) -> str:
    output = str(Path(temp_dir) / "concat_output.mp4")
    _concat_videos(paths, output, temp_dir)
    return output


def _render_with_xfade(paths: List[str], transitions: List[Dict[str, Any]], temp_dir: str) -> str:
    if len(paths) == 1:
        return paths[0]

    work_files = paths[:]
    xfade_map = {
        "fade": "fade",
        "dissolve": "fade",
        "slideleft": "slideleft",
        "slideright": "slideright",
        "zoomin": "zoomin",
    }

    for idx in range(1, len(work_files)):
        first = work_files[idx - 1]
        second = work_files[idx]
        tr = transitions[idx - 1] if idx - 1 < len(transitions) else {"type": "none", "duration": 0.0}
        tr_type = str((tr or {}).get("type") or "none").lower()
        tr_duration = max(0.0, min(1.0, float((tr or {}).get("duration") or 0.0)))
        if tr_type == "none" or tr_duration <= 0:
            continue
        first_meta = probe_media(first)
        first_duration = float(first_meta.get("duration") or 0.0)
        offset = max(0.0, first_duration - tr_duration)
        merged = str(Path(temp_dir) / f"xfade_{idx}.mp4")
        transition_name = xfade_map.get(tr_type, "fade")
        cmd = [
            "ffmpeg", "-y",
            "-i", first,
            "-i", second,
            "-filter_complex",
            f"[0:v][1:v]xfade=transition={transition_name}:duration={tr_duration:.3f}:offset={offset:.3f}[v];"
            f"[0:a][1:a]acrossfade=d={tr_duration:.3f}[a]",
            "-map", "[v]",
            "-map", "[a]",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-c:a", "aac",
            "-ar", "48000",
            "-ac", "2",
            "-movflags", "+faststart",
            merged,
        ]
        from app.services.video_editor_service import _run
        _run(cmd)
        work_files[idx] = merged
        work_files[idx - 1] = merged
    return work_files[-1]


def process_render_job(job_id: str) -> None:
    if supabase is None:
        raise RuntimeError("Supabase client is not configured")
    job = _table_select_one(VIDEO_EDITOR_RENDER_JOBS_TABLE, id=str(job_id))
    if not job:
        raise RuntimeError("Render job not found")
    project = _table_select_one(VIDEO_EDITOR_PROJECTS_TABLE, id=str(job.get("project_id") or ""), user_id=str(job.get("user_id") or ""))
    if not project:
        raise RuntimeError("Project not found")

    user_id = int(project["user_id"])
    project_json = normalize_project_json(project.get("project_json") or {}, title=project.get("title") or "Проект")
    clips = project_json.get("video_clips") or []
    if not clips:
        raise RuntimeError("В проекте нет видео-клипов")
    if len(clips) > MAX_PROJECT_CLIPS:
        raise RuntimeError(f"Максимум {MAX_PROJECT_CLIPS} клипов")

    update_render_job(job_id, {"status": "processing", "progress": 5, "started_at": utc_now_iso(), "error_text": None})
    temp_dir = tempfile.mkdtemp(prefix="video_editor_v2_")
    used_upload_ids: List[str] = []
    try:
        built_paths: List[str] = []
        transitions: List[Dict[str, Any]] = []
        total_duration = 0.0
        for idx, clip in enumerate(clips):
            path, clip_duration, used_upload_id = _build_clip_file(user_id, clip, idx, temp_dir)
            built_paths.append(path)
            transitions.append((clip.get("transition") or {}) if idx > 0 else {"type": "none", "duration": 0.0})
            total_duration += clip_duration
            if used_upload_id:
                used_upload_ids.append(used_upload_id)
            update_render_job(job_id, {"progress": 15 + int((idx + 1) / max(1, len(clips)) * 35)})

        if total_duration > MAX_RENDER_DURATION_SEC:
            raise RuntimeError(f"Итоговая длительность проекта превышает {MAX_RENDER_DURATION_SEC} сек")

        has_transition = any((tr or {}).get("type") not in {None, "", "none"} and float((tr or {}).get("duration") or 0.0) > 0 for tr in transitions[1:])
        if has_transition:
            base_video = _render_with_xfade(built_paths, transitions[1:], temp_dir)
        else:
            base_video = _render_concat_only(built_paths, temp_dir)
        update_render_job(job_id, {"progress": 70})

        project_audio = project_json.get("audio_tracks") or []
        audio_clips: List[Dict[str, Any]] = []
        for item in project_audio:
            local_audio, audio_duration, used_upload_id = _resolve_audio_source(user_id, item, temp_dir)
            used_upload_ids.append(used_upload_id or "")
            source_start = max(0.0, float(item.get("source_start") or 0.0))
            source_end = float(item.get("source_end") or 0.0)
            if source_end <= 0 or source_end > audio_duration:
                source_end = audio_duration
            audio_clips.append({
                "local_path": local_audio,
                "audio_start": source_start,
                "audio_end": source_end,
                "video_start": max(0.0, float(item.get("timeline_start") or 0.0)),
                "volume": max(0, min(100, int(item.get("volume") or 100))),
            })

        final_video = str(Path(temp_dir) / "final_video.mp4")
        _apply_audio_pipeline(
            video_path=base_video,
            output_path=final_video,
            original_audio={"mute": False, "volume": 100},
            audio_clips=audio_clips,
        )
        update_render_job(job_id, {"progress": 85})

        final_meta = probe_media(final_video)
        final_duration = float(final_meta.get("duration") or 0.0)
        if final_duration <= 0:
            raise RuntimeError("Не удалось собрать итоговое видео")
        with open(final_video, "rb") as fh:
            video_bytes = fh.read()
        storage_path = _storage_path_for_edited_video(user_id, str(uuid4()), "mp4")
        _upload_storage_bytes(storage_path, video_bytes, "video/mp4")
        access = build_workspace_video_access_urls(storage_path=storage_path, fallback_url=None, expires_in=3600)
        update_render_job(job_id, {
            "status": "completed",
            "progress": 100,
            "completed_at": utc_now_iso(),
            "output_path": storage_path,
            "output_url": access.get("video_url"),
            "duration_sec": round(final_duration, 3),
            "error_text": None,
        })
        supabase.table(VIDEO_EDITOR_PROJECTS_TABLE).update({
            "status": "ready",
            "updated_at": utc_now_iso(),
            "last_output_path": storage_path,
            "thumbnail_path": project.get("thumbnail_path"),
            "last_render_job_id": job_id,
        }).eq("id", str(project.get("id"))).eq("user_id", str(user_id)).execute()
        mark_uploads_used([x for x in used_upload_ids if x])
    except Exception as exc:
        update_render_job(job_id, {"status": "failed", "completed_at": utc_now_iso(), "error_text": str(exc)[:3900]})
        supabase.table(VIDEO_EDITOR_PROJECTS_TABLE).update({"status": "draft", "updated_at": utc_now_iso()}).eq("id", str(project.get("id"))).eq("user_id", str(user_id)).execute()
        raise
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def list_generation_library(user_id: int, limit: int = 50) -> List[Dict[str, Any]]:
    if supabase is None:
        return []
    resp = (
        supabase.table(WORKSPACE_VIDEO_GENERATIONS_TABLE)
        .select("id,prompt,status,provider,model,duration_sec,aspect_ratio,provider_video_url,storage_path,created_at")
        .eq("user_id", str(user_id))
        .in_("status", ["completed", "succeeded", "success"])
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    rows = getattr(resp, "data", None) or []
    out = []
    for row in rows:
        access = build_workspace_video_access_urls(storage_path=row.get("storage_path"), fallback_url=row.get("provider_video_url"), expires_in=3600)
        out.append({**row, **access})
    return out


def list_upload_library(user_id: int, file_type: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    if supabase is None:
        return []
    query = (
        supabase.table(WORKSPACE_VIDEO_UPLOADS_TABLE)
        .select("id,file_type,filename,storage_path,duration_sec,created_at,mime_type")
        .eq("user_id", str(user_id))
        .order("created_at", desc=True)
        .limit(limit)
    )
    if file_type:
        query = query.eq("file_type", str(file_type))
    resp = query.execute()
    rows = getattr(resp, "data", None) or []
    out = []
    for row in rows:
        access = build_workspace_video_access_urls(storage_path=row.get("storage_path"), fallback_url=None, expires_in=3600)
        out.append({**row, **access})
    return out
