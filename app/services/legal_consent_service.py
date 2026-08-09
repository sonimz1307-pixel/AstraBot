from __future__ import annotations

import os
from typing import Any, Dict, Optional

from db_supabase import supabase


LEGAL_CONSENTS_TABLE = (os.getenv("WORKSPACE_LEGAL_CONSENTS_TABLE") or "workspace_legal_consents").strip() or "workspace_legal_consents"
LEGAL_CONSENT_EVENTS_TABLE = (os.getenv("WORKSPACE_LEGAL_CONSENT_EVENTS_TABLE") or "workspace_legal_consent_events").strip() or "workspace_legal_consent_events"
LEGAL_ACCEPT_RPC = (os.getenv("WORKSPACE_LEGAL_ACCEPT_RPC") or "workspace_accept_legal_consents_v4").strip() or "workspace_accept_legal_consents_v4"
LEGAL_REVOKE_RPC = (os.getenv("WORKSPACE_LEGAL_REVOKE_RPC") or "workspace_revoke_personal_data_consent_v4").strip() or "workspace_revoke_personal_data_consent_v4"

# Версии юридических документов, опубликованных вместе с этим патчем.
# При публикации новой редакции достаточно изменить соответствующую версию:
# уже действующая сессия получит LEGAL_CONSENT_REQUIRED и попросит подтверждение.
LEGAL_OFFER_VERSION = "2026-08-09"
LEGAL_PRIVACY_VERSION = "2026-08-09"
LEGAL_PERSONAL_DATA_CONSENT_VERSION = "2026-08-09"

LEGAL_OFFER_URL = "https://nabex.ru/terms.html"
LEGAL_PRIVACY_URL = "https://nabex.ru/privacy.html"
LEGAL_PERSONAL_DATA_CONSENT_URL = "https://nabex.ru/consent-personal-data.html"


class LegalConsentError(RuntimeError):
    pass


class LegalConsentRequired(ValueError):
    pass


def _account_id(value: Any) -> int:
    try:
        numeric_account_id = int(value or 0)
    except Exception as exc:
        raise LegalConsentError("Invalid workspace account id for legal consent") from exc
    if numeric_account_id <= 0:
        raise LegalConsentError("Invalid workspace account id for legal consent")
    return numeric_account_id


def require_legal_acceptance(*, terms_accepted: bool, personal_data_accepted: bool) -> None:
    if not bool(terms_accepted):
        raise LegalConsentRequired("Необходимо принять Публичную оферту и подтвердить ознакомление с Политикой конфиденциальности.")
    if not bool(personal_data_accepted):
        raise LegalConsentRequired("Необходимо отдельно дать согласие на обработку персональных данных.")


def _current_rows(*, account_id: int) -> list[Dict[str, Any]]:
    if supabase is None:
        raise LegalConsentError("Supabase disabled: cannot read legal consent evidence")
    try:
        res = (
            supabase.table(LEGAL_CONSENTS_TABLE)
            .select("consent_type,document_version,accepted_at,revoked_at")
            .eq("account_id", account_id)
            .execute()
        )
        return [row for row in (getattr(res, "data", None) or []) if isinstance(row, dict)]
    except Exception as exc:
        raise LegalConsentError(
            f"Не удалось проверить подтверждение согласий. Проверь таблицу {LEGAL_CONSENTS_TABLE} и SQL миграцию V4."
        ) from exc


def current_legal_acceptance_status(*, account_id: int) -> Dict[str, Any]:
    """Проверяет именно ТЕКУЩИЕ версии документов для аккаунта.

    IP/user-agent сохраняются только как доказательство события и никогда не
    участвуют в валидности согласия. Поэтому VPN, смена сети или IP не заставляют
    пользователя отмечать галочки повторно.
    """
    numeric_account_id = _account_id(account_id)
    rows = _current_rows(account_id=numeric_account_id)

    by_key = {
        (str(row.get("consent_type") or ""), str(row.get("document_version") or "")): row
        for row in rows
    }
    offer_row = by_key.get(("offer_acceptance", LEGAL_OFFER_VERSION))
    privacy_row = by_key.get(("privacy_acknowledgement", LEGAL_PRIVACY_VERSION))
    personal_row = by_key.get(("personal_data_consent", LEGAL_PERSONAL_DATA_CONSENT_VERSION))

    offer_ok = bool(offer_row)
    privacy_ok = bool(privacy_row)
    personal_baseline_ok = bool(personal_row)
    personal_revoked = bool(personal_row and personal_row.get("revoked_at"))
    personal_ok = bool(personal_baseline_ok and not personal_revoked)

    return {
        "complete": bool(offer_ok and privacy_ok and personal_ok),
        "offer_accepted": offer_ok,
        "privacy_acknowledged": privacy_ok,
        "personal_data_accepted": personal_ok,
        "personal_data_revoked": personal_revoked,
        "versions": {
            "offer": LEGAL_OFFER_VERSION,
            "privacy": LEGAL_PRIVACY_VERSION,
            "personal_data_consent": LEGAL_PERSONAL_DATA_CONSENT_VERSION,
        },
    }


