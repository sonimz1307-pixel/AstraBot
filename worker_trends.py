"""Durable Trend Workshop outbox worker.

Run as a separate supervised process: python worker_trends.py
Reuses existing Workspace model workers; requires no Telegram queue changes.
The SQL lease prevents two processes from dispatching the same paid operation.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
from contextlib import suppress
from typing import Any, Dict
from uuid import uuid4

from app.services import trend_finance as finance
from app.services import trend_assets as assets
from app.services import workspace_video_archive as video_archive
from app.services.trend_generation import (
    TrendGenerationArchiving, TrendGenerationFailed, TrendGenerationPending, TrendGenerationUncertain,
    build_job, ensure_history, execute_run, generation_id_for_run, get_history,
    recipe_from_version, video_needs_archiving,
)
from app.services.trend_registry import get_model

LOG = logging.getLogger("nabex.trends")
LEASE_SECONDS = max(90, int(os.getenv("TREND_WORKER_LEASE_SECONDS", "180")))
POLL_SECONDS = max(1, min(30, int(os.getenv("TREND_WORKER_POLL_SECONDS", "5"))))
_RECOVERY_CURSOR = ""


def _db():
    from billing_db import supabase
    if supabase is None:
        raise RuntimeError("Supabase is not configured")
    return supabase


def _rows(query):
    return list(getattr(query.execute(), "data", None) or [])


def _version(run: Dict[str, Any]) -> Dict[str, Any]:
    rows = _rows(_db().table("trend_versions").select("*").eq("id", run["trend_version_id"]).eq("trend_id", run["trend_id"]).limit(1))
    if not rows:
        raise ValueError("Immutable recipe missing")
    return dict(rows[0])


def _reference_urls(run: Dict[str, Any], version: Dict[str, Any]) -> list[str]:
    """Stable order: user slots/files first, then fixed references in author order."""
    references = []
    inputs = run.get("input_assets_json") or {}
    slots = _rows(_db().table("trend_input_slots").select("*").eq("trend_version_id", version["id"]).order("position"))
    if set(inputs) - {str(s["id"]) for s in slots}:
        raise ValueError("Unknown input slot")
    for slot in slots:
        selected = inputs.get(str(slot["id"]), [])
        if not isinstance(selected, list):
            raise ValueError("Invalid input files")
        minimum = max(int(slot.get("min_files") or 0), 1 if slot.get("required") else 0)
        if not minimum <= len(selected) <= int(slot.get("max_files") or 1):
            raise ValueError("Required files missing")
        for asset_id in selected:
            asset = assets.get_owned_asset(asset_id, int(run["buyer_user_id"]), trend_id=run["trend_id"], kinds=["test_input"] if run["is_test"] else ["input"])
            if asset["result_type"] != slot["input_type"]:
                raise ValueError("Reference type mismatch")
            references.append(asset)
    fixed = _rows(_db().table("trend_fixed_assets").select("*").eq("trend_version_id", version["id"]).order("position").order("id"))
    for binding in fixed:
        asset = assets.get_owned_asset(binding["asset_id"], int(run["creator_id"]), trend_id=run["trend_id"], kinds=["fixed"])
        references.append(asset)
    spec = get_model(recipe_from_version(version)["model_key"])
    if any(a["result_type"] != "image" or int(a["size_bytes"]) > spec["max_input_bytes"] for a in references):
        raise ValueError("Unsupported reference media")
    # URL lifetime begins at worker dispatch, not at upload or recipe creation.
    return [assets.signed_provider_url(asset, expires_in=3600) for asset in references]


async def _finish(run: dict, version: dict, history: dict) -> None:
    spec = get_model(recipe_from_version(version)["model_key"])
    media_type = "image" if spec["type"] == "photo" else "video"
    result = {"type": media_type, "generation_id": generation_id_for_run(run),
              "history_url": "/api/workspace/image/history/" + generation_id_for_run(run) if media_type == "image" else "/api/workspace/history/" + generation_id_for_run(run)}
    if media_type == "image":
        result["image_url"] = history.get("image_url") or history.get("download_url")
    preview_id = None
    if run.get("is_test"):
        preview = await asyncio.to_thread(assets.persist_generated_preview, trend_id=run["trend_id"], version_id=run["trend_version_id"],
            generation_id=generation_id_for_run(run), creator_id=int(run["creator_id"]), result_type=media_type)
        preview_id = preview["id"]
    await asyncio.to_thread(finance.complete_run, run["id"], generation_id=generation_id_for_run(run), result=result, result_asset_id=preview_id)


async def _heartbeat(run_id: str, worker_id: str) -> None:
    while True:
        await asyncio.sleep(LEASE_SECONDS // 3)
        # Failure cancels the old processor before another process can reclaim.
        await asyncio.to_thread(finance.heartbeat, run_id, worker_id, lease_seconds=LEASE_SECONDS)


async def process_claimed(run: dict, worker_id: str) -> None:
    version = await asyncio.to_thread(_version, run)
    history = await asyncio.to_thread(ensure_history, run, version)
    if history.get("status") == "completed" and history.get("storage_path"):
        await _finish(run, version, history)
        return
    if video_needs_archiving(history, version):
        await asyncio.to_thread(video_archive.enqueue, generation_id_for_run(run), int(run["buyer_user_id"]), history["provider_video_url"],
            "archive_too_large" if history.get("error_code") == "archive_too_large" else "archive_pending")
        await asyncio.to_thread(finance.mark_reconciliation, run["id"], worker_id, reason="video_archive_pending")
        return
    known_task = str((run.get("generation_metadata") or {}).get("provider_task_id") or history.get("provider_task_id") or history.get("task_id") or "")
    try:
        reference_urls = await asyncio.to_thread(_reference_urls, run, version)
        build_job(run, version, reference_urls)  # deterministic validation before submission
    except Exception:
        if known_task:
            await asyncio.to_thread(finance.retry_run, run["id"], worker_id, reason="reference_access_retry")
        else:
            # No external worker was called, so a full original debit refund is safe.
            await asyncio.to_thread(finance.refund_run, run["id"], reason="input_preparation_failed", worker_id=worker_id)
        return
    if known_task:
        run = {**run, "generation_metadata": {**(run.get("generation_metadata") or {}), "provider_task_id": known_task}}
    else:
        await asyncio.to_thread(finance.mark_submitting, run["id"], worker_id)

    async def persist_task(task_id: str) -> None:
        await asyncio.to_thread(finance.mark_dispatched, run["id"], worker_id, generation_id_for_run(run),
            metadata={"provider_task_id": task_id, "source": "trend_marketplace", "model_key": recipe_from_version(version)["model_key"]})

    try:
        history = await execute_run(run, version, reference_urls, on_task_id=persist_task)
        await _finish(run, version, history)
    except TrendGenerationFailed:
        await asyncio.to_thread(finance.refund_run, run["id"], reason="provider_terminal_failure", worker_id=worker_id)
    except TrendGenerationArchiving:
        await asyncio.to_thread(finance.mark_reconciliation, run["id"], worker_id, reason="video_archive_pending")
    except TrendGenerationPending:
        await asyncio.to_thread(finance.retry_run, run["id"], worker_id, reason="provider_poll_retry")
    except TrendGenerationUncertain:
        await asyncio.to_thread(finance.mark_reconciliation, run["id"], worker_id, reason="provider_submission_unknown")
    except Exception:
        # This includes output archival/settlement failures after success. Never
        # refund a completed provider generation or repeat its submission.
        current = await asyncio.to_thread(get_history, run, version)
        if current.get("provider_task_id") or current.get("task_id") or known_task:
            await asyncio.to_thread(finance.retry_run, run["id"], worker_id, reason="result_settlement_retry")
        else:
            await asyncio.to_thread(finance.mark_reconciliation, run["id"], worker_id, reason="result_settlement_reconciliation")


async def _with_lease(run: dict, worker_id: str) -> None:
    operation = asyncio.create_task(process_claimed(run, worker_id))
    heartbeat = asyncio.create_task(_heartbeat(run["id"], worker_id))
    try:
        done, _ = await asyncio.wait({operation, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            await task
    finally:
        for task in (operation, heartbeat):
            task.cancel()
        for task in (operation, heartbeat):
            with suppress(asyncio.CancelledError):
                await task


async def reconcile_completed(worker_id: str, limit: int = 25) -> None:
    """Recover success proofs after a crash; never submit an unknown job again."""
    global _RECOVERY_CURSOR
    def batch():
        query = _db().table("trend_runs").select("*").eq("status", "needs_reconciliation").order("id").limit(limit)
        if _RECOVERY_CURSOR:
            query = query.gt("id", _RECOVERY_CURSOR)
        return _rows(query)
    runs = await asyncio.to_thread(batch)
    # Rotate across every unresolved row; an early permanently unknown task must
    # not starve later completed results from archival/financial settlement.
    _RECOVERY_CURSOR = str(runs[-1]["id"]) if len(runs) == limit else ""
    for run in runs:
        try:
            version = await asyncio.to_thread(_version, run)
            history = await asyncio.to_thread(get_history, run, version)
            if history.get("status") == "completed" and history.get("storage_path"):
                await _finish(run, version, history)
            elif video_needs_archiving(history, version):
                await asyncio.to_thread(video_archive.enqueue, generation_id_for_run(run), int(run["buyer_user_id"]), history["provider_video_url"],
                    "archive_too_large" if history.get("error_code") == "archive_too_large" else "archive_pending")
            elif history.get("provider_task_id") or history.get("task_id"):
                recovered = await asyncio.to_thread(finance.recover_run, run["id"], worker_id, lease_seconds=LEASE_SECONDS)
                if recovered:
                    await _with_lease(recovered, worker_id)
        except Exception as exc:
            # Avoid prompt/URL/credentials in log output.
            LOG.warning("Reconciliation deferred run=%s error=%s", run.get("id"), type(exc).__name__)


async def _trend_loop(worker_id: str, *, once: bool = False) -> None:
    while True:
        try:
            try:
                await asyncio.to_thread(assets.reconcile_uploads, limit=2)
            except Exception as exc:
                LOG.warning("Upload recovery deferred: %s", type(exc).__name__)
            await reconcile_completed(worker_id)
            await asyncio.to_thread(finance.release_rewards, limit=100)
            run = await asyncio.to_thread(finance.claim_run, worker_id=worker_id, lease_seconds=LEASE_SECONDS)
            if run:
                await _with_lease(run, worker_id)
            elif not once:
                await asyncio.sleep(POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.error("Trend worker iteration deferred: %s", type(exc).__name__)
            if not once:
                await asyncio.sleep(POLL_SECONDS)
            else:
                raise
        if once:
            return


async def main(*, once: bool = False, archive_only: bool = False) -> None:
    worker_id = "trends:" + socket.gethostname() + ":" + str(os.getpid()) + ":" + uuid4().hex[:8]
    if once:
        await asyncio.to_thread(video_archive.rpc, 'backfill', limit=25)
        await video_archive.run_once(worker_id)
        if not archive_only:
            await _trend_loop(worker_id, once=True)
        return
    if archive_only:
        await video_archive.run_forever(worker_id)
        return
    # Storage recovery stays active with marketplace_enabled=false and does not
    # wait for a long-running provider generation to finish.
    recovery = asyncio.create_task(video_archive.run_forever(worker_id))
    try:
        await _trend_loop(worker_id)
    finally:
        recovery.cancel()
        with suppress(asyncio.CancelledError):
            await recovery


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Nabex Trend Workshop durable dispatcher")
    parser.add_argument("--once", action="store_true", help="Process at most one available operation")
    parser.add_argument("--archive-only", action="store_true", help="Only recover existing video archives; never start generations")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main(once=args.once, archive_only=args.archive_only))
