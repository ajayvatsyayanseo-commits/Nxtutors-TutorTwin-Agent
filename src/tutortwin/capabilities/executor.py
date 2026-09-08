"""Capability execution, confidence scoring and the selective verifier.

One executor serves all text capabilities. They differ only in their prompt
block, which `services.prompts` already supplies, so a module per capability
would be eleven files of identical control flow.

**Confidence is computed, not asked for.** Asking a model "how confident are
you?" yields a number that reads calibrated and is not. Instead the band comes
from signals we can observe about the response itself: truncation, refusal,
hedging density, self-contradiction, emptiness. Each signal is recorded on the
result, so a LOW band can be explained rather than merely reported.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from tutortwin.domain.budget import ExecutionBudgetDecision, VerifierMode
from tutortwin.domain.capabilities import (
    STEM_CAPABILITIES,
    CapabilityId,
    CapabilityResult,
    ConfidenceBand,
    NextAction,
    PedagogyMode,
)
from tutortwin.domain.provider import ModelCall, ModelMessage, ModelRequest, StopReason
from tutortwin.observability.logging import get_logger
from tutortwin.providers.gateway import GatewayResult, ModelGateway

logger = get_logger(__name__)

# Hedges are normal in teaching ("this usually means..."). Density matters, not
# presence, so the threshold is deliberately above casual usage.
_HEDGE_PATTERN = re.compile(
    r"\b(i think|i believe|probably|possibly|might be|may be|not sure|"
    r"unsure|i'm not certain|it seems|perhaps|roughly|approximately|"
    r"i cannot be sure|hard to say|difficult to say)\b",
    re.IGNORECASE,
)
HEDGE_DENSITY_THRESHOLD = 3

# Explicit self-contradiction: the model corrects itself mid-answer.
_CONTRADICTION_PATTERN = re.compile(
    r"\b(actually,? (no|wait)|on second thought|i made a mistake|"
    r"correction:|scratch that|that'?s wrong|let me redo)\b",
    re.IGNORECASE,
)

_INABILITY_PATTERN = re.compile(
    r"\b(i (can'?t|cannot) (help|answer|assist)|i don'?t (know|have enough))\b",
    re.IGNORECASE,
)

MIN_USEFUL_ANSWER_CHARS = 40


@dataclass(slots=True)
class ExecutionOutcome:
    """Result plus every provider call it took.

    `calls` drives the usage ledger: one row per entry, so a verifier call and a
    retry are both accounted rather than folded into the primary.
    """

    result: CapabilityResult | None
    calls: list[ModelCall] = field(default_factory=list)


def score_confidence(
    call: ModelCall, capability: CapabilityId
) -> tuple[ConfidenceBand, tuple[str, ...]]:
    """Derive a band from observable signals. No model self-assessment."""
    signals: list[str] = []
    text = call.text or ""

    if call.stop_reason is StopReason.REFUSAL:
        return ConfidenceBand.LOW, ("refusal",)
    if not text.strip():
        return ConfidenceBand.LOW, ("empty_response",)
    if call.stop_reason is StopReason.MAX_TOKENS:
        # A truncated answer's conclusion is missing, whatever its quality.
        signals.append("truncated_output")
    if len(text) < MIN_USEFUL_ANSWER_CHARS:
        signals.append("very_short_answer")
    if _INABILITY_PATTERN.search(text):
        signals.append("stated_inability")
    if _CONTRADICTION_PATTERN.search(text):
        signals.append("self_contradiction")

    hedges = len(_HEDGE_PATTERN.findall(text))
    if hedges >= HEDGE_DENSITY_THRESHOLD:
        signals.append(f"hedging_x{hedges}")

    # Any hard signal drops to LOW; STEM gets no benefit of the doubt because a
    # confidently wrong derivation is worse than an admitted uncertainty.
    hard = {"truncated_output", "self_contradiction", "stated_inability", "empty_response"}
    if any(s in hard for s in signals):
        return ConfidenceBand.LOW, tuple(signals)
    if signals:
        return ConfidenceBand.MEDIUM, tuple(signals)
    if capability in STEM_CAPABILITIES:
        # Clean STEM output is credible but unverified arithmetic is a known
        # weak spot, so it never claims HIGH on signals alone.
        return ConfidenceBand.MEDIUM, ("stem_unverified",)
    return ConfidenceBand.HIGH, ()


def should_verify(
    *,
    decision: ExecutionBudgetDecision,
    confidence: ConfidenceBand,
    capability: CapabilityId,
) -> bool:
    """Selective by construction. A simple, confident answer never verifies."""
    if decision.verifier_mode is VerifierMode.NONE or decision.verifier_alias is None:
        return False
    if decision.verifier_mode is VerifierMode.ALWAYS:
        return True
    # ON_LOW_CONFIDENCE
    return confidence is ConfidenceBand.LOW and capability in STEM_CAPABILITIES


def _next_action(mode: PedagogyMode, confidence: ConfidenceBand) -> NextAction:
    if confidence is ConfidenceBand.LOW:
        return NextAction.ASK_CLARIFICATION
    if mode is PedagogyMode.HINT_FIRST:
        return NextAction.OFFER_NEXT_STEP
    if mode is PedagogyMode.SOCRATIC:
        return NextAction.AWAIT_STUDENT
    if mode is PedagogyMode.EXAM_REVISION:
        return NextAction.OFFER_PRACTICE
    return NextAction.AWAIT_STUDENT


class CapabilityExecutor:
    def __init__(self, gateway: ModelGateway) -> None:
        self._gateway = gateway
        self._blocked: frozenset[str] = frozenset()

    async def run(
        self,
        *,
        decision: ExecutionBudgetDecision,
        system_prompt: str,
        messages: tuple[ModelMessage, ...],
        capability: CapabilityId,
        mode: PedagogyMode,
        cacheable_prefix: str | None = None,
        blocked_providers: frozenset[str] = frozenset(),
    ) -> ExecutionOutcome:
        # Defence in depth: the caller already checked, but an executor that can
        # spend without a permitting decision is one refactor away from a bug.
        if not decision.permits_paid_call or decision.alias is None:
            logger.warning("executor_refused_unpermitted_call", outcome=decision.outcome.value)
            return ExecutionOutcome(result=None, calls=[])

        # Remembered for the verifier call, which is made from a helper that has
        # no access to this frame. A vendor blocked for the answer is blocked for
        # the check of that answer too.
        self._blocked = blocked_providers

        # `cacheable_prefix` is the byte-identical half of the system prompt -
        # safety rules, tutor identity, persona. Both adapters mark it for
        # provider-side caching, so on the second and later turns of a
        # conversation those tokens are billed at the cached rate instead of
        # being re-read in full. Passing None here is what made the caching
        # support in both adapters dead code.
        request = ModelRequest(
            alias=decision.alias,
            messages=messages,
            system=system_prompt,
            cacheable_prefix=cacheable_prefix,
            max_output_tokens=decision.max_output_tokens,
        )
        primary = await self._gateway.invoke(
            request,
            max_attempts=decision.max_attempts,
            fallback_alias=decision.fallback_alias,
            blocked_providers=blocked_providers,
        )
        calls = list(primary.attempts)

        if primary.call is None:
            return ExecutionOutcome(result=None, calls=calls)

        confidence, signals = score_confidence(primary.call, capability)
        answer = primary.call.text
        all_signals = list(signals)

        if should_verify(decision=decision, confidence=confidence, capability=capability):
            verified = await self._verify(
                answer=answer,
                messages=messages,
                decision=decision,
                capability=capability,
            )
            if verified is not None:
                calls.extend(verified.attempts)
                if verified.call is not None:
                    all_signals.append("verifier_ran")
                    # The verifier reviews rather than replaces: a second opinion
                    # that silently overwrote the answer would make the failure
                    # mode invisible.
                    if _verifier_disagrees(verified.call.text):
                        all_signals.append("verifier_disagreed")
                        confidence = ConfidenceBand.LOW
                    else:
                        all_signals.append("verifier_agreed")
                        if confidence is ConfidenceBand.LOW:
                            confidence = ConfidenceBand.MEDIUM

        result = CapabilityResult(
            capability=capability,
            answer_text=answer,
            confidence=confidence,
            signals=tuple(all_signals),
            verification_recommended=confidence is ConfidenceBand.LOW,
            next_action=_next_action(mode, confidence),
            pedagogy_mode=mode,
        )
        return ExecutionOutcome(result=result, calls=calls)

    async def _verify(
        self,
        *,
        answer: str,
        messages: tuple[ModelMessage, ...],
        decision: ExecutionBudgetDecision,
        capability: CapabilityId,
    ) -> GatewayResult | None:
        if decision.verifier_alias is None:
            return None
        question = messages[-1].content if messages else ""
        verify_request = ModelRequest(
            alias=decision.verifier_alias,
            system=(
                "You are checking another tutor's answer for correctness. "
                "Reply with AGREE if the reasoning and result are sound. "
                "Reply with DISAGREE followed by the specific error if not. "
                "Be terse."
            ),
            messages=(
                ModelMessage(
                    role="user",
                    content=f"Question:\n{question}\n\nProposed answer:\n{answer}",
                ),
            ),
            max_output_tokens=256,
        )
        # A verifier failure must never fail the request - the primary answer
        # still stands, just without corroboration.
        return await self._gateway.invoke(
            verify_request, max_attempts=1, blocked_providers=self._blocked
        )


def _verifier_disagrees(text: str) -> bool:
    return text.strip().upper().startswith("DISAGREE") or "DISAGREE" in text[:200].upper()
