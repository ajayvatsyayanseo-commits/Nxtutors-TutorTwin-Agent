"""Structured JSON logging with correlation IDs and secret redaction.

Two guarantees this module owes the rest of the system:

1. A value that looks like a secret never reaches a log sink, even if a caller
   passes it by mistake.
2. Student message content is not logged unless `log_message_content` is
   explicitly enabled (default off), because messages are student PII.
"""

from __future__ import annotations

import logging
import re
from collections.abc import MutableMapping
from contextvars import ContextVar
from typing import Any

import structlog

# Correlation context, populated per request by middleware.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)
event_id_var: ContextVar[str | None] = ContextVar("event_id", default=None)

REDACTED = "[REDACTED]"

# Keys whose values are never safe to log.
_SECRET_KEY_PATTERN = re.compile(
    # `token` is deliberately NOT bare: it would swallow the cost metrics
    # (input_tokens, output_tokens, estimated_tokens) that the usage ledger and
    # every cost trace depend on. Only auth-shaped token names are redacted.
    r"(password|passwd|secret|access[-_]?token|refresh[-_]?token|bearer[-_]?token"
    r"|id[-_]?token|auth[-_]?token|^token$|api[-_]?key|authorization|auth|credential"
    r"|dsn|database_url|connection_string|private[-_]?key|session[-_]?id)",
    re.IGNORECASE,
)

# Values that are secret-shaped regardless of their key.
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9_\-]{12,}"),  # vendor API keys
    re.compile(r"postgres(?:ql)?(?:\+\w+)?://[^\s]+"),  # DSNs with inline credentials
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{10,}", re.IGNORECASE),
)

# Fields carrying student content; dropped unless explicitly enabled.
_CONTENT_KEYS = frozenset({"text", "message_text", "student_text", "prompt", "completion"})

_log_message_content = False


def configure_logging(*, level: str = "INFO", log_message_content: bool = False) -> None:
    global _log_message_content
    _log_message_content = log_message_content

    logging.basicConfig(format="%(message)s", level=getattr(logging, level, logging.INFO))

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_correlation,
            _redact,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level, logging.INFO)),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )


def _add_correlation(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key, var in (
        ("request_id", request_id_var),
        ("correlation_id", correlation_id_var),
        ("event_id", event_id_var),
    ):
        value = var.get()
        if value is not None:
            event_dict.setdefault(key, value)
    return event_dict


def _scrub_value(value: Any) -> Any:
    if isinstance(value, str):
        for pattern in _SECRET_VALUE_PATTERNS:
            value = pattern.sub(REDACTED, value)
        return value
    if isinstance(value, dict):
        return {k: _scrub_entry(k, v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_value(v) for v in value]
    return value


def _scrub_entry(key: str, value: Any) -> Any:
    if _SECRET_KEY_PATTERN.search(key):
        return REDACTED
    if key in _CONTENT_KEYS and not _log_message_content:
        return REDACTED
    return _scrub_value(value)


def _redact(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    return {key: _scrub_entry(key, value) for key, value in event_dict.items()}


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
