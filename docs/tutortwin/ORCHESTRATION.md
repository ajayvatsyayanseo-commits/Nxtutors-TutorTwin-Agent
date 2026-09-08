# TutorTwin Orchestration

Status: Phase 02. Text-only. This document describes running code.

## The pipeline

Deterministic orchestration, not an autonomous loop. Every step is a plain call
with inspectable inputs and outputs, so "why did this request cost money" always
has an answer.

```
event
 -> idempotency claim          unique constraint; stale claims recoverable
 -> identity                   IdentityGateway (fake until Phase 09)
 -> entitlement                COST GATE 1 - plan must allow paid AI
 -> conversation + request event
 -> persist inbound message
 -> media brief gate           COST GATE 2 - no brief, no processing
 -> deterministic intent       orchestration/router.py, ZERO model calls
 -> context assembly           services/context.py, bounded window
 -> tutor persona + prompts    services/prompts.py, versioned blocks
 -> budget decision            COST GATE 3 - quota, budget, flags, size
 -- [ no transaction held ] --
 -> capability executor        the only paid call
 -> confidence scoring         deterministic signals, not self-assessment
 -> selective verifier         second model only when triggered
 -- [ no transaction held ] --
 -> persist answer + usage ledger + outbound actions
 -> settle idempotency
```

## Transaction shape

The provider call sits between two short transactions and inside neither:

| Phase | Work | Transaction |
|---|---|---|
| TX1 | idempotency, identity, entitlement, conversation, inbound message, routing, budget decision | open, then **commit** |
| — | model gateway call(s) | **none held** |
| TX2 | answer, usage ledger, outbound actions, settle idempotency | open, then **commit** |

This is load-bearing. The pool is `pool_size=2, max_overflow=2` per container;
holding TX1 across a 30-second model call would pin a connection for its whole
duration, and four concurrent requests would exhaust the pool. `db/engine.py`
states the rule; this is where it is honoured.

`api/routes/events.py` opens **no** session — the entry service owns its own
boundaries, because only it knows where the network call falls.

## Deterministic routing

`orchestration/router.py` classifies intent with a scored rule table and makes
**zero model calls**. Paying an LLM to recognise "solve 2x+5=13" costs money on
every turn, forever.

Rules are data rows, not a regex cascade — a cascade hides precedence in line
order. Weights encode precision:

| Weight | Meaning | Example |
|---|---|---|
| 4 | essentially exclusive to one subject | `photosynthesis`, `stoichiometry` |
| 3 | generic question opener | `what is`, `explain` |
| 2 | leans one way but shared | `force`, `solution`, `cell` |

Subject markers sit above openers deliberately: "what is photosynthesis?" is a
biology question, not a generic definition request. The highest total wins, and
`IntentDecision.reason` carries the score, so every route is auditable.

Also deterministic: difficulty estimation, follow-up detection (which requires
prior turns — a cold-open "why step 2?" refers to nothing), and explicit
pedagogy-change requests.

## Capability registry

23 stable IDs in `domain/capabilities.py`. The enum is complete from Phase 02
even though only 11 execute here, because adding a member later would change the
meaning of already-stored rows. Non-executable capabilities route to
`FEATURE_DISABLED` with zero spend.

Executing now: `GENERAL_TUTORING`, `EXPLAIN_CONCEPT`, `HOMEWORK_SOLVE`, `MATH`,
`PHYSICS`, `CHEMISTRY`, `BIOLOGY`, `CODING`, `WRITING_FEEDBACK`,
`LANGUAGE_HELP`, `ANSWER_CHECK`.

## Context assembly

Never sends the whole chat. `services/context.py` builds: rolling summary of
older turns + a recent-turn window (8) + the current message, under a token
ceiling, oldest-trimmed-first.

The summary is produced by **deterministic truncation, not a model**. A
model-generated summary would add a paid call per turn on long conversations —
a background cost that scales with engagement, which is the wrong shape.

`TokenEstimator` is a Protocol; the default is a ~4-chars-per-token heuristic,
deliberately slightly pessimistic so we under-fill rather than overflow.

## Prompt assembly and the injection boundary

Five independently-versioned blocks:

```
[ SAFETY ] [ IDENTITY ] [ PERSONA ] [ CAPABILITY ] [ FORMAT ]   <- system (trusted)
--------------------------------------------------------------
[ summary ] [ recent turns ] [ current message ]                <- user (untrusted)
```

Everything above the line is operator-authored. Everything below is
student-authored. `build_system_prompt()` **takes no student input at all**, so
an instruction inside a message is data being quoted, not policy being set. That
is structural, not a politely-worded request.

Safety precedes persona because an LLM weights earlier instructions more
heavily: the rules that must never bend are stated before any configurable text
that might contradict them.

## Pedagogy modes

`GUIDED`, `HINT_FIRST`, `STEP_BY_STEP`, `ANSWER_AND_EXPLAIN`, `SOCRATIC`,
`EXAM_REVISION`.

Resolution precedence: **explicit student request** > tutor persona default >
`GUIDED`. Student requests are detected deterministically, so changing depth
costs nothing extra.

## Confidence

Bands come from observable signals, never from asking the model how confident it
is — that yields a number that reads calibrated and is not.

| Signal | Effect |
|---|---|
| refusal, empty response | LOW (terminal) |
| truncated output (`max_tokens`) | LOW |
| self-contradiction ("actually, wait") | LOW |
| stated inability | LOW |
| hedging density ≥ 3 | MEDIUM |
| very short answer | MEDIUM |
| clean STEM output | MEDIUM (`stem_unverified`) |
| clean non-STEM output | HIGH |

