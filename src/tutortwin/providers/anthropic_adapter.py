"""Anthropic adapter.

The only file in the codebase that imports the Anthropic SDK. It converts a
`ModelRequest` into a Messages API call and normalizes the reply - including
errors, which become an `ErrorCategory` rather than an exception, so retry policy
lives in the gateway instead of being split across two vendors' exception trees.
"""

from __future__ import annotations

import base64
import time
from typing import Any

import anthropic

from tutortwin.domain.provider import (
    ErrorCategory,
    ModelCatalogEntry,
    ModelMessage,
    ModelRequest,
    StopReason,
)
from tutortwin.providers.gateway import ProviderResponse

_STOP_REASONS: dict[str, StopReason] = {
    "end_turn": StopReason.END_TURN,
    "max_tokens": StopReason.MAX_TOKENS,
    "stop_sequence": StopReason.END_TURN,
    "tool_use": StopReason.OTHER,
    "refusal": StopReason.REFUSAL,
    "pause_turn": StopReason.OTHER,
}


class AnthropicAdapter:
    def __init__(self, api_key: str) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    async def invoke(self, request: ModelRequest, entry: ModelCatalogEntry) -> ProviderResponse:
        started = time.monotonic()
        try:
            kwargs: dict[str, Any] = {
                "model": entry.model_id,
                "max_tokens": request.max_output_tokens,
                "messages": [_message_payload(m) for m in request.messages if m.role != "system"],
            }
            system = _build_system(request)
            if system:
                kwargs["system"] = system

            response = await self._client.with_options(
                timeout=request.timeout_seconds
            ).messages.create(**kwargs)
        except anthropic.APITimeoutError as exc:
            return _error(ErrorCategory.TIMEOUT, str(exc), started)
        except anthropic.RateLimitError as exc:
            return _error(ErrorCategory.RATE_LIMIT, str(exc), started)
        except anthropic.AuthenticationError as exc:
            return _error(ErrorCategory.AUTH, str(exc), started)
        except anthropic.BadRequestError as exc:
            return _error(ErrorCategory.BAD_REQUEST, str(exc), started)
        except anthropic.APIStatusError as exc:
            category = (
                ErrorCategory.SERVER_ERROR if exc.status_code >= 500 else ErrorCategory.BAD_REQUEST
            )
            return _error(category, str(exc), started)
        except anthropic.APIConnectionError as exc:
            return _error(ErrorCategory.TIMEOUT, str(exc), started)

        text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )
        usage = response.usage
        return ProviderResponse(
            text=text,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cached_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            latency_ms=_elapsed_ms(started),
            provider_request_id=getattr(response, "_request_id", None),
            stop_reason=_STOP_REASONS.get(response.stop_reason or "", StopReason.OTHER),
            error_category=(
                ErrorCategory.CONTENT_FILTER
                if response.stop_reason == "refusal"
                else ErrorCategory.NONE
            ),
        )


def _message_payload(message: ModelMessage) -> dict[str, Any]:
    """Text-only messages stay plain strings so existing behaviour is unchanged.

    Images become content blocks, base64-encoded here rather than by the caller -
    encoding is a vendor transport detail.
    """
    if not message.images:
        return {"role": message.role, "content": message.content}

    blocks: list[dict[str, Any]] = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": image.media_type,
                "data": base64.standard_b64encode(image.data).decode("ascii"),
            },
        }
        for image in message.images
    ]
    blocks.append({"type": "text", "text": message.content})
    return {"role": message.role, "content": blocks}


def _build_system(request: ModelRequest) -> list[dict[str, Any]] | None:
    """Split the system prompt so the stable half can be cached.

    `cacheable_prefix` is marked ephemeral; the volatile remainder follows it.
    Anything before the breakpoint is billed at roughly a tenth of the input rate
    on a hit, which is the largest single cost lever available here.
    """
    blocks: list[dict[str, Any]] = []
    remainder = request.system or ""

    if request.cacheable_prefix:
        blocks.append(
            {
                "type": "text",
                "text": request.cacheable_prefix,
                "cache_control": {"type": "ephemeral"},
            }
        )
        # The prefix is a literal prefix OF the system prompt, not a separate
        # document. Appending the whole system prompt after it sent those same
        # ~380 tokens twice on every paid call, and defeated the cache marker
        # into the bargain: the "cached" text was also sitting in the uncached
        # block right behind it, so it was billed at full rate regardless.
        if remainder.startswith(request.cacheable_prefix):
            remainder = remainder[len(request.cacheable_prefix) :].lstrip("\n")

    if remainder:
        blocks.append({"type": "text", "text": remainder})
    return blocks or None


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _error(category: ErrorCategory, message: str, started: float) -> ProviderResponse:
    return ProviderResponse(
        text="",
        latency_ms=_elapsed_ms(started),
        stop_reason=StopReason.ERROR,
        error_category=category,
        # Truncated: vendor error strings can echo request content back.
        error_message=message[:200],
    )
