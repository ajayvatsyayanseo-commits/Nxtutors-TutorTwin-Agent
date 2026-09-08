"""The model gateway: one internal call surface over every vendor.

Two invariants this module owns:

1. **No vendor concept escapes.** Callers pass a `ModelAlias`; the catalog turns
   it into a vendor + model ID. Business code never sees a model string.
2. **Exactly one `ModelCall` per attempt.** Retries and fallbacks each produce
   their own record, including failures. The caller writes one ledger row per
   returned record, so cost accounting cannot drift from what actually happened.

The gateway deliberately does no database work. Its caller holds the session and
writes the ledger *after* the network call returns, because holding a Postgres
transaction open across a 30-second provider call is exactly the serverless
failure mode the architecture forbids.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Protocol

from tutortwin.domain.provider import (
    ErrorCategory,
    ModelAlias,
    ModelCall,
    ModelCatalogEntry,
    ModelRequest,
    Provider,
    StopReason,
)
from tutortwin.observability.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class ProviderResponse:
    """What an adapter returns. Vendor-shaped fields already normalized."""

    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    latency_ms: int = 0
    provider_request_id: str | None = None
    stop_reason: StopReason = StopReason.END_TURN
    error_category: ErrorCategory = ErrorCategory.NONE
    error_message: str | None = None


class ModelAdapter(Protocol):
    """Implemented by OpenAIAdapter, AnthropicAdapter and FakeModelProvider.

    An adapter must not raise for provider-side failures: it returns a response
    carrying an `error_category`. That keeps retry policy in one place instead of
    scattered across two vendors' exception hierarchies.
    """

    async def invoke(self, request: ModelRequest, entry: ModelCatalogEntry) -> ProviderResponse: ...


class ModelUnavailableError(RuntimeError):
    """No active catalog entry, or no adapter for that vendor."""


RETRY_BASE_SECONDS = 0.5
RETRY_MAX_SECONDS = 8.0


def retry_delay(attempt: int, *, jitter: bool = True) -> float:
    """Backoff before attempt `attempt` (1-based).

    An immediate retry against a vendor returning 429 is a request that will be
    rejected again, billed or not, and it arrives while the rate limit that
    caused the first rejection is still in force.

    Full jitter, because a provider outage fails every in-flight request at once:
    without it, all of them retry on the same second and the vendor's recovery is
    met with the same spike that it is recovering from.
    """
    if attempt <= 1:
        return 0.0
    exponential = min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * (2 ** (attempt - 2)))
    return random.uniform(0, exponential) if jitter else exponential  # noqa: S311 - backoff


@dataclass(slots=True)
class GatewayResult:
    """Outcome of one logical request, plus every attempt it took."""

    call: ModelCall | None
    """The successful call, or None if every attempt failed."""

    attempts: list[ModelCall] = field(default_factory=list)
    """Every attempt, in order. One ledger row is written per entry."""

    @property
    def succeeded(self) -> bool:
        return self.call is not None

    @property
    def paid_call_count(self) -> int:
        """Attempts that actually reached a vendor. Drives usage reporting."""
        return len(self.attempts)


class ModelGateway:
    def __init__(
        self,
        adapters: dict[Provider, ModelAdapter],
        catalog: dict[ModelAlias, ModelCatalogEntry],
    ) -> None:
        self._adapters = adapters
        self._catalog = catalog

    def resolve(self, alias: ModelAlias) -> ModelCatalogEntry:
        entry = self._catalog.get(alias)
        if entry is None:
            raise ModelUnavailableError(f"No active catalog entry for alias {alias}.")
        return entry

    async def invoke(
        self,
        request: ModelRequest,
        *,
        max_attempts: int = 2,
        fallback_alias: ModelAlias | None = None,
        blocked_providers: frozenset[str] = frozenset(),
    ) -> GatewayResult:
        """Run the request, retrying and falling back within a hard attempt cap.

        `max_attempts` bounds total vendor calls across both the primary and the
        fallback alias - never per-alias, or a two-alias policy would silently
        double the ceiling.

        `blocked_providers` is the per-vendor cost ceiling and circuit breaker,
        enforced here because this is the only layer that knows which vendor an
        alias resolves to. A blocked vendor is *skipped*, not refused: falling
        through to a healthy one is the whole reason the ceiling is per-vendor
        rather than global.
        """
        result = GatewayResult(call=None)
        aliases = [request.alias] + ([fallback_alias] if fallback_alias else [])

        for index, alias in enumerate(aliases):
            try:
                entry = self.resolve(alias)
            except ModelUnavailableError:
                logger.warning("model_alias_unavailable", alias=str(alias))
                continue

            if str(entry.provider) in blocked_providers:
                # Over its daily budget, or failing. Either way, paying it again
                # is money for a result we have reason to believe will not come.
                logger.info("provider_blocked", provider=str(entry.provider), alias=str(alias))
                continue

            adapter = self._adapters.get(entry.provider)
            if adapter is None:
                logger.warning("adapter_missing", provider=str(entry.provider))
                continue

            # Reserve one attempt for each alias still to try. Without this, the
            # primary's retries consume the whole budget and the fallback never
            # runs - which silently turns "fall back to another model" into
            # "retry the same failing model twice".
            remaining_aliases = len(aliases) - index - 1
            attempt_ceiling = max(len(result.attempts) + 1, max_attempts - remaining_aliases)

            while len(result.attempts) < attempt_ceiling:
                attempt_no = len(result.attempts) + 1
                attempt_request = (
                    request
                    if alias is request.alias
                    else request.model_copy(update={"alias": alias})
                )
                response = await adapter.invoke(attempt_request, entry)
                call = _to_model_call(response, entry, alias, attempt_no)
                result.attempts.append(call)

                if call.succeeded:
                    result.call = call
                    return result

                if not call.error_category.is_retryable:
                    # A bad request or a content refusal will fail identically on
                    # retry; spending again on it is pure waste.
                    logger.info(
                        "provider_error_not_retryable",
                        alias=str(alias),
                        error_category=str(call.error_category),
                    )
                    break

                logger.info(
                    "provider_attempt_failed",
                    alias=str(alias),
                    attempt=attempt_no,
                    error_category=str(call.error_category),
                )

                # Wait before trying again, but only if there is a next attempt:
                # sleeping after the final failure delays the student's error
                # message and buys nothing.
                if len(result.attempts) < attempt_ceiling:
                    await asyncio.sleep(retry_delay(attempt_no + 1))

            if len(result.attempts) >= max_attempts:
                break

        return result


def _to_model_call(
    response: ProviderResponse,
    entry: ModelCatalogEntry,
    alias: ModelAlias,
    attempt: int,
) -> ModelCall:
    """Price the attempt against the catalog entry that was actually used."""
    cost = entry.cost_micros(response.input_tokens, response.output_tokens)
    return ModelCall(
        alias=alias,
        provider=entry.provider,
        model_id=entry.model_id,
        text=response.text,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        cached_tokens=response.cached_tokens,
        latency_ms=response.latency_ms,
        provider_request_id=response.provider_request_id,
        stop_reason=response.stop_reason,
        error_category=response.error_category,
        error_message=response.error_message,
        estimated_cost_micros=cost,
        rate_version=entry.rate_version,
        attempt=attempt,
    )
