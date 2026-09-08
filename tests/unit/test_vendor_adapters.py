"""Vendor adapters: response normalization and error mapping.

No API key and no network. The SDK client is replaced with a stub, because what
needs proving is the translation layer - if an adapter maps a 429 to the wrong
category, the gateway stops retrying a retryable failure, and that bug is
invisible until production.
"""

from __future__ import annotations

from typing import Any

import anthropic
import httpx2 as httpx
import openai
import pytest

from tutortwin.domain.provider import (
    ErrorCategory,
    ModelAlias,
    ModelCatalogEntry,
    ModelMessage,
    ModelRequest,
    Provider,
    StopReason,
)
from tutortwin.providers.anthropic_adapter import AnthropicAdapter
from tutortwin.providers.openai_adapter import OpenAIAdapter

ENTRY = ModelCatalogEntry(
    alias=ModelAlias.STANDARD_TUTOR,
    provider=Provider.ANTHROPIC,
    model_id="model-under-test",
    input_cost_micros_per_1k=2000,
    output_cost_micros_per_1k=10000,
    rate_version="test",
)

REQUEST = ModelRequest(
    alias=ModelAlias.STANDARD_TUTOR,
    messages=(ModelMessage(role="user", content="What is osmosis?"),),
    system="You are a tutor.",
    cacheable_prefix="STABLE SAFETY BLOCK",
    max_output_tokens=512,
)


def _status_error(cls: type[Exception], status: int) -> Exception:
    """Build a real SDK exception; these require an actual response object."""
    request = httpx.Request("POST", "https://api.example.com/v1/messages")
    response = httpx.Response(status, request=request)
    return cls(f"{status}", response=response, body=None)  # type: ignore[call-arg]


def _timeout_error(module: object) -> Exception:
    request = httpx.Request("POST", "https://api.example.com/v1/messages")
    return module.APITimeoutError(request=request)  # type: ignore[attr-defined,no-any-return]


