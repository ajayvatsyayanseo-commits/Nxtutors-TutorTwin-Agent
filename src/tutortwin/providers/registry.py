"""Adapter selection.

Keeps vendor SDK imports out of the composition root: `dependencies.py` asks for
a gateway factory and never learns which vendors exist. Adapters are constructed
lazily so a deployment with no keys never imports a vendor client at all.
"""

from __future__ import annotations

from collections.abc import Callable

from tutortwin.config import Settings
from tutortwin.domain.provider import ModelAlias, ModelCatalogEntry, Provider
from tutortwin.observability.logging import get_logger
from tutortwin.providers.gateway import ModelAdapter, ModelGateway

logger = get_logger(__name__)


def build_adapters(settings: Settings) -> dict[Provider, ModelAdapter]:
    """Only vendors with a configured key are wired.

    A missing key yields no adapter rather than a client that fails on every
    call - the budget decision then has nowhere to spend, which is the safe
    direction to fail.
    """
    adapters: dict[Provider, ModelAdapter] = {}

    if settings.anthropic_api_key is not None:
        from tutortwin.providers.anthropic_adapter import AnthropicAdapter

        adapters[Provider.ANTHROPIC] = AnthropicAdapter(
            settings.anthropic_api_key.get_secret_value()
        )
    if settings.openai_api_key is not None:
        from tutortwin.providers.openai_adapter import OpenAIAdapter

        adapters[Provider.OPENAI] = OpenAIAdapter(settings.openai_api_key.get_secret_value())

    return adapters


def build_gateway_factory(
    settings: Settings,
) -> Callable[[], ModelGateway | None] | None:
    """Factory used per request.

    Returns None when no vendor is configured, which the entry service treats as
    "answer deterministically" rather than an error.

    The catalog passed here is a static snapshot rather than a per-request DB
    read: Phase 02 seeds it in a migration, and re-reading it on every request
    would add a query to the hot path for data that changes on deploys. Phase 06
    (admin control plane) makes it live.
    """
    adapters = build_adapters(settings)
    if not adapters:
        logger.info("no_model_provider_configured")
        return None

    catalog = default_catalog(adapters)
    if not catalog:
        return None

    def factory() -> ModelGateway | None:
        return ModelGateway(adapters, catalog)

    return factory


# Seed catalog. Model IDs live HERE and in the seed migration only - never in
# business code, which speaks aliases. Prices are micro-dollars per 1k tokens.
_ANTHROPIC_SEED: dict[ModelAlias, tuple[str, int, int]] = {
    ModelAlias.CHEAP_TEXT: ("claude-haiku-4-5", 1000, 5000),
    ModelAlias.STANDARD_TUTOR: ("claude-sonnet-5", 2000, 10000),
    ModelAlias.ADVANCED_REASONING: ("claude-opus-5", 5000, 25000),
    ModelAlias.VERIFIER_PRIMARY: ("claude-sonnet-5", 2000, 10000),
    ModelAlias.VERIFIER_SECONDARY: ("claude-haiku-4-5", 1000, 5000),
}

_OPENAI_SEED: dict[ModelAlias, tuple[str, int, int]] = {
    ModelAlias.CHEAP_TEXT: ("gpt-4.1-mini", 400, 1600),
    ModelAlias.STANDARD_TUTOR: ("gpt-4.1", 2000, 8000),
    ModelAlias.ADVANCED_REASONING: ("o4-mini", 1100, 4400),
    ModelAlias.VERIFIER_PRIMARY: ("gpt-4.1", 2000, 8000),
    ModelAlias.VERIFIER_SECONDARY: ("gpt-4.1-mini", 400, 1600),
}

RATE_VERSION = "2026-08"


def default_catalog(
    adapters: dict[Provider, ModelAdapter],
) -> dict[ModelAlias, ModelCatalogEntry]:
    """Anthropic preferred when both are configured; OpenAI fills any gaps."""
    catalog: dict[ModelAlias, ModelCatalogEntry] = {}

    for provider, seed in (
        (Provider.ANTHROPIC, _ANTHROPIC_SEED),
        (Provider.OPENAI, _OPENAI_SEED),
    ):
        if provider not in adapters:
            continue
        for alias, (model_id, cost_in, cost_out) in seed.items():
            if alias in catalog:
                continue
            catalog[alias] = ModelCatalogEntry(
                alias=alias,
                provider=provider,
                model_id=model_id,
                input_cost_micros_per_1k=cost_in,
                output_cost_micros_per_1k=cost_out,
                rate_version=RATE_VERSION,
            )
    return catalog
