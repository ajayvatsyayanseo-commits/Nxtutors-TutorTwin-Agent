"""Media domain: the state machine, limits, and extraction plan types.

The state machine exists to make accidental spending *structurally* hard rather
than merely discouraged. Every expensive operation lives behind a transition, so
"did this OCR run" is answerable by reading a row rather than by auditing code
paths.

The ordering is the cost policy:

    RECEIVED_REFERENCE      a pointer, no bytes, no cost
    ENTITLEMENT_CHECKED     plan verified BEFORE anything is fetched
    WAITING_FOR_BRIEF       held indefinitely at zero cost
    BRIEF_RECEIVED          the student said what they want
    FETCH_QUEUED / FETCHED  first byte of network cost
    VALIDATED              MIME sniffed, limits enforced
    EXTRACTION_PLANNED      which pages, which method - decided locally
    EXTRACTING              local text, then OCR, then vision, in that order
    READY_FOR_CAPABILITY    extraction available to the tutor
    COMPLETED
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class MediaState(StrEnum):
    RECEIVED_REFERENCE = "RECEIVED_REFERENCE"
    ENTITLEMENT_CHECKED = "ENTITLEMENT_CHECKED"
    WAITING_FOR_BRIEF = "WAITING_FOR_BRIEF"
    BRIEF_RECEIVED = "BRIEF_RECEIVED"
    FETCH_QUEUED = "FETCH_QUEUED"
    FETCHED = "FETCHED"
    VALIDATED = "VALIDATED"
    EXTRACTION_PLANNED = "EXTRACTION_PLANNED"
    EXTRACTING = "EXTRACTING"
    READY_FOR_CAPABILITY = "READY_FOR_CAPABILITY"
    COMPLETED = "COMPLETED"
    # Terminal
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


TERMINAL_STATES: frozenset[MediaState] = frozenset(
    {MediaState.REJECTED, MediaState.FAILED, MediaState.EXPIRED, MediaState.COMPLETED}
)

# States at which no byte has been fetched and nothing has been spent. Asserted
# directly in the cost tests.
ZERO_COST_STATES: frozenset[MediaState] = frozenset(
    {
        MediaState.RECEIVED_REFERENCE,
        MediaState.ENTITLEMENT_CHECKED,
        MediaState.WAITING_FOR_BRIEF,
        MediaState.BRIEF_RECEIVED,
        MediaState.REJECTED,
    }
)

# Explicit adjacency. A transition not listed here cannot happen, which is what
# stops a future refactor from quietly routing around the brief gate.
_ALLOWED: dict[MediaState, frozenset[MediaState]] = {
    MediaState.RECEIVED_REFERENCE: frozenset(
        {MediaState.ENTITLEMENT_CHECKED, MediaState.REJECTED, MediaState.EXPIRED}
    ),
    MediaState.ENTITLEMENT_CHECKED: frozenset(
        {
            MediaState.WAITING_FOR_BRIEF,
            MediaState.BRIEF_RECEIVED,
            MediaState.REJECTED,
            MediaState.EXPIRED,
        }
    ),
    MediaState.WAITING_FOR_BRIEF: frozenset(
        {MediaState.BRIEF_RECEIVED, MediaState.EXPIRED, MediaState.REJECTED}
    ),
    MediaState.BRIEF_RECEIVED: frozenset(
        {MediaState.FETCH_QUEUED, MediaState.REJECTED, MediaState.EXPIRED}
    ),
    MediaState.FETCH_QUEUED: frozenset({MediaState.FETCHED, MediaState.FAILED, MediaState.EXPIRED}),
    MediaState.FETCHED: frozenset({MediaState.VALIDATED, MediaState.REJECTED, MediaState.FAILED}),
    MediaState.VALIDATED: frozenset(
        {MediaState.EXTRACTION_PLANNED, MediaState.REJECTED, MediaState.FAILED}
    ),
    MediaState.EXTRACTION_PLANNED: frozenset({MediaState.EXTRACTING, MediaState.FAILED}),
    MediaState.EXTRACTING: frozenset({MediaState.READY_FOR_CAPABILITY, MediaState.FAILED}),
    MediaState.READY_FOR_CAPABILITY: frozenset(
        {MediaState.COMPLETED, MediaState.FAILED, MediaState.EXPIRED}
    ),
    MediaState.COMPLETED: frozenset({MediaState.EXPIRED}),
    MediaState.REJECTED: frozenset(),
    MediaState.FAILED: frozenset(),
    MediaState.EXPIRED: frozenset(),
}


class InvalidTransition(ValueError):
    def __init__(self, current: MediaState, target: MediaState) -> None:
        super().__init__(f"Cannot move media from {current} to {target}.")
        self.current = current
        self.target = target


def can_transition(current: MediaState, target: MediaState) -> bool:
    """Idempotent: staying put is always allowed, so a retried job is harmless."""
    if current is target:
        return True
    return target in _ALLOWED[current]


def assert_transition(current: MediaState, target: MediaState) -> None:
    if not can_transition(current, target):
        raise InvalidTransition(current, target)


class MediaKind(StrEnum):
    IMAGE = "IMAGE"
    PDF = "PDF"
    AUDIO = "AUDIO"
    DOCUMENT = "DOCUMENT"


class RejectReason(StrEnum):
    ENTITLEMENT = "ENTITLEMENT"
    TOO_LARGE = "TOO_LARGE"
    TOO_MANY_PAGES = "TOO_MANY_PAGES"
    TOO_LONG = "TOO_LONG"
    UNSUPPORTED_MIME = "UNSUPPORTED_MIME"
    EXECUTABLE = "EXECUTABLE"
    ARCHIVE = "ARCHIVE"
    MIME_MISMATCH = "MIME_MISMATCH"
    CORRUPT = "CORRUPT"
    DIMENSIONS = "DIMENSIONS"
    DECOMPRESSION_BOMB = "DECOMPRESSION_BOMB"
    ENCRYPTED = "ENCRYPTED"
    DAILY_ALLOWANCE = "DAILY_ALLOWANCE"
    """The student's own daily ceiling for this kind of media - pages, OCR
    pages, voice seconds or mock tests. Distinct from ENTITLEMENT, which means
    they may not use the feature at all: this one means "not any more today",
    and the reply says so."""


class ExtractionMethod(StrEnum):
    """Ordered cheapest-first. The planner never skips to a costlier method
    without recording why."""

    DIGITAL_TEXT = "DIGITAL_TEXT"
    LOCAL_OCR = "LOCAL_OCR"
    VISION = "VISION"

    TRANSCRIPTION = "TRANSCRIPTION"
    """Speech, not pages. It sits outside the cheapest-first ordering above
    because a voice note has no cheaper reader to try first - there is no local
    engine and no text layer, so it is transcribe or nothing."""


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MediaLimits(Frozen):
    """Hard caps. Enforced before any expensive work, never after."""

    max_image_bytes: int = 8 * 1024 * 1024
    max_pdf_bytes: int = 20 * 1024 * 1024
    max_audio_bytes: int = 16 * 1024 * 1024
    max_document_bytes: int = 8 * 1024 * 1024

    max_pdf_pages: int = 200
    """Refuse the document outright beyond this."""

    max_pages_processed: int = 5
    """Pages any single request may extract. The reason a 100-page PDF never
    reaches a frontier model because one equation was asked about."""

    max_ocr_pages: int = 3
    max_vision_pages: int = 2
    max_audio_seconds: int = 300

    max_image_pixels: int = 40_000_000
    """Decompression-bomb guard: a 5KB PNG can decode to gigabytes."""

    max_image_dimension: int = 12_000


DEFAULT_LIMITS = MediaLimits()


class PagePlan(Frozen):
    """One page's extraction decision, with the reason it was chosen."""

    page_number: int = Field(ge=1)
    method: ExtractionMethod
    reason: str


