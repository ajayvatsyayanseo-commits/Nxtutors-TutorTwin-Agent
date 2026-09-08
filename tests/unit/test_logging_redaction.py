"""Secrets and student content must never reach a log sink."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tutortwin.config import Settings
from tutortwin.observability.logging import REDACTED, _redact, configure_logging

SECRET = "sk-live-abcdef1234567890abcdef"
DSN = "postgresql+psycopg://admin:hunter2@db.example.com:5432/prod"


def scrub(payload: dict[str, Any]) -> dict[str, Any]:
    return _redact(None, "info", dict(payload))


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "api_key",
        "apiKey",
        "internal_api_key",
        "authorization",
        "token",
        "database_url",
        "dsn",
        "private_key",
    ],
)
def test_secret_keys_are_redacted(key: str) -> None:
    assert scrub({key: SECRET})[key] == REDACTED


def test_secret_shaped_value_is_redacted_under_innocent_key() -> None:
    """Redaction cannot rely on the key alone - callers mislabel things."""
    out = scrub({"note": f"using {SECRET} today"})
    assert SECRET not in out["note"]
    assert REDACTED in out["note"]


def test_dsn_with_inline_credentials_is_redacted() -> None:
    out = scrub({"detail": f"connecting to {DSN}"})
    assert "hunter2" not in json.dumps(out)


def test_bearer_token_is_redacted() -> None:
    out = scrub({"header": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6"})
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6" not in json.dumps(out)


def test_nested_secrets_are_redacted() -> None:
    out = scrub({"context": {"db": {"password": "hunter2"}, "items": [SECRET]}})
    assert "hunter2" not in json.dumps(out)
    assert SECRET not in json.dumps(out)


def test_student_content_is_dropped_by_default() -> None:
    configure_logging(level="INFO", log_message_content=False)
    assert scrub({"text": "my private homework question"})["text"] == REDACTED


def test_student_content_can_be_enabled_deliberately() -> None:
    configure_logging(level="INFO", log_message_content=True)
    try:
        assert scrub({"text": "debug me"})["text"] == "debug me"
    finally:
        configure_logging(level="INFO", log_message_content=False)


def test_settings_never_render_secrets() -> None:
    """A Settings repr can land in a traceback; it must not carry the DSN."""
    settings = Settings(database_url=DSN, internal_api_key=SECRET)  # type: ignore[arg-type]
    rendered = f"{settings!r} {settings}"
    assert "hunter2" not in rendered
    assert SECRET not in rendered
    # The real values are still reachable deliberately.
    assert settings.app_dsn == DSN


@pytest.mark.parametrize(
    "key",
    [
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "estimated_tokens",
        "estimated_cost_micros",
        "paid_model_calls",
    ],
)
def test_cost_metrics_are_not_redacted(key: str) -> None:
    """Regression: a bare `token` pattern once swallowed every cost metric.

    These values ARE the cost trace. Redacting them makes spend unauditable,
    which defeats the purpose of the usage ledger.
    """
    assert scrub({key: 1234})[key] == 1234


@pytest.mark.parametrize(
    "key",
    ["token", "access_token", "refresh_token", "auth_token", "id_token", "bearer_token"],
)
def test_auth_token_names_are_still_redacted(key: str) -> None:
    assert scrub({key: SECRET})[key] == REDACTED