class _Recorder:
    """Captures the kwargs an adapter would send, or raises a chosen error."""

    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.kwargs: dict[str, Any] = {}

    def with_options(self, **_options: Any) -> _Recorder:
        return self

    async def _call(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        return self.result


# --- Anthropic ----------------------------------------------------------------


class _AnthropicBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _AnthropicUsage:
    def __init__(self) -> None:
        self.input_tokens = 120
        self.output_tokens = 45
        self.cache_read_input_tokens = 80


class _AnthropicResponse:
    def __init__(self, stop_reason: str = "end_turn", text: str = "Osmosis is...") -> None:
        self.content = [_AnthropicBlock(text)]
        self.usage = _AnthropicUsage()
        self.stop_reason = stop_reason
        self._request_id = "req_anthropic_1"


def anthropic_adapter(recorder: _Recorder) -> AnthropicAdapter:
    adapter = AnthropicAdapter(api_key="test-key")
    messages = type("M", (), {"create": recorder._call})()
    adapter._client = type(  # type: ignore[assignment]
        "C", (), {"with_options": lambda _self, **_k: type("O", (), {"messages": messages})()}
    )()
    return adapter


async def test_anthropic_maps_a_successful_response() -> None:
    recorder = _Recorder(result=_AnthropicResponse())
    result = await anthropic_adapter(recorder).invoke(REQUEST, ENTRY)

    assert result.text == "Osmosis is..."
    assert result.input_tokens == 120
    assert result.output_tokens == 45
    assert result.cached_tokens == 80
    assert result.stop_reason is StopReason.END_TURN
    assert result.error_category is ErrorCategory.NONE
    assert result.provider_request_id == "req_anthropic_1"


async def test_anthropic_sends_a_cacheable_prefix_breakpoint() -> None:
    """The stable prefix must be marked ephemeral or caching never engages."""
    recorder = _Recorder(result=_AnthropicResponse())
    await anthropic_adapter(recorder).invoke(REQUEST, ENTRY)

    system = recorder.kwargs["system"]
    assert system[0]["text"] == "STABLE SAFETY BLOCK"
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    # The volatile half follows and is deliberately not cached.
    assert system[1]["text"] == "You are a tutor."
    assert "cache_control" not in system[1]


async def test_anthropic_uses_the_catalog_model_id() -> None:
    recorder = _Recorder(result=_AnthropicResponse())
    await anthropic_adapter(recorder).invoke(REQUEST, ENTRY)
    assert recorder.kwargs["model"] == "model-under-test"


async def test_anthropic_maps_truncation_and_refusal() -> None:
    truncated = await anthropic_adapter(
        _Recorder(result=_AnthropicResponse(stop_reason="max_tokens"))
    ).invoke(REQUEST, ENTRY)
    assert truncated.stop_reason is StopReason.MAX_TOKENS

    refused = await anthropic_adapter(
        _Recorder(result=_AnthropicResponse(stop_reason="refusal"))
    ).invoke(REQUEST, ENTRY)
    assert refused.stop_reason is StopReason.REFUSAL
    assert refused.error_category is ErrorCategory.CONTENT_FILTER


@pytest.mark.parametrize(
    ("exc", "expected", "retryable"),
    [
        (_timeout_error(anthropic), ErrorCategory.TIMEOUT, True),
        (_status_error(anthropic.RateLimitError, 429), ErrorCategory.RATE_LIMIT, True),
        (_status_error(anthropic.AuthenticationError, 401), ErrorCategory.AUTH, False),
        (_status_error(anthropic.BadRequestError, 400), ErrorCategory.BAD_REQUEST, False),
    ],
)
async def test_anthropic_errors_become_categories_not_exceptions(
    exc: Exception, expected: ErrorCategory, retryable: bool
) -> None:
    """Adapters must not raise: retry policy lives in the gateway, not here."""
    result = await anthropic_adapter(_Recorder(error=exc)).invoke(REQUEST, ENTRY)

    assert result.error_category is expected
    assert result.error_category.is_retryable is retryable
    assert result.stop_reason is StopReason.ERROR
    assert result.text == ""


async def test_anthropic_error_message_is_truncated() -> None:
    """Vendor error strings can echo request content; they are not logged whole."""
    request = httpx.Request("POST", "https://api.example.com/v1/messages")
    long_error = anthropic.BadRequestError(
        "x" * 5000, response=httpx.Response(400, request=request), body=None
    )
    result = await anthropic_adapter(_Recorder(error=long_error)).invoke(REQUEST, ENTRY)
    assert result.error_message is not None
    assert len(result.error_message) <= 200


# --- OpenAI -------------------------------------------------------------------


class _OpenAIDetails:
    def __init__(self) -> None:
        self.cached_tokens = 64


class _OpenAIUsage:
    def __init__(self) -> None:
        self.prompt_tokens = 200
        self.completion_tokens = 70
        self.prompt_tokens_details = _OpenAIDetails()


class _OpenAIChoice:
    def __init__(self, text: str, finish_reason: str) -> None:
        self.message = type("Msg", (), {"content": text})()
        self.finish_reason = finish_reason


class _OpenAIResponse:
    def __init__(self, text: str = "Osmosis is...", finish_reason: str = "stop") -> None:
        self.choices = [_OpenAIChoice(text, finish_reason)]
        self.usage = _OpenAIUsage()
        self.id = "req_openai_1"


def openai_adapter(recorder: _Recorder) -> OpenAIAdapter:
    adapter = OpenAIAdapter(api_key="test-key")
    completions = type("Comp", (), {"create": recorder._call})()
    chat = type("Chat", (), {"completions": completions})()
    adapter._client = type(  # type: ignore[assignment]
        "C", (), {"with_options": lambda _self, **_k: type("O", (), {"chat": chat})()}
    )()
    return adapter


async def test_openai_maps_a_successful_response() -> None:
    recorder = _Recorder(result=_OpenAIResponse())
    result = await openai_adapter(recorder).invoke(REQUEST, ENTRY)

    assert result.text == "Osmosis is..."
    assert result.input_tokens == 200
    assert result.output_tokens == 70
    assert result.cached_tokens == 64
    assert result.stop_reason is StopReason.END_TURN
    assert result.provider_request_id == "req_openai_1"


async def test_openai_puts_the_stable_prefix_first() -> None:
    """OpenAI keys automatic caching on a shared prefix, so order matters."""
    recorder = _Recorder(result=_OpenAIResponse())
    await openai_adapter(recorder).invoke(REQUEST, ENTRY)

    messages = recorder.kwargs["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"].startswith("STABLE SAFETY BLOCK")
    assert "You are a tutor." in messages[0]["content"]
    assert messages[1]["content"] == "What is osmosis?"


async def test_openai_maps_length_and_content_filter() -> None:
    truncated = await openai_adapter(
        _Recorder(result=_OpenAIResponse(finish_reason="length"))
    ).invoke(REQUEST, ENTRY)
    assert truncated.stop_reason is StopReason.MAX_TOKENS

    filtered = await openai_adapter(
        _Recorder(result=_OpenAIResponse(finish_reason="content_filter"))
    ).invoke(REQUEST, ENTRY)
    assert filtered.stop_reason is StopReason.REFUSAL
    assert filtered.error_category is ErrorCategory.CONTENT_FILTER


@pytest.mark.parametrize(
    ("exc", "expected", "retryable"),
    [
        (_timeout_error(openai), ErrorCategory.TIMEOUT, True),
        (_status_error(openai.RateLimitError, 429), ErrorCategory.RATE_LIMIT, True),
        (_status_error(openai.AuthenticationError, 401), ErrorCategory.AUTH, False),
        (_status_error(openai.BadRequestError, 400), ErrorCategory.BAD_REQUEST, False),
    ],
)
async def test_openai_errors_become_categories_not_exceptions(
    exc: Exception, expected: ErrorCategory, retryable: bool
) -> None:
    result = await openai_adapter(_Recorder(error=exc)).invoke(REQUEST, ENTRY)

    assert result.error_category is expected
    assert result.error_category.is_retryable is retryable
    assert result.stop_reason is StopReason.ERROR


async def test_both_adapters_agree_on_error_categories() -> None:
    """A vendor swap must not silently change retry behaviour."""
    a = await anthropic_adapter(_Recorder(error=_timeout_error(anthropic))).invoke(REQUEST, ENTRY)
    o = await openai_adapter(_Recorder(error=_timeout_error(openai))).invoke(REQUEST, ENTRY)
    assert a.error_category is o.error_category is ErrorCategory.TIMEOUT