class ExtractionPlan(Frozen):
    """What will be extracted, decided locally before anything costly runs.

    `pages` is already truncated to the limits, so executing the plan cannot
    exceed budget - the cap is applied at planning time, not hoped for at
    execution time.
    """

    pages: tuple[PagePlan, ...] = ()
    total_pages: int = 0
    targeting_reason: str = ""

    @property
    def ocr_page_count(self) -> int:
        return sum(1 for p in self.pages if p.method is ExtractionMethod.LOCAL_OCR)

    @property
    def vision_page_count(self) -> int:
        return sum(1 for p in self.pages if p.method is ExtractionMethod.VISION)

    @property
    def digital_page_count(self) -> int:
        return sum(1 for p in self.pages if p.method is ExtractionMethod.DIGITAL_TEXT)


class PageExtraction(Frozen):
    page_number: int
    method: ExtractionMethod
    text: str
    confidence: float | None = None
    """OCR confidence 0-1 where the engine reports it; None for digital text."""


class ExtractionResult(Frozen):
    pages: tuple[PageExtraction, ...] = ()
    parser_version: str = ""
    ocr_version: str | None = None
    escalated_to_vision: bool = False
    escalation_reason: str | None = None

    @property
    def text(self) -> str:
        return "\n\n".join(
            f"[page {p.page_number}]\n{p.text}" for p in self.pages if p.text.strip()
        )
