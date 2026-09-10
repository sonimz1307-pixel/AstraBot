"""Bridge from immutable Trend recipes to the existing Workspace pipeline.

This module never debits/refunds user tokens. The marketplace transaction owns
its single combined operation; legacy workers receive charge_tokens=0.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional
from uuid import NAMESPACE_URL, uuid5

from app.services.trend_registry import get_model

TREND_ORIGIN = "trend_marketplace"


class TrendGenerationPending(RuntimeError):
    """Known provider task needs polling/recovery, never a second submission."""


class TrendGenerationArchiving(RuntimeError):
    """The video provider is done; only the durable Storage outbox may retry."""


class TrendGenerationUncertain(RuntimeError):
    """Submission outcome is unknown; requires operator reconciliation."""


class TrendGenerationFailed(RuntimeError):
    """The provider explicitly confirmed terminal failure."""


def recipe_from_version(version: Dict[str, Any]) -> Dict[str, Any]:
    return dict(version.get("recipe_json") or version.get("recipe") or {})


def generation_id_for_run(run: Dict[str, Any]) -> str:
    return str(run.get("generation_id") or uuid5(NAMESPACE_URL, "nabex:trend:generation:" + str(run["id"])))


def build_job(run: Dict[str, Any], version: Dict[str, Any], reference_urls: List[str]) -> Dict[str, Any]:
    recipe = recipe_from_version(version)
    spec = get_model(recipe["model_key"])
    settings = dict(recipe["settings"])
    model = "seedance25-" + settings["resolution"] if spec["provider"] == "seedance25" else spec["model"]
    task_id = str((run.get("generation_metadata") or {}).get("provider_task_id") or "")
    job = {
        "job_id": "trend:" + str(run["id"]),
        "generation_id": generation_id_for_run(run), "user_id": int(run["buyer_user_id"]),
        "kind": "workspace_image_run" if spec["type"] == "photo" else "workspace_video_run",
        "provider": spec["provider"], "model": model, "mode": recipe["mode"],
        "prompt": recipe["prompt"], "run_prompt": recipe["prompt"],
        "origin": TREND_ORIGIN, "trend_id": run["trend_id"],
        "trend_version_id": run["trend_version_id"], "trend_run_id": run["id"],
        "charge_tokens": 0, "charge_ref_id": "", "refund_reason": "trend_refund",
        "resume_task_id": task_id, "safety_level": "high", "quality": "pro", "provider_mode": "normal",
        **settings,
    }
    if len(reference_urls) > spec["max_references"]:
        raise ValueError("Too many references")
    if recipe["mode"].startswith("text_to_") and reference_urls:
        raise ValueError("Text-only recipe cannot use references")
    if not recipe["mode"].startswith("text_to_") and not reference_urls:
        raise ValueError("Recipe requires references")
    if spec["type"] == "photo":
        job["source_image_urls"] = list(reference_urls)
        job["source_image_url"] = reference_urls[0] if reference_urls else None
    else:
        job["reference_image_urls"] = list(reference_urls)
    return job


def history_table(version: Dict[str, Any]) -> str:
    return "workspace_image_generations" if get_model(recipe_from_version(version)["model_key"])["type"] == "photo" else "workspace_video_generations"


def get_history(run: Dict[str, Any], version: Dict[str, Any]) -> Dict[str, Any]:
    from app.routers import web_workspace_api as ww
    resp = ww.supabase.table(history_table(version)).select("*").eq("id", generation_id_for_run(run)).eq("user_id", str(run["buyer_user_id"])).limit(1).execute()
    rows = list(getattr(resp, "data", None) or [])
    return dict(rows[0]) if rows else {}


def ensure_history(run: Dict[str, Any], version: Dict[str, Any]) -> Dict[str, Any]:
    """The history row contains no recipe prompt, fixed references or mapping."""
    from app.routers import web_workspace_api as ww
    existing = get_history(run, version)
    if existing:
        if existing.get("origin") != TREND_ORIGIN:
            raise RuntimeError("Generation identity conflict")
        return existing
    recipe = recipe_from_version(version)
    spec = get_model(recipe["model_key"])
    model = "seedance25-" + recipe["settings"]["resolution"] if spec["provider"] == "seedance25" else spec["model"]
    row = {"id": generation_id_for_run(run), "user_id": str(run["buyer_user_id"]),
           "provider": spec["provider"], "model": model, "mode": recipe["mode"],
           "prompt": "", "origin": TREND_ORIGIN, "status": "queued"}
    insert = ww._insert_workspace_image_generation if spec["type"] == "photo" else ww._insert_workspace_generation
    insert(row)
    return row


async def execute_run(run: Dict[str, Any], version: Dict[str, Any], reference_urls: List[str],
                      on_task_id: Optional[Callable[[str], Any]] = None) -> Dict[str, Any]:
    from app.services.workspace_worker_jobs import process_workspace_image_job, process_workspace_video_job
    from gpt_image_2_kie import GptImage2TaskFailedError
    from seedream_5_pro_kie import Seedream5ProTaskFailedError
    job = build_job(run, version, reference_urls)
    history = get_history(run, version)
    if history.get("status") == "completed" and history.get("storage_path"):
        return history
    if video_needs_archiving(history, version):
        raise TrendGenerationArchiving("Video archive pending")
    persisted_task = str(history.get("provider_task_id") or history.get("task_id") or job.get("resume_task_id") or "")
    if persisted_task:
        job["resume_task_id"] = persisted_task
    try:
        if job["kind"] == "workspace_image_run":
            await process_workspace_image_job(job, on_provider_task_id=on_task_id)
        else:
            await process_workspace_video_job(job, on_provider_task_id=on_task_id)
    except (GptImage2TaskFailedError, Seedream5ProTaskFailedError) as exc:
        raise TrendGenerationFailed("Provider confirmed failure") from exc
    except Exception as exc:
        history = get_history(run, version)
        if video_needs_archiving(history, version):
            raise TrendGenerationArchiving("Video archive pending") from exc
        task = str(history.get("provider_task_id") or history.get("task_id") or persisted_task)
        if task:
            raise TrendGenerationPending(task) from exc
        raise TrendGenerationUncertain("Submission outcome requires reconciliation") from exc
    history = get_history(run, version)
    if history.get("status") == "completed" and history.get("storage_path"):
        return history
    if video_needs_archiving(history, version):
        raise TrendGenerationArchiving("Video archive pending")
    # Video terminal provider failures are converted to False by the old worker;
    # a task id proves the failure happened after a definite submission.
    task = str(history.get("provider_task_id") or history.get("task_id") or persisted_task)
    if history.get("status") == "failed" and task:
        raise TrendGenerationFailed("Provider confirmed failure")
    if task:
        raise TrendGenerationPending(task)
    raise TrendGenerationUncertain("Submission outcome requires reconciliation")


def video_needs_archiving(history: Dict[str, Any], version: Dict[str, Any]) -> bool:
    if (history.get("deleted_at") or history.get("status") in {"failed", "cancelled"}
            or not history.get("provider_video_url") or history.get("storage_path")):
        return False
    return get_model(recipe_from_version(version)["model_key"])["type"] == "video"


def redact_history_item(row: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    if row.get("origin") != TREND_ORIGIN:
        return payload
    # An allowlist avoids future optional provider fields silently leaking recipes.
    allowed = {"id", "user_id", "status", "origin", "is_favorite", "created_at", "updated_at", "completed_at",
               "video_url", "image_url", "image_urls", "download_url", "signed_url", "has_storage_file", "mime_type", "file_size_bytes"}
    result = {key: value for key, value in payload.items() if key in allowed}
    result.update({"prompt": "Мастерская трендов", "provider": "trend_marketplace", "model": "Мастерская трендов", "mode": "trend",
                   "error_code": "generation_failed" if row.get("status") == "failed" else None,
                   "error_message": "Не удалось завершить генерацию. Проверьте статус в Мастерской." if row.get("status") == "failed" else None})
    return result


def assert_history_deletable(row: Dict[str, Any], generation_id: str, user_id: int) -> None:
    if row.get("origin") != TREND_ORIGIN:
        return
    from fastapi import HTTPException
    from app.routers import web_workspace_api as ww
    resp = ww.supabase.table("trend_runs").select("status,is_test").eq("generation_id", generation_id).eq("buyer_user_id", user_id).limit(1).execute()
    rows = list(getattr(resp, "data", None) or [])
    if not rows or rows[0].get("status") not in {"completed", "failed", "refunded", "cancelled"}:
        raise HTTPException(status_code=409, detail="Дождитесь завершения обработки этой генерации.")
