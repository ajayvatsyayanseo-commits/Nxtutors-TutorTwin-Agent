"""Knowledge, retrieval and memory domain types.

The visibility model is the security core of RAG. Its rule is absolute:

    Ownership predicates are applied IN THE DATABASE, before ranking.

Fetching rows and filtering them in Python would mean private content briefly
existed in a process that was not entitled to it, and one missed filter would be
a cross-student leak. The scope is therefore compiled into the SQL WHERE clause,
never applied afterwards.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Visibility(StrEnum):
    """Who may retrieve a chunk. Ordered loosest to tightest."""

    GLOBAL_CURATED = "GLOBAL_CURATED"
    """Vetted reference material. Readable by everyone."""

    TUTOR = "TUTOR"
    """A tutor's own material, readable by their assigned students."""

    COURSE = "COURSE"
    """Course material, readable by students enrolled in that course."""

    STUDENT_PRIVATE = "STUDENT_PRIVATE"
    """A student's own uploads. Never readable by anyone else, ever."""

    CONVERSATION = "CONVERSATION"
    """Scoped to one conversation - tighter than STUDENT_PRIVATE."""


class SourceKind(StrEnum):
    PDF_EXTRACTION = "PDF_EXTRACTION"
    NOTE = "NOTE"
    TUTOR_MATERIAL = "TUTOR_MATERIAL"
    SYLLABUS = "SYLLABUS"
    GENERATED_NOTES = "GENERATED_NOTES"


class MemoryKind(StrEnum):
    """Only durable, useful facts. Not a diary of everything a student said."""

    PREFERENCE = "PREFERENCE"
    """How the student learns best - visual examples, worked steps, bilingual."""

    MISCONCEPTION = "MISCONCEPTION"
    """A repeated, specific error pattern worth pre-empting."""

    CURRENT_FOCUS = "CURRENT_FOCUS"
    """What they are studying now. Expires - syllabi move on."""

    CONSTRAINT = "CONSTRAINT"
    """A hard requirement, e.g. an exam date or a required language."""


class MemoryConfidence(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RetrievalScope(Frozen):
    """Exactly who is asking. Compiled into SQL predicates, never post-filtered.

    Every field narrows what may be seen. A scope with no tutor cannot see TUTOR
    material at all - absence is denial, not a wildcard.
    """

    subject_id: UUID
    tutor_id: UUID | None = None
    course_ids: tuple[UUID, ...] = ()
    conversation_id: UUID | None = None
    include_global: bool = True

    @property
    def visible_kinds(self) -> tuple[Visibility, ...]:
        """The visibility classes this scope could possibly read.

        Used for logging and assertions. The SQL predicate is built from the
        same facts, so the two cannot disagree.
        """
        kinds: list[Visibility] = [Visibility.STUDENT_PRIVATE]
        if self.include_global:
            kinds.append(Visibility.GLOBAL_CURATED)
        if self.tutor_id is not None:
            kinds.append(Visibility.TUTOR)
        if self.course_ids:
            kinds.append(Visibility.COURSE)
        if self.conversation_id is not None:
            kinds.append(Visibility.CONVERSATION)
        return tuple(kinds)


class RetrievalEvidence(Frozen):
    """One retrieved chunk, with everything needed to cite and audit it."""

    chunk_id: UUID
    source_id: UUID
    source_title: str
    visibility: Visibility
    score: float
    snippet: str
    page_number: int | None = None
    section: str | None = None

    @property
    def citation(self) -> str:
        if self.page_number is not None:
            return f"{self.source_title}, page {self.page_number}"
        if self.section:
            return f"{self.source_title} - {self.section}"
        return self.source_title


class RetrievalResult(Frozen):
    """Retrieval outcome plus its cost evidence."""

    evidence: tuple[RetrievalEvidence, ...] = ()
    query_ms: int = 0
    embedding_calls: int = 0
    candidates_scanned: int = 0
    skipped_reason: str | None = None
    """Set when retrieval was deliberately not performed - the common case."""

    @property
    def performed(self) -> bool:
        return self.skipped_reason is None

    @property
    def total_snippet_chars(self) -> int:
        return sum(len(e.snippet) for e in self.evidence)


class Chunk(Frozen):
    """A unit of indexed text with its provenance."""

    text: str
    ordinal: int = Field(ge=0)
    page_number: int | None = None
    section: str | None = None
    token_estimate: int = 0


class StudentMemory(Frozen):
    """A durable fact worth carrying between conversations."""

    id: UUID
    subject_id: UUID
    kind: MemoryKind
    statement: str
    confidence: MemoryConfidence
    evidence: str
    """What was observed that justified this. Never the raw message."""

    observed_count: int = 1
    expires_at: datetime | None = None
    superseded_by: UUID | None = None

    @property
    def is_active(self) -> bool:
        return self.superseded_by is None


class MemoryCandidate(Frozen):
    """A proposed memory, before it is judged worth keeping."""

    kind: MemoryKind
    statement: str
    evidence: str
    confidence: MemoryConfidence
    ttl_days: int | None = None
    """None means durable. Current focus expires; a misconception does not."""

    derived_by: str = "deterministic"
    """`deterministic` or `model`. Records whether this cost money."""