def ensure_current_legal_acceptance(
    *,
    account_id: int,
    terms_accepted: bool,
    personal_data_accepted: bool,
    source: str,
    request: Any = None,
    evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Гарантирует наличие актуального согласия и при необходимости записывает его.

    Если текущие версии уже приняты и персональное согласие не отозвано, повторное
    подтверждение не требуется. При новой версии документа либо после отзыва
    требуются два самостоятельных действия пользователя.
    """
    status = current_legal_acceptance_status(account_id=account_id)
    if bool(status.get("complete")):
        return status

    require_legal_acceptance(
        terms_accepted=terms_accepted,
        personal_data_accepted=personal_data_accepted,
    )
    record_legal_acceptances(
        account_id=account_id,
        terms_accepted=terms_accepted,
        personal_data_accepted=personal_data_accepted,
        source=source,
        request=request,
        evidence=evidence,
    )
    refreshed = current_legal_acceptance_status(account_id=account_id)
    if not bool(refreshed.get("complete")):
        raise LegalConsentError("Не удалось подтвердить сохранение актуальных юридических согласий.")
    return refreshed


def request_legal_evidence(request: Any) -> Dict[str, Optional[str]]:
    """Минимальные технические данные для доказательства действия пользователя.

    ВАЖНО: IP является вспомогательным evidence-полем и не используется ни для
    аутентификации, ни для решения, нужно ли показывать согласия повторно.
    """
    headers = getattr(request, "headers", {}) or {}
    forwarded = str(headers.get("x-forwarded-for") or "").split(",", 1)[0].strip()
    real_ip = str(headers.get("x-real-ip") or "").strip()
    client = getattr(request, "client", None)
    client_host = str(getattr(client, "host", "") or "").strip()
    ip_address = (forwarded or real_ip or client_host or "")[:128] or None
    user_agent = str(headers.get("user-agent") or "").strip()[:1000] or None
    return {"ip_address": ip_address, "user_agent": user_agent}


def _legal_meta(*, request: Any = None, evidence: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    meta: Dict[str, Any] = {}
    if request is not None:
        meta.update(request_legal_evidence(request))
    if isinstance(evidence, dict):
        for key in ("ip_address", "user_agent"):
            value = evidence.get(key)
            if value not in (None, ""):
                meta[key] = str(value)[:1000 if key == "user_agent" else 128]
    return meta


def _rpc_payload_common(*, account_id: int, source: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "p_account_id": _account_id(account_id),
        "p_source": str(source or "workspace_auth").strip()[:80] or "workspace_auth",
        "p_ip_address": meta.get("ip_address"),
        "p_user_agent": meta.get("user_agent"),
    }


def record_legal_acceptances(
    *,
    account_id: int,
    terms_accepted: bool,
    personal_data_accepted: bool,
    source: str,
    request: Any = None,
    evidence: Optional[Dict[str, Any]] = None,
) -> None:
    """Атомарно сохраняет current-state и audit-event через PostgreSQL RPC.

    V4 намеренно не делает отдельные INSERT/UPSERT из Python. Одна RPC-функция
    выполняется в одной транзакции БД и берёт advisory lock на account_id.
    Поэтому конкурентные повторы и сетевой retry не создают лишних accepted
    событий, а ошибка audit INSERT откатывает и current-state.
    """
    require_legal_acceptance(
        terms_accepted=terms_accepted,
        personal_data_accepted=personal_data_accepted,
    )
    if supabase is None:
        raise LegalConsentError("Supabase disabled: cannot store legal consent evidence")

    numeric_account_id = _account_id(account_id)
    meta = _legal_meta(request=request, evidence=evidence)
    payload = _rpc_payload_common(account_id=numeric_account_id, source=source, meta=meta)
    payload.update({
        "p_offer_version": LEGAL_OFFER_VERSION,
        "p_offer_url": LEGAL_OFFER_URL,
        "p_privacy_version": LEGAL_PRIVACY_VERSION,
        "p_privacy_url": LEGAL_PRIVACY_URL,
        "p_personal_version": LEGAL_PERSONAL_DATA_CONSENT_VERSION,
        "p_personal_url": LEGAL_PERSONAL_DATA_CONSENT_URL,
    })

    try:
        supabase.rpc(LEGAL_ACCEPT_RPC, payload).execute()
    except Exception as exc:
        raise LegalConsentError(
            f"Не удалось атомарно сохранить юридические согласия. Проверь RPC {LEGAL_ACCEPT_RPC} и SQL миграцию V4."
        ) from exc


def revoke_personal_data_consent(
    *,
    account_id: int,
    source: str,
    request: Any = None,
    evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Атомарно отзывает ПДн-согласие и пишет append-only audit-event.

    Повторный отзыв идемпотентен. Обновление current-state и запись события идут
    в одной транзакции SQL RPC; частичное состояние невозможно.
    """
    if supabase is None:
        raise LegalConsentError("Supabase disabled: cannot revoke legal consent")

    numeric_account_id = _account_id(account_id)
    meta = _legal_meta(request=request, evidence=evidence)
    payload = _rpc_payload_common(account_id=numeric_account_id, source=source or "workspace_profile", meta=meta)
    payload.update({
        "p_personal_version": LEGAL_PERSONAL_DATA_CONSENT_VERSION,
        "p_personal_url": LEGAL_PERSONAL_DATA_CONSENT_URL,
    })

    try:
        supabase.rpc(LEGAL_REVOKE_RPC, payload).execute()
    except Exception as exc:
        raise LegalConsentError(
            f"Не удалось атомарно отозвать согласие. Проверь RPC {LEGAL_REVOKE_RPC} и SQL миграцию V4."
        ) from exc

    return current_legal_acceptance_status(account_id=numeric_account_id)
