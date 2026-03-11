#!/usr/bin/env python3
"""Nightly janitor for temporary Supabase Storage files.

What it cleans by default for this AstraBot project:
- bucket SB_MEDIA_BUCKET / SUPABASE_MEDIA_BUCKET / SUPABASE_BUCKET / Kling -> kling3/
- bucket SUPABASE_BUCKET / Kling -> kling_inputs/
- bucket SUPABASE_BUCKET / Kling -> veo_inputs/ (or VEO_BUCKET_PREFIX)
- bucket SEEDANCE_REF_BUCKET / seedance-refs -> refs/ (or SEEDANCE_REF_PREFIX)

Safe behavior:
- deletes only files older than CLEANUP_MAX_AGE_HOURS (default: 72)
- touches only known temp prefixes
- supports dry-run mode

Usage:
  python cleanup_storage.py
  python cleanup_storage.py --dry-run
  python cleanup_storage.py --hours 48
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional, Tuple
from urllib.parse import urlencode

import httpx
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
    # Kling 3 temp frames may use dedicated media bucket or the same Kling bucket.
    kling3_bucket = (
        _env("SB_MEDIA_BUCKET")
        or _env("SUPABASE_MEDIA_BUCKET")
        or _env("SUPABASE_BUCKET")
        or "Kling"
    )

    # Kling / Veo temp inputs use SUPABASE_BUCKET in this project.
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
        # Format: bucket1:prefix1;bucket2:prefix2
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


def _build_rest_headers(service_key: str) -> dict:
    return {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Accept-Profile": "storage",
    }


def _list_old_paths(
    *,
    url: str,
    service_key: str,
    bucket: str,
    prefix: str,
    cutoff: datetime,
    page_size: int,
    timeout_sec: float,
) -> List[str]:
    """Read only metadata from storage.objects, then delete through Storage API.

    We query the storage schema for object metadata because it gives us full object names
    and timestamps reliably for age-based cleanup, while actual deletion still goes through
    the Storage API via `storage.remove(...)`.
    """
    endpoint = f"{url}/rest/v1/objects"
    headers = _build_rest_headers(service_key)
    cutoff_iso = cutoff.isoformat()
    results: List[str] = []
    offset = 0

    with httpx.Client(timeout=timeout_sec, headers=headers) as client:
        while True:
            params = {
                "select": "name,created_at,updated_at",
                "bucket_id": f"eq.{bucket}",
                "name": f"like.{prefix}/%",
                "or": f"(created_at.lt.{cutoff_iso},and(created_at.is.null,updated_at.lt.{cutoff_iso}))",
                "order": "created_at.asc.nullsfirst,name.asc",
                "limit": str(page_size),
                "offset": str(offset),
            }
            resp = client.get(endpoint, params=params)
            if resp.status_code >= 300:
                raise RuntimeError(
                    f"Failed to read storage.objects for bucket={bucket} prefix={prefix}: "
                    f"{resp.status_code} {resp.text[:500]}"
                )
            rows = resp.json()
            if not isinstance(rows, list):
                raise RuntimeError(
                    f"Unexpected response for bucket={bucket} prefix={prefix}: {type(rows).__name__}"
                )
            if not rows:
                break

            for row in rows:
                name = (row.get("name") or "").strip().lstrip("/")
                if not name:
                    continue
                # Extra guard in case filters change or API returns surprises.
                if not name.startswith(prefix.rstrip("/") + "/"):
                    continue
                results.append(name)

            if len(rows) < page_size:
                break
            offset += page_size

    return results


def _chunked(items: List[str], size: int) -> Iterable[List[str]]:
    for idx in range(0, len(items), size):
        yield items[idx : idx + size]


def _delete_paths(
    *,
    sb_client,
    bucket: str,
    paths: List[str],
    chunk_size: int,
) -> int:
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
    timeout_sec = float(_env("CLEANUP_HTTP_TIMEOUT_SEC", "60"))
    dry_run = args.dry_run or _env_bool("CLEANUP_DRY_RUN", False)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    rules = _default_rules()
    if not rules:
        LOG.warning("No cleanup rules configured. Nothing to do.")
        return 0

    LOG.info("Cleanup started | dry_run=%s | older_than_hours=%s | cutoff_utc=%s", dry_run, max_age_hours, cutoff.isoformat())
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
                url=supabase_url,
                service_key=service_key,
                bucket=rule.bucket,
                prefix=rule.prefix,
                cutoff=cutoff,
                page_size=page_size,
                timeout_sec=timeout_sec,
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
