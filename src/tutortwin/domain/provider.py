"""Vendor-neutral model request/response schema.

Business code speaks only this. Vendor SDKs live behind adapters in
`tutortwin.providers`, so swapping or adding a vendor never reaches orchestration.

Model identity is an *alias* here (CHEAP_TEXT, STANDARD_TUTOR, ...). The concrete
vendor model ID is resolved from the `model_catalog` table at call time and only
appears on the *response*, as a record of what actually ran.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ModelAlias(StrEnum):
    CHEAP_TEXT = "CHEAP_TEXT"
    STANDARD_TUTOR = "STANDARD_TUTOR"
    ADVANCED_REASONING = "ADVANCED_REASONING"
    VERIFIER_PRIMARY = "VERIFIER_PRIMARY"
    VERIFIER_SECONDARY = "VERIFIER_SECONDARY"
    VISION = "VISION"
    TRANSCRIBE = "TRANSCRIBE"
    EMBEDDING = "EMBEDDING"


class Provider(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    FAKE = "fake"


class StopReason(StrEnum):
    END_TURN = "END_TURN"
    MAX_TOKENS = "MAX_TOKENS"
    REFUSAL = "REFUSAL"
    ERROR = "ERROR"
    OTHER = "OTHER"


class ErrorCategory(StrEnum):
    """Coarse categories the retry/fallback policy branches on."""

    NONE = "NONE"
    TIMEOUT = "TIMEOUT"
    RATE_LIMIT = "RATE_LIMIT"
    SERVER_ERROR = "SERVER_ERROR"
    BAD_REQUEST = "BAD_REQUEST"
    AUTH = "AUTH"
    CONTENT_FILTER = "CONTENT_FILTER"
    UNKNOWN = "UNKNOWN"

    @property
    def is_retryable(self) -> bool:
        return self in {
            ErrorCategory.TIMEOUT,
            ErrorCategory.RATE_LIMIT,
            ErrorCategory.SERVER_ERROR,
        }


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ImagePart(Frozen):
    """One image in a multimodal message.

    Carries raw bytes rather than a URL: the vendor must not be handed a signed
    link to a student's private object, and base64 encoding is the adapter's
    concern, not the caller's.
    """

    data: bytes
    media_type: str = Field(pattern="^image/(png|jpeg|webp|gif)$")

    @property
    def size_bytes(self) -> int:
        return len(self.data)


class ModelMessage(Frozen):
    role: str = Field(pattern="^(system|user|assistant)$")
    content: str
    images: tuple[ImagePart, ...] = ()
    """Non-empty only for vision requests. Text-only capabilities never set it,
    so a text model can never accidentally be handed an image."""


class ModelRequest(Frozen):
    """What business code asks for. No vendor concept appears here."""

    alias: ModelAlias
    messages: tuple[ModelMessage, ...]
    max_output_tokens: int = Field(ge=1, le=32_000)
    system: str | None = None
    # Stable prefix eligible for provider-side prompt caching. Kept separate from
    # `system` so a volatile per-request suffix cannot invalidate the cached part.
    cacheable_prefix: str | None = None
    timeout_seconds: float = Field(default=30.0, gt=0, le=300)


class ModelCall(Frozen):
    """The full record of one provider call - success or failure.

    Exactly one of these is produced per attempt, and exactly one usage_ledger row
    is written from it. Failed attempts are recorded too: a timeout that burned
    input tokens still costs money, and an invisible failure is how retry storms
    hide.
    """

    alias: ModelAlias
    provider: Provider
    model_id: str
    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    latency_ms: int = 0
    provider_request_id: str | None = None
    stop_reason: StopReason = StopReason.END_TURN
    error_category: ErrorCategory = ErrorCategory.NONE
    error_message: str | None = None
    estimated_cost_micros: int = 0
    rate_version: str = "v1"
    attempt: int = 1

    @property
    def succeeded(self) -> bool:
        return self.error_category is ErrorCategory.NONE


class ModelCatalogEntry(Frozen):
    """alias -> vendor model, with the price that applied when it was read."""

    alias: ModelAlias
    provider: Provider
    model_id: str
    input_cost_micros_per_1k: int
    output_cost_micros_per_1k: int
    rate_version: str

    def cost_micros(self, input_tokens: int, output_tokens: int) -> int:
        """Cost in micro-dollars, rounded up.

        Rounding up rather than truncating keeps the ledger from systematically
        under-reporting spend across many small calls.
        """
        total = (
            input_tokens * self.input_cost_micros_per_1k
            + output_tokens * self.output_cost_micros_per_1k
        )
        return -(-total // 1000)
