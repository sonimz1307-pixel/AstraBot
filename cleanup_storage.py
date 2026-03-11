#!/usr/bin/env python3
"""Nightly janitor for temporary Supabase Storage files.

This version uses the Supabase Storage SDK to list files/folders instead of
querying PostgREST on the `storage` schema.

Default cleanup targets for this AstraBot project:
- bucket SB_MEDIA_BUCKET / SUPABASE_MEDIA_BUCKET / SUPABASE_BUCKET / Kling -> kling3/
- bucket SUPABASE_BUCKET / Kling -> kling_inputs/
- bucket SUPABASE_BUCKET / Kling -> veo_inputs/ (or VEO_BUCKET_PREFIX)
- bucket SEEDANCE_REF_BUCKET / seedance-refs -> refs/ (or SEEDANCE_REF_PREFIX)

Safe behavior:
- deletes only files older than CLEANUP_MAX_AGE_HOURS (default: 72)
- touches only known temp prefixes
- supports dry-run mode

Usage:
  python cleanup_storage_fixed.py
  python cleanup_storage_fixed.py --dry-run
  python cleanup_storage_fixed.py --hours 48
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional, Tuple

from supabase import create_client

LOG = logging.getLogger("cleanup_storage")


@dataclass(frozen=True)
class CleanupRule:
    bucket: str
    prefix: str


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "y", "on"}


def _require_env_pair() -> Tuple[str, str]:
    url = _env("SUPABASE_URL").rstrip("/")
    key = _env("SUPABASE_SERVICE_KEY") or _env("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise RuntimeError("Missing SUPABASE_URL and SUPABASE_SERVICE_KEY (or SUPABASE_SERVICE_ROLE_KEY).")
    return url, key


def _unique_rules(rules: Iterable[CleanupRule]) -> List[CleanupRule]:
    seen = set()
    out: List[CleanupRule] = []
    for rule in rules:
        bucket = rule.bucket.strip()
        prefix = rule.prefix.strip().strip("/")
        if not bucket or not prefix:
            continue
        key = (bucket, prefix)
        if key in seen:
            continue
        seen.add(key)
        out.append(CleanupRule(bucket=bucket, prefix=prefix))
    return out


def _default_rules() -> List[CleanupRule]:
    kling3_bucket = (
        _env("SB_MEDIA_BUCKET")
        or _env("SUPABASE_MEDIA_BUCKET")
        or _env("SUPABASE_BUCKET")
        or "Kling"
    )
    main_bucket = _env("SUPABASE_BUCKET") or kling3_bucket or "Kling"
    veo_prefix = _env("VEO_BUCKET_PREFIX") or "veo_inputs"
    seedance_bucket = _env("SEEDANCE_REF_BUCKET") or "seedance-refs"
    seedance_prefix = _env("SEEDANCE_REF_PREFIX") or "refs"

    rules = [
        CleanupRule(bucket=kling3_bucket, prefix="kling3"),
        CleanupRule(bucket=main_bucket, prefix="kling_inputs"),
        CleanupRule(bucket=main_bucket, prefix=veo_prefix),
        CleanupRule(bucket=seedance_bucket, prefix=seedance_prefix),
    ]

    extra = _env("CLEANUP_EXTRA_RULES")
    if extra:
        for chunk in extra.split(";"):
            chunk = chunk.strip()
            if not chunk or ":" not in chunk:
                continue
            bucket, prefix = chunk.split(":", 1)
            rules.append(CleanupRule(bucket=bucket.strip(), prefix=prefix.strip()))

    return _unique_rules(rules)


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _normalize_list_response(rows):
    if rows is None:
        return []
    if isinstance(rows, list):
        return rows
    if isinstance(rows, dict):
        if isinstance(rows.get("data"), list):
            return rows["data"]
        if isinstance(rows.get("items"), list):
            return rows["items"]
    raise RuntimeError(f"Unexpected list() response type: {type(rows).__name__}")


def _is_folder_entry(entry: dict) -> bool:
    # Supabase docs note that folder entries have id/created_at/updated_at/metadata == null.
    return (
        entry.get("id") is None
        and entry.get("created_at") is None
        and entry.get("updated_at") is None
        and entry.get("metadata") is None
    )


def _join_path(parent: str, name: str) -> str:
    parent = parent.strip().strip("/")
    name = name.strip().strip("/")
    if not parent:
        return name
    if not name:
        return parent
    return f"{parent}/{name}"


def _list_old_paths(
    *,
    sb_client,
    bucket: str,
    prefix: str,
    cutoff: datetime,
    page_size: int,
) -> List[str]:
    results: List[str] = []
    folders_to_scan: List[str] = [prefix.strip().strip("/")]

    while folders_to_scan:
        current_folder = folders_to_scan.pop(0)
        offset = 0
        while True:
            rows = sb_client.storage.from_(bucket).list(
                current_folder,
                {
                    "limit": page_size,
                    "offset": offset,
                    "sortBy": {"column": "name", "order": "asc"},
                },
            )
            rows = _normalize_list_response(rows)
            if not rows:
                break

            for entry in rows:
                if not isinstance(entry, dict):
                    continue
                name = (entry.get("name") or "").strip().strip("/")
                if not name:
                    continue
                full_path = _join_path(current_folder, name)

                if _is_folder_entry(entry):
                    folders_to_scan.append(full_path)
                    continue

                created_at = _parse_ts(entry.get("created_at"))
                updated_at = _parse_ts(entry.get("updated_at"))
                ts = created_at or updated_at
                if ts is None:
                    # Unknown timestamp -> skip for safety.
                    continue
                if ts < cutoff:
                    results.append(full_path)

            if len(rows) < page_size:
                break
            offset += page_size

    return results


def _chunked(items: List[str], size: int) -> Iterable[List[str]]:
    for idx in range(0, len(items), size):
        yield items[idx : idx + size]


def _delete_paths(*, sb_client, bucket: str, paths: List[str], chunk_size: int) -> int:
    deleted = 0
    for chunk in _chunked(paths, chunk_size):
        sb_client.storage.from_(bucket).remove(chunk)
        deleted += len(chunk)
    return deleted


def main() -> int:
    parser = argparse.ArgumentParser(description="Cleanup old temp files from Supabase Storage.")
    parser.add_argument("--hours", type=int, default=None, help="Delete files older than this many hours.")
    parser.add_argument("--dry-run", action="store_true", help="Only print what would be deleted.")
    parser.add_argument("--verbose", action="store_true", help="Verbose logging.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    try:
        supabase_url, service_key = _require_env_pair()
    except Exception as exc:
        LOG.error("Config error: %s", exc)
        return 2

    max_age_hours = args.hours or int(_env("CLEANUP_MAX_AGE_HOURS", "72"))
    page_size = int(_env("CLEANUP_PAGE_SIZE", "1000"))
    delete_chunk_size = int(_env("CLEANUP_DELETE_CHUNK_SIZE", "100"))
    dry_run = args.dry_run or _env_bool("CLEANUP_DRY_RUN", False)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    rules = _default_rules()
    if not rules:
        LOG.warning("No cleanup rules configured. Nothing to do.")
        return 0

    LOG.info(
        "Cleanup started | dry_run=%s | older_than_hours=%s | cutoff_utc=%s",
        dry_run,
        max_age_hours,
        cutoff.isoformat(),
    )
    for rule in rules:
        LOG.info("Rule: bucket=%s prefix=%s/", rule.bucket, rule.prefix)

    try:
        sb = create_client(supabase_url, service_key)
    except Exception as exc:
        LOG.error("Failed to create Supabase client: %s", exc)
        return 2

    total_found = 0
    total_deleted = 0
    had_errors = False

    for rule in rules:
        try:
            old_paths = _list_old_paths(
                sb_client=sb,
                bucket=rule.bucket,
                prefix=rule.prefix,
                cutoff=cutoff,
                page_size=page_size,
            )
            total_found += len(old_paths)
            if not old_paths:
                LOG.info("No expired files | bucket=%s prefix=%s/", rule.bucket, rule.prefix)
                continue

            preview = ", ".join(old_paths[:5])
            suffix = " ..." if len(old_paths) > 5 else ""
            LOG.info(
                "Expired files found: %s | bucket=%s prefix=%s/ | sample=%s%s",
                len(old_paths),
                rule.bucket,
                rule.prefix,
                preview,
                suffix,
            )

            if dry_run:
                continue

            deleted_now = _delete_paths(
                sb_client=sb,
                bucket=rule.bucket,
                paths=old_paths,
                chunk_size=delete_chunk_size,
            )
            total_deleted += deleted_now
            LOG.info("Deleted: %s | bucket=%s prefix=%s/", deleted_now, rule.bucket, rule.prefix)
        except Exception as exc:
            had_errors = True
            LOG.exception("Cleanup failed for bucket=%s prefix=%s/: %s", rule.bucket, rule.prefix, exc)

    LOG.info(
        "Cleanup finished | dry_run=%s | found=%s | deleted=%s | errors=%s",
        dry_run,
        total_found,
        total_deleted,
        had_errors,
    )

    return 1 if had_errors else 0


if __name__ == "__main__":
    sys.exit(main())