Clean STEM is capped at MEDIUM: unverified arithmetic is a known weak spot, and
a confidently wrong derivation is worse than an admitted uncertainty. Signals
are recorded on `CapabilityResult.signals`, so a LOW band is explainable.

## Selective verification

The second model is **never** default. `ExecutionBudgetDecision.verifier_mode`:

- `NONE` — simple/moderate work, non-STEM. One call, always.
- `ON_LOW_CONFIDENCE` — advanced STEM only, and only if confidence came back LOW.
- `ALWAYS` — high-stakes grading/test-generation.

So a confident advanced STEM answer still costs exactly one call. The verifier
reviews rather than replaces: it emits AGREE/DISAGREE, and disagreement lowers
confidence rather than silently overwriting the answer.

## Provider gateway

`providers/gateway.py` is the single call surface. Two invariants:

1. **No vendor concept escapes.** Callers pass a `ModelAlias`; the catalog
   resolves the vendor model ID. Business code never sees a model string —
   verified by grep in the acceptance report.
2. **Exactly one `ModelCall` per attempt**, including failures. A timeout can
   still consume input tokens, and an unrecorded failure is how a retry storm
   stays invisible.

Adapters normalize errors into `ErrorCategory` instead of raising, so retry
policy lives in one place rather than split across two vendors' exception trees.
Non-retryable categories (`AUTH`, `BAD_REQUEST`, `CONTENT_FILTER`) abort
immediately — retrying them is pure waste.

**Fallback reserves an attempt.** Without that, the primary's retries consume the
whole budget and the fallback alias never runs, silently turning "fall back to
another model" into "retry the same failing model twice".

Fallback goes one tier *down*, never up: a struggling request should not silently
escalate cost.

---

# Phase 04: retrieval and memory in the pipeline

Two steps join the pipeline, both positioned so they cannot cost money
unnecessarily.

```
... -> deterministic intent -> capability route
    -> RAG DECISION            pure function, zero model calls
    -> [ retrieval ]           only if the decision approved it
    -> MEMORY LOAD             small, bounded, from Postgres
    -> CONTEXT BUDGET          priority allocation across all sections
    -> tutor persona -> prompt assembly -> budget decision
    -> [ provider call ] ...
```

## Where retrieval sits

The RAG decision runs **after** intent routing (which supplies the capability
and follow-up signal) and **before** the budget decision (whose context-size
input depends on how much evidence was retrieved).

It is a pure function, so the common case — a self-contained question — costs
nothing and never reaches the embedding call.

## Where memory sits

Memory is loaded from Postgres in TX1, alongside the conversation history, so it
adds one indexed query and no provider call. Memory *candidates* are extracted
deterministically from the student's message and written in TX2, after the
answer — never on the critical path.

## Context assembly

Phase 02 trimmed history oldest-first under a single ceiling. That is no longer
sufficient now that memory and evidence compete for the same window: naive
trimming would drop the passage the student explicitly asked about to keep a
greeting from four turns ago.

`services/budgeter.py` allocates by priority instead, dropping whole units —
a passage, a turn, a memory — rather than slicing characters.

The evidence block is rendered as fenced, explicitly-untrusted quoted material,
placed in the **user** turn. Retrieved text therefore cannot reach the system
prompt, which is the same structural injection boundary Phase 02 established for
student messages.

---

# Phase 05: where the learning engine sits

The learning engine adds work on both sides of the provider call, and almost all
of it is free.

```
... -> deterministic intent -> capability route
    -> RAG DECISION            pure function, zero model calls
    -> [ retrieval ]           only if the decision approved it
    -> MEMORY LOAD
    -> NORMALIZE PROBLEM       Unicode operators, subject, assumptions - free
    -> CONTEXT BUDGET
    -> tutor persona -> prompt assembly -> budget decision
    -> [ provider call ]
    -> LOCAL VERIFICATION      SymPy / Pint / arithmetic - free
    -> VERIFICATION POLICY     pure function; may end here
    -> [ second provider call ]  only when the policy says so
    -> CROSS-MODEL COMPARISON  final result, assumptions, intermediates - free
    -> pedagogical formatting -> persist artifacts, attempts, task state
```

## Local verification precedes paid verification

The order is the point. A substitution that refutes an answer costs nothing and
is certain; a second frontier model costs money and returns another opinion.
Running the expensive check first would be paying for the weaker signal.

`VerificationPolicy.plan()` returns both the decision and its reason, so a test
asserts *why* a call was or was not made rather than only counting calls. A local
REFUTED or VERIFIED ends the pipeline before the second model is considered.

## Disagreement produces a qualified answer

`compare_solutions()` compares three fields, not whole texts: final result,
assumptions, and named intermediate quantities. On disagreement the primary
answer is returned with a prefix saying the two checks did not agree. There is no
evidence for choosing between two disagreeing models, so choosing one and
presenting it plainly would manufacture confidence that does not exist.

## Artifacts are produced outside the transaction that answers

Rendering is CPU work with no network call, so it happens between TX1 and TX2
where a provider call would sit; the blob write and the `learning_artifacts` row
land in TX2 with everything else. Because artifacts are content-addressed, a
retry finds the existing row instead of storing a second copy.

## Task state is the reason follow-ups resolve

`homework_tasks` holds one row per conversation, written in TX2 alongside the
outbound action. "I don't understand step 3" is answerable on the next turn only
because the steps that were shown are on record; a step that was never displayed
returns a refusal rather than an invention.
