"""Conversation context assembly under a token budget.

The rule this module exists to enforce: never send the whole chat. A lifetime
history grows without bound, and paying to re-read it on every turn is the single
easiest way to make a tutoring product uneconomic.

What gets sent instead: a rolling summary of older turns, a recent-turn window,
and the current message. The summary is produced by *deterministic truncation*,
not by a model - see `summarize_turns`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from tutortwin.domain.provider import ModelMessage

# Recent turns kept verbatim. Four exchanges is enough for "why step 2?" to
# resolve while staying small; older context arrives via the summary.
RECENT_TURN_LIMIT = 8

MAX_TURN_CHARS = 1_500
"""A single pasted essay must not crowd out the rest of the window."""


class TokenEstimator(Protocol):
    def estimate(self, text: str) -> int: ...


class HeuristicTokenEstimator:
    """~4 characters per token.

    Deliberately not tiktoken: that would add a dependency, be wrong for
    Anthropic anyway, and this value only needs to be good enough to make budget
    decisions. It is intentionally slightly pessimistic so we under-fill rather
    than overflow a real context window.
    """

    CHARS_PER_TOKEN = 4

    def estimate(self, text: str) -> int:
        if not text:
            return 0
        return max(1, (len(text) + self.CHARS_PER_TOKEN - 1) // self.CHARS_PER_TOKEN)


@dataclass(frozen=True, slots=True)
class Turn:
    role: str  # STUDENT | ASSISTANT
    text: str


@dataclass(frozen=True, slots=True)
class AssembledContext:
    messages: tuple[ModelMessage, ...]
    estimated_tokens: int
    turns_included: int
    turns_summarized: int


def _truncate(text: str, limit: int = MAX_TURN_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + " [...truncated]"


def summarize_turns(turns: list[Turn]) -> str:
    """Compress older turns WITHOUT a model call.

    A model-generated summary would mean every long conversation quietly costs an
    extra paid call per turn - a background cost that scales with engagement,
    which is exactly the wrong shape. Topic keywords are cheap and sufficient for
    "what were we talking about".
    """
    if not turns:
        return ""
    student_texts = [t.text for t in turns if t.role == "STUDENT"]
    if not student_texts:
        return ""
    # Keep the first (the original topic) and the most recent, which is what the
    # model actually needs to stay oriented.
    head = _truncate(student_texts[0], 200)
    if len(student_texts) == 1:
        return f"Earlier in this conversation the student asked: {head}"
    tail = _truncate(student_texts[-1], 200)
    return (
        f"Earlier in this conversation the student asked about: {head} "
        f"Most recently before this: {tail} "
        f"({len(turns)} earlier messages omitted.)"
    )


def assemble(
    *,
    system_prompt: str,
    history: list[Turn],
    current_message: str,
    estimator: TokenEstimator,
    max_context_tokens: int,
) -> AssembledContext:
    """Build the message list under a hard token ceiling.

    The current message is never dropped - if it alone does not fit, the budget
    policy's size check should already have rejected the request.
    """
    recent = history[-RECENT_TURN_LIMIT:] if history else []
    older = history[:-RECENT_TURN_LIMIT] if len(history) > RECENT_TURN_LIMIT else []

    messages: list[ModelMessage] = []
    summary = summarize_turns(older)
    if summary:
        messages.append(ModelMessage(role="user", content=summary))

    for turn in recent:
        messages.append(
            ModelMessage(
                role="assistant" if turn.role == "ASSISTANT" else "user",
                content=_truncate(turn.text),
            )
        )

    messages.append(ModelMessage(role="user", content=_truncate(current_message, 8_000)))

    # Trim oldest-first until it fits. The system prompt and the current message
    # are fixed costs and are never trimmed.
    fixed = estimator.estimate(system_prompt) + estimator.estimate(messages[-1].content)
    budget = max_context_tokens - fixed

    while len(messages) > 1:
        used = sum(estimator.estimate(m.content) for m in messages[:-1])
        if used <= budget:
            break
        messages.pop(0)

    total = estimator.estimate(system_prompt) + sum(estimator.estimate(m.content) for m in messages)
    return AssembledContext(
        messages=tuple(messages),
        estimated_tokens=total,
        turns_included=len(messages) - 1,
        turns_summarized=len(older),
    )
