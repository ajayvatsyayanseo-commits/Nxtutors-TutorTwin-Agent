"""Spaced repetition and the progress engine.

**Scheduling is arithmetic, and arithmetic does not need a model.** Asking an LLM
when a card is next due would cost money per review, return a different answer
each time, and be worse than the fifty-year-old algorithm it was imitating. This
is SM-2, with the parameters written down.

**Progress refuses to invent precision.** A student with three attempts has no
mastery level. `accuracy` returns `None` below the evidence threshold rather than
a number, because a number would be believed - by the student, and by their
tutor.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from tutortwin.domain.learning import (
    MIN_ATTEMPTS_FOR_SIGNAL,
    CardSchedule,
    MasterySignal,
    ReviewGrade,
    TopicProgress,
    WeakTopic,
)
from tutortwin.observability.logging import get_logger

logger = get_logger(__name__)

# --- SM-2 parameters, stated rather than buried --------------------------------

MIN_EASE = 1.3
"""SM-2's floor. Below this, intervals collapse and the card is shown forever."""

DEFAULT_EASE = 2.5
FIRST_INTERVAL_DAYS = 1
SECOND_INTERVAL_DAYS = 6
MAX_INTERVAL_DAYS = 365
"""A year is long enough for school material; beyond it the syllabus has moved on
and the card should be re-learned rather than trusted."""

# Quality scores in SM-2's 0-5 scale, mapped from what a student can honestly
# report. AGAIN is a lapse; the rest are passes of increasing ease.
_QUALITY: dict[ReviewGrade, int] = {
    ReviewGrade.AGAIN: 2,
    ReviewGrade.HARD: 3,
    ReviewGrade.GOOD: 4,
    ReviewGrade.EASY: 5,
}


def schedule_review(current: CardSchedule, grade: ReviewGrade, *, today: date) -> CardSchedule:
    """Next due date for a card. Pure, deterministic, no model call.

    Same inputs always give the same date - which is what lets the tests assert
    exact intervals rather than ranges.
    """
    quality = _QUALITY[grade]

    if quality < 3:
        # A lapse restarts the ladder but keeps a reduced ease, so a card that
        # keeps failing surfaces more often than one that failed once.
        ease = max(MIN_EASE, current.ease_factor - 0.20)
        return CardSchedule(
            repetitions=0,
            interval_days=FIRST_INTERVAL_DAYS,
            ease_factor=round(ease, 2),
            due_on=today + timedelta(days=FIRST_INTERVAL_DAYS),
            lapses=current.lapses + 1,
        )

    # SM-2's ease update. Rewards EASY, penalises HARD, leaves GOOD roughly flat.
    ease = current.ease_factor + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    ease = max(MIN_EASE, min(3.0, ease))

    repetitions = current.repetitions + 1
    if repetitions == 1:
        interval = FIRST_INTERVAL_DAYS
    elif repetitions == 2:
        interval = SECOND_INTERVAL_DAYS
    else:
        interval = min(MAX_INTERVAL_DAYS, round(current.interval_days * ease))

    return CardSchedule(
        repetitions=repetitions,
        interval_days=interval,
        ease_factor=round(ease, 2),
        due_on=today + timedelta(days=interval),
        lapses=current.lapses,
    )


def is_due(schedule: CardSchedule, *, today: date) -> bool:
    """A card with no due date has never been reviewed, so it is due."""
    return schedule.due_on is None or schedule.due_on <= today


def select_due_cards(
    schedules: dict[str, CardSchedule], *, today: date, limit: int = 20
) -> list[str]:
    """Cards to review now, most overdue first.

    Bounded: an unbounded review queue after a long absence is demoralising and
    nobody completes it.
    """
    due = [(key, s) for key, s in schedules.items() if is_due(s, today=today)]
    due.sort(
        key=lambda pair: (
            pair[1].due_on or date.min,
            -pair[1].lapses,  # cards that keep failing come first
        )
    )
    return [key for key, _ in due[:limit]]


# --- progress -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProgressSnapshot:
    """Observed counters only. Nothing here is inferred."""

    topics: tuple[TopicProgress, ...]

    @property
    def total_attempts(self) -> int:
        return sum(t.attempts for t in self.topics)

    @property
    def has_enough_evidence(self) -> bool:
        return any(t.attempts >= MIN_ATTEMPTS_FOR_SIGNAL for t in self.topics)


def weak_topics(snapshot: ProgressSnapshot, *, limit: int = 3) -> tuple[WeakTopic, ...]:
    """Topics worth practising, with the evidence attached.

    Topics below the evidence threshold are **excluded entirely** rather than
    reported as weak. Three wrong answers is not a weakness; it is three wrong
    answers, and telling a student otherwise is both inaccurate and discouraging.
    """
    candidates: list[WeakTopic] = []
    for topic in snapshot.topics:
        signal = topic.signal
        if signal in {MasterySignal.INSUFFICIENT_EVIDENCE, MasterySignal.SECURE}:
            continue
        candidates.append(
            WeakTopic(
                topic=topic.topic,
                signal=signal,
                attempts=topic.attempts,
                correct=topic.correct,
                evidence=(
                    f"{topic.correct} of {topic.attempts} correct"
                    + (f", {topic.hints_used} with hints" if topic.hints_used else "")
                ),
            )
        )

    # Struggling before developing; within a band, more evidence first.
    order = {MasterySignal.STRUGGLING: 0, MasterySignal.DEVELOPING: 1}
    candidates.sort(key=lambda w: (order.get(w.signal, 2), -w.attempts))
    return tuple(candidates[:limit])


def describe_progress(snapshot: ProgressSnapshot) -> str:
    """A plain-language summary that never overstates what is known."""
    if not snapshot.topics:
        return "No practice recorded yet."

    ready = [t for t in snapshot.topics if t.signal is not MasterySignal.INSUFFICIENT_EVIDENCE]
    if not ready:
        return (
            f"{snapshot.total_attempts} question(s) attempted so far - not yet "
            f"enough to say anything reliable about strengths or weaknesses."
        )

    secure = [t.topic for t in ready if t.signal is MasterySignal.SECURE]
    weak = [t.topic for t in ready if t.signal is MasterySignal.STRUGGLING]

    parts: list[str] = []
    if secure:
        parts.append(f"Looking secure on {', '.join(secure)}.")
    if weak:
        parts.append(f"Worth more practice on {', '.join(weak)}.")
    if not parts:
        parts.append("Progressing steadily; nothing stands out either way yet.")
    return " ".join(parts)
