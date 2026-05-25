import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import httpx


class ProviderBalanceError(RuntimeError):
    pass


@dataclass
class ProviderBalance:
    provider: str
    title: str
    configured: bool
    status: str
    checked_at: str
    account_id: Optional[str] = None
    account_name: Optional[str] = None
    balance_usd: Optional[float] = None
    credits: Optional[float] = None
    currency: str = "USD"
    message: Optional[str] = None
    raw_fields: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


Fetcher = Callable[[], Awaitable[ProviderBalance]]
_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _float_or_none(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_present(data: Dict[str, Any], keys: List[str]) -> Any:
    for key in keys:
        if key in data and data.get(key) is not None:
            return data.get(key)
    return None


def _flatten_dict(data: Dict[str, Any], prefix: str = "") -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in (data or {}).items():
        full_key = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            out.update(_flatten_dict(value, full_key))
        elif isinstance(value, (str, int, float, bool)) or value is None:
            out[full_key] = value
    return out


def _piapi_base_url() -> str:
    return (os.getenv("PIAPI_BASE_URL") or "https://api.piapi.ai").strip().rstrip("/")


def _piapi_api_key() -> str:
    return (
        os.getenv("PIAPI_API_KEY")
        or os.getenv("PIAPI_KEY")
        or os.getenv("PIAPI_TOKEN")
        or os.getenv("PIAPI_API_TOKEN")
        or ""
    ).strip()


def _cache_seconds() -> int:
    raw = (os.getenv("ADMIN_PROVIDER_BALANCE_CACHE_SECONDS") or "60").strip()
    try:
        return max(0, min(int(raw), 3600))
    except ValueError:
        return 60


def _safe_piapi_raw_fields(account_data: Dict[str, Any]) -> Dict[str, Any]:
    allowed = {
        "account_id",
        "account_name",
        "name",
        "email",
        "credit",
        "credits",
        "remaining_credit",
        "remaining_credits",
        "balance",
        "balance_usd",
        "equivalent_in_usd",
        "currency",
        "plan",
        "plan_name",
        "subscription",
        "created_at",
        "updated_at",
    }
    flat = _flatten_dict(account_data)
    safe: Dict[str, Any] = {}
    for key, value in flat.items():
        leaf = key.split(".")[-1].lower()
        if leaf in allowed:
            safe[key] = value
    return safe


async def fetch_piapi_balance() -> ProviderBalance:
    checked_at = _now_iso()
    api_key = _piapi_api_key()
    if not api_key:
        return ProviderBalance(
            provider="piapi",
            title="PiAPI",
            configured=False,
            status="not_configured",
            checked_at=checked_at,
            message="PIAPI_API_KEY не настроен в env.",
        )

    url = f"{_piapi_base_url()}/account/info"
    headers = {"X-API-Key": api_key, "Accept": "application/json"}
    timeout = float(os.getenv("ADMIN_PROVIDER_BALANCE_TIMEOUT_SECONDS", "20") or "20")

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, headers=headers)
            text = response.text[:1000]
            if response.status_code >= 400:
                raise ProviderBalanceError(f"PiAPI HTTP {response.status_code}: {text}")
            payload = response.json()
    except ProviderBalanceError:
        raise
    except Exception as exc:
        raise ProviderBalanceError(f"PiAPI balance request failed: {exc}") from exc

    account_data: Dict[str, Any]
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        account_data = payload["data"]
    elif isinstance(payload, dict):
        account_data = payload
    else:
        raise ProviderBalanceError("PiAPI returned unsupported balance response")

    flat = _flatten_dict(account_data)
    lower_flat = {k.lower(): v for k, v in flat.items()}

    def find_by_leaf(keys: List[str]) -> Any:
        direct = _first_present(account_data, keys)
        if direct is not None:
            return direct
        for key, value in lower_flat.items():
            if key.split(".")[-1] in keys:
                return value
        return None

    balance_usd = _float_or_none(
        find_by_leaf(["equivalent_in_usd", "balance_usd", "usd_balance", "remaining_usd", "amount_usd"])
    )
    credits = _float_or_none(
        find_by_leaf(["remaining_credits", "remaining_credit", "credits", "credit", "balance"])
    )

    account_id = find_by_leaf(["account_id", "id", "user_id"])
    account_name = find_by_leaf(["account_name", "name", "username", "email"])

    return ProviderBalance(
        provider="piapi",
        title="PiAPI",
        configured=True,
        status="ok",
        checked_at=checked_at,
        account_id=str(account_id) if account_id is not None else None,
        account_name=str(account_name) if account_name is not None else None,
        balance_usd=balance_usd,
        credits=credits,
        currency="USD",
        raw_fields=_safe_piapi_raw_fields(account_data),
    )


_PROVIDER_FETCHERS: Dict[str, Fetcher] = {
    "piapi": fetch_piapi_balance,
}


async def get_provider_balances(*, force: bool = False) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    ttl = _cache_seconds()
    now = time.monotonic()

    for provider, fetcher in _PROVIDER_FETCHERS.items():
        cached = _CACHE.get(provider)
        if not force and cached and ttl > 0 and now - cached[0] < ttl:
            cached_item = dict(cached[1])
            cached_item["cached"] = True
            items.append(cached_item)
            continue

        try:
            item = (await fetcher()).to_dict()
        except Exception as exc:
            item = ProviderBalance(
                provider=provider,
                title=provider.upper(),
                configured=True,
                status="error",
                checked_at=_now_iso(),
                message=str(exc),
            ).to_dict()
        item["cached"] = False
        _CACHE[provider] = (now, item)
        items.append(item)

    return {
        "ok": True,
        "items": items,
        "cache_seconds": ttl,
        "supported_providers": list(_PROVIDER_FETCHERS.keys()),
    }
