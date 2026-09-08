"""Student memory: what is worth remembering, and what is not.

The failure mode this module exists to avoid is a memory that records
everything. A store full of "mentioned a dog on Tuesday" is worse than no store:
it costs tokens on every request and buries the two facts that actually help.

So candidates must clear a bar. They are extracted **deterministically** from
observable behaviour - an explicit preference, a repeated error - and a model is
consulted only where rules genuinely cannot see the pattern.

Data minimisation is a rule, not a nicety: these are often minors. Names,
contact details, locations, health and family information are never stored, even
when volunteered.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tutortwin.db.knowledge_models import StudentMemoryRow, TopicStat
from tutortwin.domain.knowledge import (
    MemoryCandidate,
    MemoryConfidence,
    MemoryKind,
    StudentMemory,
)
from tutortwin.observability.logging import get_logger

logger = get_logger(__name__)

CURRENT_FOCUS_TTL_DAYS = 45
"""A syllabus moves on. A stale "working on trigonometry" actively misleads."""

MAX_ACTIVE_MEMORIES = 40
"""A cap, not a target. Beyond this the oldest low-confidence entries retire."""

MISCONCEPTION_THRESHOLD = 2
"""One mistake is an accident. Two of the same kind is a pattern worth naming."""

# --- data minimisation --------------------------------------------------------
# Patterns that must never enter memory, however they arrived.
_FORBIDDEN = (
    re.compile(r"\b\d{10,}\b"),  # phone / ID numbers
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),  # email
    re.compile(r"\b(?:my |i )?(?:address|postcode|zip|pin ?code)\b", re.I),
    re.compile(r"\b(?:mother|father|mum|mom|dad|parent|sister|brother)\b", re.I),
    re.compile(r"\b(?:diagnos|medication|therapy|adhd|dyslexi|anxiet|depress)\w*\b", re.I),
    re.compile(r"\b(?:school name|i live|i study at|my school is)\b", re.I),
)


def is_storable(statement: str) -> bool:
    """Reject anything carrying personal detail. Fails closed."""
    return not any(pattern.search(statement) for pattern in _FORBIDDEN)


# --- deterministic candidate extraction ---------------------------------------

_PREFERENCE_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b(diagram|picture|drawing|visual|graph|chart)s?\b.*\b(help|easier|better|prefer)\b",
            re.I,
        ),
        "Prefers visual explanations such as diagrams and graphs.",
    ),
    (
        re.compile(r"\b(prefer|like|works better)\b.*\b(step by step|steps)\b", re.I),
        "Prefers step-by-step working.",
    ),
    (
        re.compile(
            r"\b(explain|say|tell)\b.*\bin (hindi|urdu|tamil|telugu|bengali|marathi)\b", re.I
        ),
        "Prefers bilingual explanation alongside English.",
    ),
    (
        re.compile(r"\b(don'?t|do not) (give|show) (me )?the (answer|solution)\b", re.I),
        "Prefers hints before full solutions.",
    ),
    (
        re.compile(r"\b(too (long|wordy)|keep it short|be brief|shorter)\b", re.I),
        "Prefers concise answers.",
    ),
)

_CONSTRAINT_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\b(exam|test|board)\b.*\b(tomorrow|next week|on \w+day)\b", re.I),
        "Has an exam imminent; prioritise revision over depth.",
    ),
)

_TOPIC_RULE = re.compile(
    r"\b(?:working on|studying|revising|preparing for)\s+([a-z0-9 ]{3,40})", re.I
)


def extract_candidates(text: str) -> tuple[MemoryCandidate, ...]:
    """Deterministic extraction. Costs nothing, runs on every eligible turn.

    Only explicit, self-reported signals are matched. Inferring a preference
    from tone would be guesswork stored as fact.
    """
    if not text or not text.strip():
        return ()

    candidates: list[MemoryCandidate] = []

    for pattern, statement in _PREFERENCE_RULES:
        if pattern.search(text):
            candidates.append(
                MemoryCandidate(
                    kind=MemoryKind.PREFERENCE,
                    statement=statement,
                    evidence="student stated this preference explicitly",
                    confidence=MemoryConfidence.HIGH,
                )
            )

    for pattern, statement in _CONSTRAINT_RULES:
        if pattern.search(text):
            candidates.append(
                MemoryCandidate(
                    kind=MemoryKind.CONSTRAINT,
                    statement=statement,
                    evidence="student stated a deadline",
                    confidence=MemoryConfidence.MEDIUM,
                    ttl_days=30,
                )
            )

    match = _TOPIC_RULE.search(text)
    if match:
        topic = match.group(1).strip().rstrip(".,!?")
        if 3 <= len(topic) <= 40:
            candidates.append(
                MemoryCandidate(
                    kind=MemoryKind.CURRENT_FOCUS,
                    statement=f"Currently working on {topic}.",
                    evidence="student said what they are studying",
                    confidence=MemoryConfidence.MEDIUM,
                    ttl_days=CURRENT_FOCUS_TTL_DAYS,
                )
            )

    return tuple(c for c in candidates if is_storable(c.statement))


def misconception_candidate(
    topic: str, error_pattern: str, observed: int
) -> MemoryCandidate | None:
    """A misconception is earned by repetition, not by a single wrong answer."""
    if observed < MISCONCEPTION_THRESHOLD:
        return None
    return MemoryCandidate(
        kind=MemoryKind.MISCONCEPTION,
        statement=f"Repeatedly {error_pattern} in {topic}.",
        evidence=f"observed {observed} times",
        confidence=(MemoryConfidence.HIGH if observed >= 3 else MemoryConfidence.MEDIUM),
    )


def statement_key(statement: str) -> str:
    return hashlib.sha256(
        re.sub(r"\s+", " ", statement).strip().lower().encode("utf-8")
    ).hexdigest()


@dataclass(slots=True)
class MemoryWriteResult:
    created: int = 0
    reinforced: int = 0
    rejected: int = 0


async def store_candidates(
    session: AsyncSession,
    *,
    subject_id: UUID,
    candidates: tuple[MemoryCandidate, ...],
    now: datetime | None = None,
) -> MemoryWriteResult:
    """Persist candidates, reinforcing rather than duplicating.

    Re-observing a fact bumps `observed_count` and can raise confidence. That is
    how a MEDIUM guess becomes a HIGH fact through repetition rather than
    through a model asserting it more loudly.
    """
    stamp = now or datetime.now(UTC)
    result = MemoryWriteResult()

    for candidate in candidates:
        if not is_storable(candidate.statement):
            result.rejected += 1
            logger.info("memory_rejected_personal_data", kind=candidate.kind.value)
            continue

        key = statement_key(candidate.statement)
        expires = (
            stamp + timedelta(days=candidate.ttl_days) if candidate.ttl_days is not None else None
        )

        inserted = await session.execute(
            pg_insert(StudentMemoryRow)
            .values(
                subject_id=subject_id,
                kind=str(candidate.kind),
                statement=candidate.statement,
                statement_sha256=key,
                confidence=str(candidate.confidence),
                evidence=candidate.evidence,
                derived_by=candidate.derived_by,
                expires_at=expires,
            )
            .on_conflict_do_update(
                constraint="uq_memory_statement",
                set_={
                    "observed_count": StudentMemoryRow.observed_count + 1,
                    "expires_at": expires,
                    # Repetition raises confidence; it never lowers it.
                    "confidence": str(MemoryConfidence.HIGH)
                    if candidate.confidence is MemoryConfidence.HIGH
                    else StudentMemoryRow.confidence,
                },
            )
            .returning(StudentMemoryRow.observed_count)
        )
        count = inserted.scalar_one()
        if count > 1:
            result.reinforced += 1
        else:
            result.created += 1

    return result


async def load_relevant_memories(
    session: AsyncSession,
    *,
    subject_id: UUID,
    now: datetime | None = None,
    limit: int = 6,
) -> tuple[StudentMemory, ...]:
    """The small set worth spending context tokens on.

    Expired and superseded entries are excluded in SQL. Ordering puts durable,
    high-confidence facts first, because those are the ones that survive a
    budget squeeze.
    """
    stamp = now or datetime.now(UTC)
    rows = (
        await session.execute(
            select(StudentMemoryRow)
            .where(
                StudentMemoryRow.subject_id == subject_id,
                StudentMemoryRow.superseded_by.is_(None),
                (StudentMemoryRow.expires_at.is_(None)) | (StudentMemoryRow.expires_at > stamp),
            )
            .order_by(
                StudentMemoryRow.confidence.desc(),
                StudentMemoryRow.observed_count.desc(),
                StudentMemoryRow.updated_at.desc(),
            )
            .limit(limit)
        )
    ).scalars()

    return tuple(
        StudentMemory(
            id=row.id,
            subject_id=row.subject_id,
            kind=MemoryKind(row.kind),
            statement=row.statement,
            confidence=MemoryConfidence(row.confidence),
            evidence=row.evidence,
            observed_count=row.observed_count,
            expires_at=row.expires_at,
            superseded_by=row.superseded_by,
        )
        for row in rows
    )


async def supersede(session: AsyncSession, *, old_id: UUID, new_id: UUID) -> None:
    """Replace a memory rather than deleting it - the history stays auditable."""
    row = (
        await session.execute(select(StudentMemoryRow).where(StudentMemoryRow.id == old_id))
    ).scalar_one_or_none()
    if row is not None:
        row.superseded_by = new_id


# --- observed statistics ------------------------------------------------------


async def record_attempt(
    session: AsyncSession,
    *,
    subject_id: UUID,
    topic: str,
    correct: bool,
    used_hint: bool = False,
) -> None:
    """Observed facts, updated deterministically.

    Kept strictly apart from inferred memory: a count of attempts is a fact, and
    "struggles with trigonometry" is an interpretation. Conflating them is how
    products end up presenting a guess as a measurement.
    """
    await session.execute(
        pg_insert(TopicStat)
        .values(
            subject_id=subject_id,
            topic=topic.lower().strip()[:128],
            attempts=1,
            correct=1 if correct else 0,
            hints_used=1 if used_hint else 0,
        )
        .on_conflict_do_update(
            constraint="uq_topic_stat",
            set_={
                "attempts": TopicStat.attempts + 1,
                "correct": TopicStat.correct + (1 if correct else 0),
                "hints_used": TopicStat.hints_used + (1 if used_hint else 0),
                "last_seen_at": datetime.now(UTC),
            },
        )
    )


PROFILE_REFRESH_ATTEMPTS = 10
"""Regenerate the inferred profile every N attempts, not every message."""


def should_refresh_profile(total_attempts: int) -> bool:
    return total_attempts > 0 and total_attempts % PROFILE_REFRESH_ATTEMPTS == 0
