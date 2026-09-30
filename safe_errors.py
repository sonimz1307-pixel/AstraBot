"""Secret redaction at Telegram, provider-error and logging boundaries.

Uses only the standard library. Ordinary signed media links are preserved.
Never use redaction on an outgoing authenticated API request.
"""
from __future__ import annotations

import builtins
import json
import logging
import os
import re
import traceback
from urllib.parse import quote, quote_plus

REDACTED = "[REDACTED]"
_UVICORN_ACCESS_FORMAT = '%s - "%s %s HTTP/%s" %d'
_SECRET_ENV_NAME = re.compile(r"TOKEN|SECRET|API_?KEY|SERVICE_?KEY|_KEY$|PASSWORD|(?:^|_)PASS(?:$|_)", re.I)
_TELEGRAM_URL = re.compile(
    r"(https?://api\.telegram\.org/(?:file/)?bot)[^\s/?#<>\"'\\]+", re.I
)
_BOT_TOKEN = re.compile(r"(?<![A-Za-z0-9_])\d{5,20}(?::|%3a)[A-Za-z0-9_-]{20,}", re.I)
_AUTH = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=%:-]{8,}", re.I)
_NAMED_SECRET = re.compile(
    r"(\b(?:api[_-]?key|bot[_-]?token|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|password)\b[\"']?\s*[:=]\s*[\"']?)([^\s\"'&,;}<>]+)", re.I
)
_URL_PASSWORD = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s/@:]+:)[^\s/@]+(@)", re.I)


def redact_secrets(value: object) -> str:
    text = str(value)
    # Read on use: also covers configuration loaded after module import/rotation.
    values = set()
    for name, secret in list(os.environ.items()):
        if _SECRET_ENV_NAME.search(name) and len(secret.strip()) >= 8:
            secret = secret.strip()
            values.update((secret, quote(secret, safe=""), quote_plus(secret), json.dumps(secret)[1:-1]))
    for secret in sorted(values, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    # Patterns also cover old bot tokens no longer present in the environment.
    text = _TELEGRAM_URL.sub(lambda m: m[1] + REDACTED, text)
    text = _BOT_TOKEN.sub(REDACTED, text)
    text = _AUTH.sub(lambda m: m[1] + " " + REDACTED, text)
    text = _NAMED_SECRET.sub(lambda m: m[1] + REDACTED, text)
    return _URL_PASSWORD.sub(lambda m: m[1] + REDACTED + m[2], text)


def public_error_text(error: object, *, fallback: str = "Не удалось выполнить запрос. Попробуй ещё раз.", limit: int = 700) -> str:
    raw = str(error)
    safe = redact_secrets(raw).strip()
    if not safe or safe != raw.strip() or REDACTED in safe or re.search(
        r"api\.telegram\.org|Traceback \(most recent call last\)|Authorization", safe, re.I
    ):
        safe = fallback
    return redact_secrets(safe)[:limit]


def safe_print(*values: object, **kwargs: object) -> None:
    builtins.print(*(redact_secrets(value) for value in values), **kwargs)


def install_secret_log_redaction() -> None:
    """Sanitize logs while preserving Uvicorn's five access-log arguments."""
    previous = logging.getLogRecordFactory()
    if getattr(previous, "_nabex_secret_redaction", False):
        return

    def factory(*args: object, **kwargs: object) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        is_uvicorn_access = record.name == "uvicorn.access"
        try:
            if is_uvicorn_access:
                # AccessFormatter builds request_line/status_code from args;
                # flattening them into msg breaks every HTTP access log.
                client_addr, method, full_path, http_version, status_code = record.args
                record.msg = _UVICORN_ACCESS_FORMAT
                record.args = (
                    redact_secrets(client_addr),
                    redact_secrets(method),
                    redact_secrets(full_path),
                    redact_secrets(http_version),
                    int(status_code),
                )
            else:
                # Redact after interpolation: also covers secrets split across
                # format arguments and placeholders such as password=%s.
                record.msg = redact_secrets(record.getMessage())
                record.args = ()
            if record.exc_info:
                record.exc_text = redact_secrets("".join(traceback.format_exception(*record.exc_info)))
            elif record.exc_text:
                record.exc_text = redact_secrets(record.exc_text)
            if record.stack_info:
                record.stack_info = redact_secrets(record.stack_info)
        except Exception:
            record.msg = "Log entry omitted: could not safely format diagnostic"
            record.args = ()
            if is_uvicorn_access:
                # Even fail-closed records must satisfy AccessFormatter's shape.
                record.msg = _UVICORN_ACCESS_FORMAT
                record.args = (REDACTED, REDACTED, "/log-entry-omitted", "?", 0)
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return record

    factory._nabex_secret_redaction = True
    logging.setLogRecordFactory(factory)
