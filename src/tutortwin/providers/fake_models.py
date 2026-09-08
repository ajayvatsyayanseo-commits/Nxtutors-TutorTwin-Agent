"""Deterministic fake model providers.

Scriptable, so a test can pin exactly what the model "said" and force specific
failure modes (timeout, rate limit, refusal, truncation) without a network call
or an API key. This is what makes the Phase 02 test suite provable rather than
approximate.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from tutortwin.domain.provider import (
    ErrorCategory,
    ModelCatalogEntry,
    ModelRequest,
    Provider,
    StopReason,
)
from tutortwin.providers.gateway import ProviderResponse


@dataclass(slots=True)
class ScriptedReply:
    text: str = "Here is an explanation."
    input_tokens: int = 120
    output_tokens: int = 60
    cached_tokens: int = 0
    stop_reason: StopReason = StopReason.END_TURN
    error_category: ErrorCategory = ErrorCategory.NONE
    error_message: str | None = None
    latency_ms: int = 10


@dataclass(slots=True)
class FakeModelProvider:
    """Returns queued replies in order; falls back to `default` when drained.

    Every call is recorded, so tests assert on the exact number of provider calls
    made - which is how "zero paid calls for an ineligible student" is proved.
    """

    name: Provider = Provider.FAKE
    queue: deque[ScriptedReply] = field(default_factory=deque)
    default: ScriptedReply = field(default_factory=ScriptedReply)
    calls: list[ModelRequest] = field(default_factory=list)

    def script(self, *replies: ScriptedReply) -> None:
        self.queue.extend(replies)

    @property
    def call_count(self) -> int:
        return len(self.calls)

    async def invoke(self, request: ModelRequest, entry: ModelCatalogEntry) -> ProviderResponse:
        self.calls.append(request)
        reply = self.queue.popleft() if self.queue else self.default
        return ProviderResponse(
            text=reply.text,
            input_tokens=reply.input_tokens,
            output_tokens=reply.output_tokens,
            cached_tokens=reply.cached_tokens,
            latency_ms=reply.latency_ms,
            provider_request_id=f"fake_req_{len(self.calls):04d}",
            stop_reason=reply.stop_reason,
            error_category=reply.error_category,
            error_message=reply.error_message,
        )


def timeout_reply() -> ScriptedReply:
    return ScriptedReply(
        text="",
        input_tokens=100,
        output_tokens=0,
        stop_reason=StopReason.ERROR,
        error_category=ErrorCategory.TIMEOUT,
        error_message="request timed out",
    )


def rate_limited_reply() -> ScriptedReply:
    return ScriptedReply(
        text="",
        input_tokens=0,
        output_tokens=0,
        stop_reason=StopReason.ERROR,
        error_category=ErrorCategory.RATE_LIMIT,
        error_message="429 too many requests",
    )


def truncated_reply(text: str = "The first step is") -> ScriptedReply:
    return ScriptedReply(text=text, stop_reason=StopReason.MAX_TOKENS)


def refusal_reply() -> ScriptedReply:
    return ScriptedReply(
        text="",
        stop_reason=StopReason.REFUSAL,
        error_category=ErrorCategory.CONTENT_FILTER,
    )
