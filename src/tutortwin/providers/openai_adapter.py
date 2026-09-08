"""OpenAI adapter.

The only file in the codebase that imports the OpenAI SDK. Mirrors the Anthropic
adapter's contract exactly: errors are normalized into `ErrorCategory` rather
than raised, so the gateway's retry policy is vendor-agnostic.
"""

from __future__ import annotations

import base64
import time
from typing import Any

import openai

from tutortwin.domain.provider import (
    ErrorCategory,
    ModelCatalogEntry,
    ModelMessage,
    ModelRequest,
    StopReason,
)
from tutortwin.providers.gateway import ProviderResponse

_FINISH_REASONS: dict[str, StopReason] = {
    "stop": StopReason.END_TURN,
    "length": StopReason.MAX_TOKENS,
    "content_filter": StopReason.REFUSAL,
    "tool_calls": StopReason.OTHER,
}


class OpenAIAdapter:
    def __init__(self, api_key: str) -> None:
        self._client = openai.AsyncOpenAI(api_key=api_key)

    async def invoke(self, request: ModelRequest, entry: ModelCatalogEntry) -> ProviderResponse:
        started = time.monotonic()

        # OpenAI has no separate system field: the system prompt is the first
        # message. The cacheable prefix leads so the stable span is a shared
        # prefix across turns, which is what its automatic caching keys on.
        messages: list[dict[str, str]] = []
        system_text = "\n\n".join(
            part for part in (request.cacheable_prefix, request.system) if part
        )
        if system_text:
            messages.append({"role": "system", "content": system_text})
        messages.extend(_message_payload(m) for m in request.messages)

        try:
            response = await self._client.with_options(
                timeout=request.timeout_seconds
            ).chat.completions.create(
                model=entry.model_id,
                messages=messages,  # type: ignore[arg-type]
                max_completion_tokens=request.max_output_tokens,
            )
        except openai.APITimeoutError as exc:
            return _error(ErrorCategory.TIMEOUT, str(exc), started)
        except openai.RateLimitError as exc:
            return _error(ErrorCategory.RATE_LIMIT, str(exc), started)
        except openai.AuthenticationError as exc:
            return _error(ErrorCategory.AUTH, str(exc), started)
        except openai.BadRequestError as exc:
            return _error(ErrorCategory.BAD_REQUEST, str(exc), started)
        except openai.APIStatusError as exc:
            category = (
                ErrorCategory.SERVER_ERROR if exc.status_code >= 500 else ErrorCategory.BAD_REQUEST
            )
            return _error(category, str(exc), started)
        except openai.APIConnectionError as exc:
            return _error(ErrorCategory.TIMEOUT, str(exc), started)

        choice = response.choices[0] if response.choices else None
        text = (choice.message.content or "") if choice else ""
        finish = (choice.finish_reason or "") if choice else ""
        usage = response.usage

        cached = 0
        if usage is not None and usage.prompt_tokens_details is not None:
            cached = usage.prompt_tokens_details.cached_tokens or 0

        return ProviderResponse(
            text=text,
            input_tokens=(usage.prompt_tokens if usage else 0) or 0,
            output_tokens=(usage.completion_tokens if usage else 0) or 0,
            cached_tokens=cached,
            latency_ms=_elapsed_ms(started),
            provider_request_id=response.id,
            stop_reason=_FINISH_REASONS.get(finish, StopReason.OTHER),
            error_category=(
                ErrorCategory.CONTENT_FILTER if finish == "content_filter" else ErrorCategory.NONE
            ),
        )


def _message_payload(message: ModelMessage) -> dict[str, Any]:
    """Images become data-URL content parts.

    A data URL rather than a link: the vendor must never receive a signed URL to
    a student's private object.
    """
    if not message.images:
        return {"role": message.role, "content": message.content}

    parts: list[dict[str, Any]] = [
        {
            "type": "image_url",
            "image_url": {
                "url": (
                    f"data:{image.media_type};base64,"
                    f"{base64.standard_b64encode(image.data).decode('ascii')}"
                )
            },
        }
        for image in message.images
    ]
    parts.append({"type": "text", "text": message.content})
    return {"role": message.role, "content": parts}


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _error(category: ErrorCategory, message: str, started: float) -> ProviderResponse:
    return ProviderResponse(
        text="",
        latency_ms=_elapsed_ms(started),
        stop_reason=StopReason.ERROR,
        error_category=category,
        error_message=message[:200],
    )
