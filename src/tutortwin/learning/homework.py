"""Homework task state and the code-execution boundary.

**Task state exists so follow-ups resolve.** "I don't understand step 3" is only
answerable if the steps that were shown are still on record. The state is
persisted per conversation, so the third turn knows what the first one said.

**The sandbox is DISABLED by default and refuses rather than degrades.** Running
student code inside the API process would give arbitrary code the same
credentials as the database connection. A gateway that quietly "tries its best"
is worse than one that says no, so the default implementation always refuses and
says why.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from tutortwin.domain.learning import HomeworkAction, SolutionStep, TaskStage
from tutortwin.observability.logging import get_logger

logger = get_logger(__name__)


# --- homework task state ------------------------------------------------------


@dataclass(slots=True)
class HomeworkTask:
    """What is being worked on, and how far it has got."""

    problem_text: str
    stage: TaskStage = TaskStage.POSED
    steps: tuple[SolutionStep, ...] = ()
    hints_given: int = 0
    student_answer: str | None = None
    topic: str = ""

    def step(self, number: int) -> SolutionStep | None:
        """Resolve "explain step 3" against the steps actually shown."""
        return next((s for s in self.steps if s.number == number), None)

    @property
    def has_solution(self) -> bool:
        return bool(self.steps)


_ACTION_STAGE: dict[HomeworkAction, TaskStage] = {
    HomeworkAction.HINT: TaskStage.HINTED,
    HomeworkAction.FULL_SOLUTION: TaskStage.SOLVED,
    HomeworkAction.CHECK_MY_ANSWER: TaskStage.CHECKED,
    HomeworkAction.ANALYSE_MISTAKE: TaskStage.CHECKED,
}


def apply_action(task: HomeworkTask, action: HomeworkAction) -> HomeworkTask:
    """Advance the task. Stage never moves backwards.

    A student asking for a hint after seeing the full solution should not reset
    the task to HINTED - the solution is still on screen.
    """
    order = list(TaskStage)
    target = _ACTION_STAGE.get(action, task.stage)
    if order.index(target) > order.index(task.stage):
        task.stage = target
    if action is HomeworkAction.HINT:
        task.hints_given += 1
    return task


def can_explain_step(task: HomeworkTask, number: int) -> bool:
    """A step can only be explained if it was actually shown."""
    return task.step(number) is not None


# --- detecting what the student asked for -------------------------------------

_ACTION_PATTERNS: tuple[tuple[HomeworkAction, tuple[str, ...]], ...] = (
    (HomeworkAction.HINT, ("hint", "nudge", "point me", "get me started")),
    (
        HomeworkAction.FULL_SOLUTION,
        ("full solution", "work it out", "show me how", "solve it"),
    ),
    (HomeworkAction.EXPLAIN_STEP, ("step", "why does", "why did", "how did you get")),
    (
        HomeworkAction.CHECK_MY_ANSWER,
        ("check my", "is my answer", "did i get", "am i right"),
    ),
    (
        HomeworkAction.ANALYSE_MISTAKE,
        ("what did i do wrong", "where did i go wrong", "my mistake"),
    ),
    (HomeworkAction.SIMPLER, ("simpler", "simply", "easier way", "eli5")),
    (HomeworkAction.DIAGRAM, ("diagram", "draw", "graph", "sketch", "plot")),
    (HomeworkAction.SIMILAR_PROBLEM, ("similar", "another one", "practice", "twin")),
    (HomeworkAction.HARDER, ("harder", "more difficult", "challenge")),
    (HomeworkAction.EASIER, ("easier", "less difficult", "simpler problem")),
)


def detect_action(text: str) -> HomeworkAction | None:
    """Deterministic intent detection within an active task.

    Ordered so specific phrases win: "what did I do wrong" is mistake analysis,
    not a hint request, even though both mention the student's own work.
    """
    lowered = text.lower()
    for action, phrases in _ACTION_PATTERNS:
        if any(phrase in lowered for phrase in phrases):
            return action
    return None


_STEP_NUMBER = re.compile(r"\bstep\s*(\d{1,2})\b", re.IGNORECASE)


def detect_step_number(text: str) -> int | None:
    match = _STEP_NUMBER.search(text)
    return int(match.group(1)) if match else None


# --- code execution boundary --------------------------------------------------


class SandboxStatus(StrEnum):
    DISABLED = "DISABLED"
    EXECUTED = "EXECUTED"
    REFUSED = "REFUSED"


@dataclass(frozen=True, slots=True)
class SandboxResult:
    status: SandboxStatus
    stdout: str = ""
    stderr: str = ""
    reason: str = ""


class SandboxGateway(Protocol):
    """Isolated execution of student code. Never in the API process."""

    @property
    def enabled(self) -> bool: ...

    async def run(self, language: str, source: str) -> SandboxResult: ...


@dataclass(slots=True)
class DisabledSandbox:
    """The default, and the only implementation in this phase.

    Refuses every request and says so. This is deliberate: the API process holds
    database credentials and provider keys, so executing student code there would
    hand those to arbitrary input. Phase 07 may add an ephemeral Cloud Run job
    with no secrets, no network and a hard timeout - until then, refusal is the
    correct behaviour, not a limitation to work around.
    """

    attempts: int = 0

    @property
    def enabled(self) -> bool:
        return False

    async def run(self, language: str, source: str) -> SandboxResult:
        self.attempts += 1
        logger.info("sandbox_refused", language=language, source_bytes=len(source))
        return SandboxResult(
            status=SandboxStatus.DISABLED,
            reason=(
                "Code execution is not enabled. I can read your code and explain "
                "what it does, trace it by hand, or reason about test cases."
            ),
        )


# --- safe deterministic calculation, which is not code execution --------------


@dataclass(slots=True)
class ArithmeticCalculator:
    """Evaluate a bounded arithmetic expression - not a code sandbox.

    Kept separate from `SandboxGateway` on purpose: "compute 17 * 23" is a
    parsed expression evaluated by SymPy under the same two-layer filter as the
    verifier, and conflating it with running arbitrary programs is how a
    calculator turns into an execution path.
    """

    evaluations: int = 0

    def evaluate(self, expression: str) -> str | None:
        from tutortwin.learning.verification import UnsafeExpression, safe_parse

        try:
            parsed = safe_parse(expression)
        except UnsafeExpression as exc:
            logger.info("calculator_rejected", reason=exc.reason)
            return None
        if parsed.free_symbols:
            return None  # not a closed-form number; nothing to compute
        self.evaluations += 1
        return str(parsed.evalf(12))


# --- code review without execution --------------------------------------------

CODE_REVIEW_GUIDANCE = (
    "Read the code and explain it. You cannot run it and must not claim to have "
    "run it, produced output, or observed behaviour. Point to the specific line "
    "or construct at fault and explain the underlying concept so the student can "
    "fix similar bugs themselves."
)


@dataclass(slots=True)
class CodeContext:
    """Code under discussion, with its language and provenance."""

    source: str
    language: str = "unknown"
    from_image: bool = False
    """True when the code arrived as a screenshot, so line numbers may be
    approximate and the transcription itself could be wrong."""

    steps: list[str] = field(default_factory=list)

    @property
    def line_count(self) -> int:
        return len(self.source.splitlines())
