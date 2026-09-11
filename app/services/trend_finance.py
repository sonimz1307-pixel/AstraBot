"""Marketplace money boundary over Nabex's existing token balance.

All mutations are database transactions; intentionally no REST-update fallback.
Public routes MUST authenticate the actor and use canonical billing IDs. Worker
completion accepts only durable ordinary history proof, checked again by SQL.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

import billing_db


FINANCIAL_CONFIG_KEYS = (
    'settlement_kopecks_per_markup_token', 'marketplace_fee_bps', 'hold_seconds',
    'rewards_enabled', 'max_backed_settlement_kopecks_per_token',
)


def _client():
    if billing_db.supabase is None:
        raise RuntimeError('Supabase is required for trend marketplace billing')
    return billing_db.supabase


def _rpc(name: str, **params: Any) -> dict:
    response = _client().rpc('nabex_trend_' + name, {'p_' + k: v for k, v in params.items()}).execute()
    data = getattr(response, 'data', response)
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        raise RuntimeError(f'Trend finance RPC {name} returned an invalid response')
    return data


def _uuid(value: str) -> str:
    return str(UUID(str(value)))


def financial_config_snapshot(config: dict) -> dict:
    return {
        'settlement_kopecks_per_markup_token': int(config.get('settlement_kopecks_per_markup_token') or 0),
        'marketplace_fee_bps': int(config.get('marketplace_fee_bps') or 0),
        'hold_seconds': int(config.get('hold_seconds', 604800)),
        'rewards_enabled': config.get('rewards_enabled') is True,
        'max_backed_settlement_kopecks_per_token': int(config.get('max_backed_settlement_kopecks_per_token') or 0),
    }


def start_run(*, buyer_user_id: int, trend_id: str, trend_version_id: str,
              base_tokens: int, expected_markup_tokens: int, max_total_tokens: int,
              idempotency_key: str, request_hash: str, input_assets: dict,
              is_test: bool = False, expected_financial_config: dict | None = None) -> dict:
    if not isinstance(input_assets, dict):
        raise ValueError('input_assets must map slot UUIDs to lists of uploaded asset UUIDs')
    return _rpc('start_run', buyer_user_id=int(billing_db.resolve_billing_user_id(buyer_user_id)),
                trend_id=_uuid(trend_id), trend_version_id=_uuid(trend_version_id),
                base_tokens=int(base_tokens), expected_markup_tokens=int(expected_markup_tokens),
                max_total_tokens=int(max_total_tokens), idempotency_key=str(idempotency_key),
                request_hash=str(request_hash), input_assets_json=input_assets, is_test=bool(is_test),
                expected_financial_config=expected_financial_config)


def claim_run(*, worker_id: str, lease_seconds: int = 180, run_id: str | None = None) -> dict:
    return _rpc('claim_run', worker_id=str(worker_id), lease_seconds=int(lease_seconds),
                run_id=_uuid(run_id) if run_id else None)


def worker_state() -> dict:
    return _rpc('worker_state')


def touch_model_worker(worker_id: str, model_keys: list[str], capacity: int, trend_capacity: int) -> dict:
    return _rpc('model_worker_touch', worker_id=worker_id, model_keys=model_keys,
                capacity=capacity, trend_capacity=trend_capacity)


def claim_model_run(*, worker_id: str, model_keys: list[str], lease_seconds: int = 180) -> dict:
    return _rpc('claim_model_run', worker_id=worker_id, model_keys=model_keys, lease_seconds=lease_seconds)


def mark_submitting(run_id: str, worker_id: str) -> dict:
    return _rpc('mark_submitting', run_id=_uuid(run_id), worker_id=str(worker_id))


def mark_dispatched(run_id: str, worker_id: str, generation_id: str, metadata: dict) -> dict:
    return _rpc('mark_dispatched', run_id=_uuid(run_id), worker_id=str(worker_id),
                generation_id=str(generation_id), metadata=dict(metadata))


def heartbeat(run_id: str, worker_id: str, lease_seconds: int = 180) -> dict:
    return _rpc('heartbeat', run_id=_uuid(run_id), worker_id=str(worker_id), lease_seconds=int(lease_seconds))


def mark_reconciliation(run_id: str, worker_id: str, reason: str) -> dict:
    return _rpc('mark_reconciliation', run_id=_uuid(run_id), worker_id=str(worker_id), reason=str(reason))


def retry_run(run_id: str, worker_id: str, reason: str) -> dict:
    return _rpc('retry_run', run_id=_uuid(run_id), worker_id=str(worker_id), reason=str(reason))


def recover_run(run_id: str, worker_id: str, lease_seconds: int = 180) -> dict:
    return _rpc('recover_run', run_id=_uuid(run_id), worker_id=str(worker_id), lease_seconds=int(lease_seconds))


def complete_run(run_id: str, *, generation_id: str, result: dict, result_asset_id: str | None = None) -> dict:
    return _rpc('complete_run', run_id=_uuid(run_id), generation_id=str(generation_id),
                result_json=dict(result), result_asset_id=_uuid(result_asset_id) if result_asset_id else None)


def refund_run(run_id: str, *, reason: str = 'generation_failed', allow_completed: bool = False, worker_id: str | None = None) -> dict:
    """allow_completed is ADMIN/explicit review only; never from request JSON."""
    return _rpc('refund_run', run_id=_uuid(run_id), reason=str(reason), allow_completed=bool(allow_completed), worker_id=worker_id)


def release_rewards(*, limit: int = 100) -> dict:
    return _rpc('release_rewards', limit=max(1, min(int(limit), 1000)))


def request_payout(user_id: int, amount_kopecks: int, *, idempotency_key: str, details: dict) -> dict:
    return _rpc('request_payout', user_id=int(user_id), amount_kopecks=int(amount_kopecks),
                idempotency_key=str(idempotency_key), details=dict(details))


def update_payout(payout_id: str, action: str, *, admin_id: int, note: str = '') -> dict:
    return _rpc('update_payout', payout_id=_uuid(payout_id), action=str(action), admin_id=int(admin_id), note=str(note))


def cancel_own_payout(user_id: int, payout_id: str) -> dict:
    return _rpc('cancel_own_payout', user_id=int(user_id), payout_id=_uuid(payout_id))


def set_freeze(user_id: int, *, balance_frozen: bool, payouts_frozen: bool, admin_id: int, reason: str) -> dict:
    return _rpc('set_freeze', user_id=int(user_id), balance_frozen=bool(balance_frozen),
                payouts_frozen=bool(payouts_frozen), admin_id=int(admin_id), reason=str(reason))


def manual_adjustment(user_id: int, amount_kopecks: int, *, operation_key: str, admin_id: int, reason: str) -> dict:
    return _rpc('manual_adjustment', user_id=int(user_id), amount_kopecks=int(amount_kopecks),
                operation_key=str(operation_key), admin_id=int(admin_id), reason=str(reason))


def _rows(query) -> list[dict]:
    data = getattr(query.execute(), 'data', None) or []
    return [dict(item) for item in data if isinstance(item, dict)]


def wallet(user_id: int) -> dict:
    rows = _rows(_client().table('creator_wallets').select('*').eq('user_id', int(user_id)).limit(1))
    return rows[0] if rows else {'user_id': int(user_id), 'pending_kopecks': 0, 'available_kopecks': 0,
                                'reserved_kopecks': 0, 'paid_kopecks': 0, 'debt_kopecks': 0,
                                'balance_frozen': False, 'payouts_frozen': False}


def ledger(user_id: int, limit: int = 100) -> list[dict]:
    return _rows(_client().table('creator_ledger').select('*').eq('user_id', int(user_id))
                 .order('created_at', desc=True).limit(max(1, min(int(limit), 500))))


def payouts(user_id: int, limit: int = 100) -> list[dict]:
    return _rows(_client().table('creator_payouts').select('*').eq('user_id', int(user_id))
                 .order('created_at', desc=True).limit(max(1, min(int(limit), 500))))


def eligible_balance(user_id: int) -> int:
    uid = int(billing_db.resolve_billing_user_id(user_id))
    rows = _rows(_client().table('bot_user_balance').select('eligible_cash_tokens').eq('telegram_user_id', uid).limit(1))
    return int(rows[0].get('eligible_cash_tokens') or 0) if rows else 0
