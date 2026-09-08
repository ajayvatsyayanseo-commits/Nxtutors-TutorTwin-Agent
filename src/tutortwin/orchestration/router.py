"""Deterministic intent routing.

The cost argument for this module: classifying "solve 2x+5=13" with an LLM costs
money on every single turn forever. Rules cost nothing. So rules run first, and a
model is consulted only when the text is genuinely ambiguous - which, on real
tutoring traffic, is the minority.

Rule representation is a scored table rather than a regex cascade. A cascade
makes precedence implicit in line order and rots as rules accumulate; scoring
makes "why this capability" a number you can print, which is what
`IntentDecision.reason` carries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from tutortwin.domain.capabilities import (
    CapabilityId,
    Difficulty,
    IntentDecision,
    PedagogyMode,
)

# --- Pedagogy override phrases -------------------------------------------------
# A student explicitly asking for a different depth is unambiguous, so it is
# matched deterministically and never sent to a model.
_MODE_PHRASES: tuple[tuple[PedagogyMode, tuple[str, ...]], ...] = (
    (
        PedagogyMode.ANSWER_AND_EXPLAIN,
        ("just the answer", "just give me the answer", "final answer", "answer directly"),
    ),
    (
        PedagogyMode.STEP_BY_STEP,
        ("step by step", "step-by-step", "show the steps", "show your work", "in steps"),
    ),
    (PedagogyMode.HINT_FIRST, ("give me a hint", "just a hint", "hint first", "nudge me")),
    (
        PedagogyMode.SOCRATIC,
        ("ask me questions", "socratic", "quiz me through", "guide me with questions"),
    ),
    (
        PedagogyMode.EXAM_REVISION,
        ("exam revision", "revise for", "revision mode", "for my exam tomorrow"),
    ),
    (
        PedagogyMode.GUIDED,
        (
            "explain simpler",
            "simpler explanation",
            "explain it simply",
            "in simple terms",
            "eli5",
            "like i'm five",
            "easier explanation",
            "simplify that",
        ),
    ),
)

# --- Follow-up markers ---------------------------------------------------------
# "why step 2?" only makes sense against prior turns. Detecting this
# deterministically is what keeps context assembly from re-sending everything.
_FOLLOW_UP_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bwhy\s+(did|is|does|was|do)\b"),
    re.compile(r"\bstep\s*\d+\b"),
    re.compile(r"\b(that|this|it|those)\s+(step|part|line|bit|answer|one)\b"),
    re.compile(r"^(why|how come|and then|what about|but)\b"),
    re.compile(r"\b(previous|last|earlier)\s+(answer|step|message|explanation)\b"),
    re.compile(r"\b(explain|clarify)\s+(that|this|it)\b"),
)


@dataclass(frozen=True, slots=True)
class _Rule:
    capability: CapabilityId
    weight: int
    pattern: re.Pattern[str]


def _rule(capability: CapabilityId, weight: int, pattern: str) -> _Rule:
    return _Rule(capability, weight, re.compile(pattern))


# Weights encode precision, not enthusiasm:
#   4 - the term is essentially exclusive to that subject ("photosynthesis")
#   3 - generic question openers ("what is", "explain"), which fire everywhere
#   2 - the term leans that way but is shared ("force", "solution", "cell")
# Subject markers sit above the openers deliberately: "what is photosynthesis?"
# is a biology question, not a generic definition request.
# The highest total wins; a lone weight-2 hit is treated as too weak to commit.
_RULES: tuple[_Rule, ...] = (
    # Mathematics - symbolic forms are the strongest signal available.
    _rule(CapabilityId.MATH, 3, r"\b(integrate|derivative|differentiate|integral)\b"),
    _rule(CapabilityId.MATH, 3, r"\b(quadratic|logarithm|polynomial|factorise|factorize)\b"),
    _rule(CapabilityId.MATH, 3, r"\b(sine|cosine|tangent|trigonometry|calculus|algebra)\b"),
    _rule(CapabilityId.MATH, 3, r"\b(matrix|matrices|eigenvalue|determinant|vector)\b"),
    _rule(CapabilityId.MATH, 2, r"\b(equation|simplify|solve for|theorem|probability)\b"),
    _rule(CapabilityId.MATH, 2, r"\d\s*[\+\-\*/\^=]\s*\d|[a-z]\s*=\s*\d|\bx\s*[\+\-\^]"),
    _rule(CapabilityId.MATH, 2, r"\b(fraction|percentage|geometry|angle|triangle)\b"),
    # Advanced-math vocabulary is also a strong *subject* signal, not only a
    # difficulty marker. Without these, "prove this Taylor series converges"
    # matches no capability rule and falls through to GENERAL_TUTORING.
    _rule(CapabilityId.MATH, 4, r"\b(taylor series|maclaurin|fourier|laplace|eigenvalue)\b"),
    _rule(CapabilityId.MATH, 4, r"\b(differential equation|partial derivative|multivariable)\b"),
    _rule(CapabilityId.MATH, 3, r"\b(converge|convergence|divergence theorem|stokes)\b"),
    _rule(CapabilityId.MATH, 2, r"\b(prove|proof|theorem|lemma)\b"),
    # Physics
    _rule(CapabilityId.PHYSICS, 4, r"\b(newton|kinematics|momentum|thermodynamic)\b"),
    _rule(CapabilityId.PHYSICS, 4, r"\b(voltage|resistor|circuit|ohm|capacitor|ampere)\b"),
    _rule(CapabilityId.PHYSICS, 4, r"\b(projectile|refraction|wavelength|kinetic energy)\b"),
    _rule(CapabilityId.PHYSICS, 2, r"\b(velocity|acceleration|friction|gravity|inertia)\b"),
    _rule(CapabilityId.PHYSICS, 2, r"\b(force|mass|joule|newtons|watt|magnetic)\b"),
    # Chemistry
    _rule(CapabilityId.CHEMISTRY, 4, r"\b(stoichiometry|molarity|titration|electron config)\b"),
    _rule(CapabilityId.CHEMISTRY, 4, r"\b(covalent|ionic bond|periodic table|isotope)\b"),
    _rule(CapabilityId.CHEMISTRY, 4, r"\b(balance the equation|oxidation|reduction|ph of)\b"),
    _rule(CapabilityId.CHEMISTRY, 2, r"\b(mole|molecule|compound|reaction|acid|alkali|base)\b"),
    _rule(CapabilityId.CHEMISTRY, 2, r"\b(atom|valence|catalyst|solution|element)\b"),
    # Biology
    _rule(CapabilityId.BIOLOGY, 4, r"\b(photosynthesis|mitosis|meiosis|osmosis|enzyme)\b"),
    _rule(CapabilityId.BIOLOGY, 4, r"\b(dna|rna|chromosome|genotype|phenotype|allele)\b"),
    _rule(CapabilityId.BIOLOGY, 4, r"\b(respiration|homeostasis|ecosystem|photosynth)\b"),
    _rule(CapabilityId.BIOLOGY, 2, r"\b(cell|organism|gene|protein|tissue|bacteria)\b"),
    _rule(CapabilityId.BIOLOGY, 2, r"\b(evolution|species|nucleus|membrane|hormone)\b"),
    # Coding
    _rule(CapabilityId.CODING, 4, r"\b(syntax error|stack trace|traceback|segfault)\b"),
    _rule(CapabilityId.CODING, 4, r"\b(python|javascript|java|c\+\+|sql|typescript)\b"),
    _rule(
        CapabilityId.CODING, 4, r"\b(function|variable|loop|array|recursion)\b.*\b(code|program)\b"
    ),
    _rule(CapabilityId.CODING, 4, r"```|\bdef \w+\(|\bfor\s*\(|\bconsole\.log\b|\bprint\("),
    _rule(CapabilityId.CODING, 2, r"\b(debug|compile|algorithm|runtime error|null pointer)\b"),
    _rule(CapabilityId.CODING, 2, r"\b(my code|this code|the code|bug in)\b"),
    # Writing feedback
    _rule(CapabilityId.WRITING_FEEDBACK, 4, r"\b(my essay|my paragraph|my thesis|my draft)\b"),
    _rule(CapabilityId.WRITING_FEEDBACK, 4, r"\b(feedback on|review my|critique my)\b"),
    _rule(
        CapabilityId.WRITING_FEEDBACK, 2, r"\b(essay|paragraph|thesis|introduction|conclusion)\b"
    ),
    _rule(CapabilityId.WRITING_FEEDBACK, 2, r"\b(improve my writing|writing style|structure)\b"),
    # Language help
    # Weight 4: unambiguous subject markers must outrank the generic "what is" /
    # "explain" openers, which fire on nearly every question a student asks.
    _rule(CapabilityId.LANGUAGE_HELP, 4, r"\b(grammar|tense|preposition|conjugat)\b"),
    _rule(
        CapabilityId.LANGUAGE_HELP, 4, r"\b(translate|translation|in french|in spanish|in hindi)\b"
    ),
    _rule(CapabilityId.LANGUAGE_HELP, 2, r"\b(vocabulary|synonym|antonym|spelling|pronoun)\b"),
    _rule(CapabilityId.LANGUAGE_HELP, 2, r"\b(sentence|comprehension|meaning of the word)\b"),
    # Answer checking
    _rule(CapabilityId.ANSWER_CHECK, 4, r"\b(check my answer|is my answer|did i get)\b"),
    _rule(CapabilityId.ANSWER_CHECK, 4, r"\b(am i right|is this correct|verify my)\b"),
    _rule(CapabilityId.ANSWER_CHECK, 2, r"\b(i got|my answer is|i think the answer)\b"),
    # Explain concept
    _rule(CapabilityId.EXPLAIN_CONCEPT, 3, r"\b(what is|what are|define|definition of)\b"),
    _rule(CapabilityId.EXPLAIN_CONCEPT, 3, r"\b(explain|describe|tell me about)\b"),
    _rule(CapabilityId.EXPLAIN_CONCEPT, 2, r"\b(how does|how do|why does|difference between)\b"),
    # Homework solve
    _rule(CapabilityId.HOMEWORK_SOLVE, 3, r"\b(question \d+|q\d+\b|exercise \d+|problem \d+)\b"),
    _rule(CapabilityId.HOMEWORK_SOLVE, 3, r"\b(my homework|this homework|assignment)\b"),
    _rule(CapabilityId.HOMEWORK_SOLVE, 2, r"\b(solve|work out|calculate|find the value)\b"),
)

# Difficulty markers. Deterministic, so tests can pin the model tier chosen.
_ADVANCED_MARKERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(prove|proof|derive|derivation|rigorous|from first principles)\b"),
    re.compile(
        r"\b(differential equation|partial derivative|laplace|fourier|eigen"
        r"|taylor series|maclaurin|convergence|multivariable|triple integral"
        r"|double integral|stokes|divergence theorem|lagrangian|hamiltonian)\b"
    ),
    re.compile(r"\b(quantum|relativis|thermodynamic cycle|statistical mechanics)\b"),
    re.compile(r"\b(olympiad|jee advanced|competition problem|university level)\b"),
)

_SIMPLE_MARKERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*(what is|who is|when is|define)\b"),
    re.compile(r"\b(simple|basic|beginner|class \d|grade \d)\b"),
)

_MIN_CONFIDENT_SCORE = 2
"""A single weak (weight-1/2) hit is not enough to commit; below this the router
either falls back to GENERAL_TUTORING or asks for a cheap classification."""


def detect_pedagogy_request(text: str) -> PedagogyMode | None:
    """Explicit student request for a different explanation depth."""
    lowered = text.lower()
    for mode, phrases in _MODE_PHRASES:
        if any(phrase in lowered for phrase in phrases):
            return mode
    return None


def detect_follow_up(text: str, *, has_history: bool) -> bool:
    """A follow-up needs prior turns to be meaningful."""
    if not has_history:
        return False
    lowered = text.strip().lower()
    if any(pattern.search(lowered) for pattern in _FOLLOW_UP_PATTERNS):
        return True
    # A very short message after existing turns is almost always continuation.
    return len(lowered.split()) <= 4


def estimate_difficulty(text: str, capability: CapabilityId) -> Difficulty:
    """Deterministic difficulty estimate, used to pick the cheapest viable tier."""
    lowered = text.lower()
    if any(pattern.search(lowered) for pattern in _ADVANCED_MARKERS):
        return Difficulty.ADVANCED
    if any(pattern.search(lowered) for pattern in _SIMPLE_MARKERS) and len(lowered) < 120:
        return Difficulty.SIMPLE
    # Long multi-part questions cost more context and reasoning regardless of topic.
    if len(lowered) > 600 or lowered.count("?") >= 3:
        return Difficulty.ADVANCED
    if capability is CapabilityId.EXPLAIN_CONCEPT and len(lowered) < 100:
        return Difficulty.SIMPLE
    return Difficulty.MODERATE


def score_capabilities(text: str) -> dict[CapabilityId, int]:
    lowered = text.lower()
    scores: dict[CapabilityId, int] = {}
    for rule in _RULES:
        if rule.pattern.search(lowered):
            scores[rule.capability] = scores.get(rule.capability, 0) + rule.weight
    return scores


def classify(text: str | None, *, has_history: bool = False) -> IntentDecision:
    """Route an inbound message. Never calls a model.

    When the rules cannot commit, this returns GENERAL_TUTORING with
    `used_model=False`; the caller may optionally escalate to a cheap
    classification call, which is the only place a model touches routing.
    """
    cleaned = (text or "").strip()
    requested_mode = detect_pedagogy_request(cleaned)
    is_follow_up = detect_follow_up(cleaned, has_history=has_history)

    if not cleaned:
        return IntentDecision(
            capability=CapabilityId.UNKNOWN,
            difficulty=Difficulty.SIMPLE,
            reason="empty_message",
            is_follow_up=False,
            requested_mode=requested_mode,
        )

    # A follow-up inherits the conversation's topic; re-classifying "why step 2?"
    # in isolation would misroute it, so it is handled before scoring.
    if is_follow_up:
        return IntentDecision(
            capability=CapabilityId.GENERAL_TUTORING,
            difficulty=estimate_difficulty(cleaned, CapabilityId.GENERAL_TUTORING),
            reason="follow_up_inherits_conversation_topic",
            is_follow_up=True,
            requested_mode=requested_mode,
        )

    scores = score_capabilities(cleaned)
    if not scores:
        return IntentDecision(
            capability=CapabilityId.GENERAL_TUTORING,
            difficulty=estimate_difficulty(cleaned, CapabilityId.GENERAL_TUTORING),
            reason="no_rule_matched_default_general",
            requested_mode=requested_mode,
        )

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0].value))
    top, top_score = ranked[0]
    runner_up_score = ranked[1][1] if len(ranked) > 1 else 0

    if top_score < _MIN_CONFIDENT_SCORE:
        return IntentDecision(
            capability=CapabilityId.GENERAL_TUTORING,
            difficulty=estimate_difficulty(cleaned, CapabilityId.GENERAL_TUTORING),
            reason=f"weak_signal_score_{top_score}_default_general",
            requested_mode=requested_mode,
        )

    # A subject rule and a task rule both firing is normal ("solve this quadratic"
    # is MATH + HOMEWORK_SOLVE). The subject wins because it determines the prompt
    # and the verifier policy; the task only shapes formatting.
    reason = (
        f"rule_score_{top_score}"
        if top_score > runner_up_score
        else f"rule_tie_{top_score}_resolved_by_priority"
    )
    return IntentDecision(
        capability=top,
        difficulty=estimate_difficulty(cleaned, top),
        reason=reason,
        requested_mode=requested_mode,
    )
