"""When to retrieve, and when not to.

The default is **not to retrieve**. "Solve 2x + 5 = 13" needs no corpus; running
a vector search for it costs an embedding call and adds latency to answer a
question the model already knows. RAG earns its cost only when the answer
depends on material the model has not seen.

This is a pure function - no I/O, no model call - so every decision is
explainable and testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from tutortwin.domain.capabilities import CapabilityId


class RagDecision(StrEnum):
    RETRIEVE = "RETRIEVE"
    SKIP = "SKIP"


class RagReason(StrEnum):
    # Retrieve
    REFERS_TO_UPLOAD = "REFERS_TO_UPLOAD"
    ASKS_FOR_SOURCE = "ASKS_FOR_SOURCE"
    COURSE_OR_SYLLABUS = "COURSE_OR_SYLLABUS"
    TUTOR_SPECIFIC = "TUTOR_SPECIFIC"
    ACTIVE_DOCUMENT_CONTEXT = "ACTIVE_DOCUMENT_CONTEXT"
    TEST_GENERATION = "TEST_GENERATION"
    # Skip
    SELF_CONTAINED_COMPUTATION = "SELF_CONTAINED_COMPUTATION"
    GENERAL_KNOWLEDGE = "GENERAL_KNOWLEDGE"
    NO_CORPUS_AVAILABLE = "NO_CORPUS_AVAILABLE"
    FOLLOW_UP_USES_EXISTING_CONTEXT = "FOLLOW_UP_USES_EXISTING_CONTEXT"
    BUDGET_FORBIDS = "BUDGET_FORBIDS"
    TOO_SHORT_TO_TARGET = "TOO_SHORT_TO_TARGET"


# Phrases that point at material the student supplied or the course owns.
_DOCUMENT_REFERENCE = re.compile(
    r"\b(this (document|pdf|file|paper|handout|worksheet|notes?)"
    r"|the (document|pdf|file|attachment|handout|worksheet)"
    r"|my (notes?|handout|worksheet|textbook|book)"
    r"|uploaded|attached|the chapter|this chapter)\b",
    re.IGNORECASE,
)

_SOURCE_REQUEST = re.compile(
    r"\b(according to|as stated in|cite|citation|reference|which page"
    r"|where does it say|quote|source for|per the (text|book|notes))\b",
    re.IGNORECASE,
)

_COURSE_MATERIAL = re.compile(
    r"\b(syllabus|curriculum|course|module|unit \d|chapter \d|our (class|course)"
    r"|the textbook|prescribed|exam board|board exam)\b",
    re.IGNORECASE,
)

_TUTOR_SPECIFIC = re.compile(
    r"\b(my tutor|sir said|ma'?am said|teacher said|in class|the lesson"
    r"|as taught|your method|the way we did)\b",
    re.IGNORECASE,
)

# Self-contained arithmetic/algebra: the question carries all it needs.
_PURE_COMPUTATION = re.compile(
    r"^\s*(solve|calculate|compute|evaluate|simplify|factorise|factorize|expand"
    r"|differentiate|integrate)\b",
    re.IGNORECASE,
)

_HAS_EXPRESSION = re.compile(r"\d\s*[\+\-\*/\^=]\s*\d|[a-z]\s*[\+\-\^]\s*\d|\bx\s*=")

MIN_QUERY_CHARS = 12
"""Below this a query cannot meaningfully target a corpus - "why?" retrieves
noise at full cost."""


@dataclass(frozen=True, slots=True)
class RagContext:
    text: str
    capability: CapabilityId
    has_corpus: bool
    """False when the student has no indexed sources - retrieval would scan
    nothing and still cost an embedding call."""

    has_active_document: bool = False
    """A document is already the subject of this conversation."""

    is_follow_up: bool = False
    high_stakes: bool = False
    """Test/quiz generation grounded in syllabus material."""

    budget_permits: bool = True


@dataclass(frozen=True, slots=True)
class RagVerdict:
    decision: RagDecision
    reason: RagReason
    top_k: int = 0

    @property
    def should_retrieve(self) -> bool:
        return self.decision is RagDecision.RETRIEVE


def decide(ctx: RagContext) -> RagVerdict:
    """Deterministic. Cheap refusals first, so the common case exits early."""
    # 1. No corpus means retrieval scans nothing but still costs an embedding.
    if not ctx.has_corpus:
        return RagVerdict(RagDecision.SKIP, RagReason.NO_CORPUS_AVAILABLE)

    # 2. Budget refusal outranks relevance: an embedding call is a paid call.
    if not ctx.budget_permits:
        return RagVerdict(RagDecision.SKIP, RagReason.BUDGET_FORBIDS)

    text = ctx.text.strip()
    if len(text) < MIN_QUERY_CHARS:
        return RagVerdict(RagDecision.SKIP, RagReason.TOO_SHORT_TO_TARGET)

    # 3. Strong positive signals, checked before the negative ones: "cite the
    #    page where this integral is derived" is a source request even though it
    #    opens with a computation verb.
    if _SOURCE_REQUEST.search(text):
        return RagVerdict(RagDecision.RETRIEVE, RagReason.ASKS_FOR_SOURCE, top_k=5)
    if _DOCUMENT_REFERENCE.search(text):
        return RagVerdict(RagDecision.RETRIEVE, RagReason.REFERS_TO_UPLOAD, top_k=6)
    if _TUTOR_SPECIFIC.search(text):
        return RagVerdict(RagDecision.RETRIEVE, RagReason.TUTOR_SPECIFIC, top_k=4)
    if _COURSE_MATERIAL.search(text):
        return RagVerdict(RagDecision.RETRIEVE, RagReason.COURSE_OR_SYLLABUS, top_k=5)
    if ctx.high_stakes:
        # Test generation must be grounded in the syllabus, not invented.
        return RagVerdict(RagDecision.RETRIEVE, RagReason.TEST_GENERATION, top_k=8)

    # 4. A follow-up already has its context in the conversation window.
    #    Re-retrieving on "why step 2?" pays again for what is already loaded.
    if ctx.is_follow_up:
        return RagVerdict(RagDecision.SKIP, RagReason.FOLLOW_UP_USES_EXISTING_CONTEXT)

    # 5. An active document makes ambient questions document-scoped.
    if ctx.has_active_document:
        return RagVerdict(RagDecision.RETRIEVE, RagReason.ACTIVE_DOCUMENT_CONTEXT, top_k=4)

    # 6. Self-contained computation. The whole point of this policy.
    if _PURE_COMPUTATION.match(text) or _HAS_EXPRESSION.search(text):
        return RagVerdict(RagDecision.SKIP, RagReason.SELF_CONTAINED_COMPUTATION)

    return RagVerdict(RagDecision.SKIP, RagReason.GENERAL_KNOWLEDGE)


def dynamic_top_k(base_k: int, *, available_tokens: int, avg_chunk_tokens: int = 180) -> int:
    """Fit k to the context budget rather than a fixed constant.

    Retrieving eight chunks and then discarding six to fit the budget wastes the
    ranking work and the tokens spent deciding.
    """
    if available_tokens <= 0 or avg_chunk_tokens <= 0:
        return 0
    affordable = available_tokens // avg_chunk_tokens
    return max(0, min(base_k, affordable))
