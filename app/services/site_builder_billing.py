from __future__ import annotations

from typing import Dict, Optional

from billing_db import add_tokens, ensure_user_row, get_balance, ledger_ref_exists

SITE_CREATE_PRICE = 30
SITE_REVISION_PRICE = 10


def get_site_balance(user_id: int) -> int:
    ensure_user_row(int(user_id))
    return int(get_balance(int(user_id)) or 0)


def charge_site_create(*, user_id: int, project_id: str, meta: Optional[Dict[str, object]] = None) -> None:
    ensure_user_row(int(user_id))
    add_tokens(
        int(user_id),
        -SITE_CREATE_PRICE,
        reason="site_create",
        ref_id=str(project_id),
        meta={"price_tokens": SITE_CREATE_PRICE, **(meta or {})},
    )


def refund_site_create(*, user_id: int, project_id: str, error: str = "") -> None:
    ensure_user_row(int(user_id))
    if ledger_ref_exists(reason="site_create_refund", ref_id=str(project_id)):
        return
    add_tokens(
        int(user_id),
        SITE_CREATE_PRICE,
        reason="site_create_refund",
        ref_id=str(project_id),
        meta={"price_tokens": SITE_CREATE_PRICE, "error": (error or "")[:500]},
    )


def charge_site_revision(*, user_id: int, job_id: str, project_id: str, meta: Optional[Dict[str, object]] = None) -> None:
    ensure_user_row(int(user_id))
    add_tokens(
        int(user_id),
        -SITE_REVISION_PRICE,
        reason="site_revision",
        ref_id=str(job_id),
        meta={"price_tokens": SITE_REVISION_PRICE, "project_id": str(project_id), **(meta or {})},
    )


def refund_site_revision(*, user_id: int, job_id: str, project_id: str, error: str = "") -> None:
    ensure_user_row(int(user_id))
    if ledger_ref_exists(reason="site_revision_refund", ref_id=str(job_id)):
        return
    add_tokens(
        int(user_id),
        SITE_REVISION_PRICE,
        reason="site_revision_refund",
        ref_id=str(job_id),
        meta={
            "price_tokens": SITE_REVISION_PRICE,
            "project_id": str(project_id),
            "error": (error or "")[:500],
        },
    )
