# TutorTwin Cost Controls

Status: Phase 02. Every control below is implemented and tested.

## Principle

The largest controllable cost is not compute. It is unnecessary model calls.
TutorTwin refuses or downgrades expensive work *before* it happens, and the
refusal is structural rather than advisory.

## The gates, in order

`policies/budget_policy.decide()` is a pure function — no I/O, no clock, no
randomness — so every routing choice is unit-testable. Precedence is the design:

| # | Gate | Outcome when it fires |
|---|---|---|
| 1 | Global AI kill switch (`feature_flags.ai_enabled`) | `FEATURE_DISABLED` |
| 2 | Plan allows paid AI | `REJECT_PLAN` |
| 3 | Capability executable this phase | `FEATURE_DISABLED` |
| 4 | Capability in the plan's matrix | `REJECT_PLAN` |
| 5 | System daily budget | `REJECT_SYSTEM_BUDGET` |
| 6 | User daily budget | `REJECT_QUOTA` |
| 7 | User daily call quota | `REJECT_QUOTA` |
| 8 | Provider health circuit breaker | `REJECT_SYSTEM_BUDGET` |
| 9 | Context size | `REJECT_SIZE` |
| — | *past this line the request may spend* | tier selection |

Cheap, certain rejections run before expensive, uncertain ones, and every gate
that can forbid spending runs before any gate that selects a tier.

System budget outranks user quota deliberately: a blown system budget affects
everyone and must fail closed even for a student with personal quota left.

## Rejections cannot spend

`ExecutionBudgetDecision.alias` is `None` for every non-allow outcome. A provider
call without an alias is not merely disallowed, it is impossible to construct.
`CapabilityExecutor.run()` re-checks `permits_paid_call` as defence in depth.

Proven by `test_no_non_allow_outcome_permits_spend` and, end to end, by
`test_non_pro_student_makes_zero_model_calls` and
`test_quota_exhausted_makes_zero_model_calls`, which assert the provider's own
call counter is zero.

## Model tier selection

Cheapest model that can plausibly do the job:

| Difficulty | Capability | Alias |
|---|---|---|
| SIMPLE | non-STEM | `CHEAP_TEXT` |
| SIMPLE | STEM | `STANDARD_TUTOR` |
| MODERATE | any | `STANDARD_TUTOR` |
| ADVANCED | any (plan permitting) | `ADVANCED_REASONING` |

Simple STEM does *not* get the cheapest model: a short arithmetic question is
still arithmetic, and correctness matters more than the few micros saved.

## What deterministic code decides, never a model

Entitlement · quota · dedupe · file type · cost arithmetic · access control ·
intent routing · difficulty · follow-up detection · pedagogy-change requests ·
conversation summarisation · the media brief gate.

Asking a model any of these would cost money on every turn to answer a question
code already knows.

## Media brief gate

An attachment with no accompanying instruction (< 3 characters of caption) is
answered with `ASK_FILE_BRIEF`. Zero download, zero OCR, zero vision, zero
embedding. It runs *before* tier selection, and entitlement runs before it, so an
ineligible student never reaches media handling at all.

## Verifier selectivity

Second-model verification is off by default. It arms only for advanced STEM
(conditional on LOW confidence at runtime) or explicit high-stakes mode. A
confident advanced answer therefore costs exactly one call.

## Bounded retries

`max_attempts` (default 2) caps total vendor calls across primary *and* fallback
— never per-alias, or a two-alias policy would silently double the ceiling.
Non-retryable errors abort immediately.

## Usage ledger

One row per provider attempt, including failures. Each row stores the vendor
model ID, token counts, cached tokens, estimated cost in micro-dollars, and the
`rate_version` that applied — so historical spend is never recomputed with
today's prices.

Cost is rounded **up**, so many small calls do not systematically under-report.

## Prompt caching

`ModelRequest.cacheable_prefix` carries the byte-stable half of the system prompt
(safety + identity + persona). Anthropic receives it with an `ephemeral`
`cache_control` breakpoint; OpenAI receives it first in the message list, which
is what its automatic caching keys on. The volatile capability and format blocks
follow the breakpoint so they cannot invalidate the cached span.

## Quota accounting

`load_quota_snapshot` reads counters; it does not reserve. Two concurrent
requests can both observe the same count and both proceed, so a limit is soft by
at most the number of in-flight requests. A hard reservation would require a row
lock held across the provider call — exactly what the serverless design forbids.
This is a deliberate trade, documented here rather than discovered later.

## Model catalog

`model_catalog` maps alias → vendor + model ID + price. Vendor model strings
appear **only** in that table, the seed migration, and
`providers/registry.py`. Verified by grep in the acceptance report.

## Observed costs (fake provider, deterministic)

| Scenario | Provider calls | Ledger rows |
|---|---|---|
| Simple biology question | 1 (`CHEAP_TEXT`) | 1 |
| Advanced calculus | 1 (`ADVANCED_REASONING`) | 1 |
| Advanced STEM, truncated output | 2 (+ verifier) | 2 |
| Timeout then success | 2 (retry/fallback) | 2 |
| Duplicate event | 0 | 0 |
| FREE student | 0 | 0 |
| Quota exhausted | 0 | 0 |
| Media without brief | 0 | 0 |

## Admin-configurable (Phase 06 UI)

`feature_flags.ai_enabled` is live now. Per-plan limits live in
`policies/budget_policy.py` (`FREE_PLAN`, `PRO_PLAN`) and move to
`plan_policies` rows when the admin control plane lands.

---

# Phase 03: media cost controls

The largest avoidable cost in an education product is processing a file nobody
asked a question about. Phase 03 makes that structurally hard.

## The media gate

For IMAGE / PDF / DOCUMENT with no usable brief: **zero** downloads, OCR,
embeddings, vision calls, transcriptions. One database row, one question back.

Enforced by the state machine, not by discipline — there is no transition from
`WAITING_FOR_BRIEF` to `FETCH_QUEUED`. See [MEDIA_PIPELINE.md](MEDIA_PIPELINE.md).

Ordering inside intake, cheapest refusal first:

1. **entitlement** — an ineligible student costs zero downloads
2. **brief gate** — no instruction, no fetch
3. **job creation** — only now is work scheduled

## Method escalation

Never skips a cheaper method without recording why:

```
DIGITAL_TEXT (free) → LOCAL_OCR (our CPU) → VISION (a frontier model)
```

A 20-page digital PDF costs **zero OCR and zero vision**. A mixed document OCRs
only its scanned pages. Vision runs only where `assess()` judged OCR unusable,
and `escalation_reason` records which signal triggered it.

## Page budget applied at planning time

`plan_extraction()` truncates to `max_pages_processed` before anything runs, so
executing a plan cannot overspend. `"explain the graph on page 13"` produces a
one-page plan against a twenty-page document.

## Budget decision still governs vision

`_vision_page()` refuses when `decision.permits_paid_call` is false. A
quota-exhausted student gets degraded extraction, not a surprise bill.

## Caching

Extraction is cached by `(subject, sha256, parser_version, page, method)`. The
same file sent twice is OCR'd once. **Owner-scoped**: two students sending
identical bytes get two extractions, because cross-student cache reuse would be
a privacy leak, not an optimisation.

## Measured media costs (fake providers, deterministic)

| Scenario | Downloads | OCR pages | Vision | Transcriptions |
|---|---|---|---|---|
| non-Pro sends PDF | **0** | **0** | **0** | — |
| Pro sends PDF, no brief | **0** | **0** | **0** | — |
| brief names page 13 of 20 | 1 | 0 | 0 | — |
| digital PDF, 5 pages | 1 | **0** | **0** | — |
| scanned page in a 3-page doc | 1 | **1** | 0 | — |
| OCR unusable | 1 | 1 | **1** | — |
| vision but quota exhausted | 1 | 1 | **0** | — |
| image, no brief | **0** | **0** | **0** | — |
| duplicate media event | 1 | 1 | 0 | — |
| identical file, second send | 1 | **0** (cache) | 0 | — |
| oversized file | 1 | **0** | **0** | — |
| malformed PDF | 1 | **0** | **0** | — |
| executable disguised as PDF | 1 | **0** | **0** | — |
| voice, non-Pro | — | — | — | **0** |
| voice, eligible | — | — | — | **1** |
| voice over duration cap | — | — | — | **0** |

Rejections still cost one download because MIME must be sniffed from real bytes;
a claimed content type is attacker-controlled. Nothing more is spent.

---

# Phase 04: RAG and memory cost controls

## Retrieval is off by default

The largest RAG cost is retrieving for questions that never needed it. "Solve
2x + 5 = 13" pays an embedding call to answer something the model already knows.

`policies/rag_policy.decide()` is a pure function. Cheap refusals run first:

1. **no corpus** — retrieval would scan nothing and still cost an embedding
2. **budget forbids** — an embedding call is a paid call
3. **too short to target** — "why?" retrieves noise at full price
4. strong positive signals (source request, upload reference, tutor, syllabus)
5. **follow-up** — the context is already loaded; re-retrieving pays twice
6. **self-contained computation** — the default skip

## Free before paid

Keyword search (`ts_rank` + GIN index) costs nothing and beats vectors at exact
terms. When it alone returns three or more owned passages, the embedding call is
skipped entirely.

## Embedding cache

Keyed by normalized text hash + model. Identical text is never embedded twice —
across documents, across students, across re-uploads.

Measured: two students uploading the same textbook cost **one** embedding call
between them; a repeated query costs **zero**.

## Ingestion idempotency

`uq_source_content` means re-uploading the same file creates zero chunks and
makes zero embedding calls. Boilerplate repeated across pages is indexed once.

## Dynamic top-k

`top_k` is sized to the remaining context budget, so ranking work is never spent
on passages that would be discarded. With 900 tokens available, k drops from 8
to 5; with 100, to 0.

## Summaries and profiles are not model-generated

Conversation summaries use deterministic truncation. A model summary would add a
paid call per turn on long conversations — a cost that scales with engagement,
which is the wrong shape.

The inferred learning profile regenerates every 10 attempts
(`PROFILE_REFRESH_ATTEMPTS`), not every message.

## Measured RAG costs (deterministic provider)

| Scenario | Embedding calls | Vector queries |
|---|---|---|
| "Solve 2x + 5 = 13" | **0** | **0** |
| "What is photosynthesis?" | **0** | **0** |
| Follow-up ("why step 2?") | **0** | **0** |
| No corpus yet | **0** | **0** |
| Budget refused | **0** | **0** |
| Keyword search alone | **0** | 0 |
| "Summarize this document" | 1 | 1 |
| Same query repeated | **0** (cache) | 1 |
| Re-upload identical file | **0** | — |
| Same file, second student | **0** (cache) | — |

---

# Phase 05: learning-engine costs

The engine adds a large amount of capability and almost no cost, because the
expensive-looking parts are arithmetic.

## What costs nothing

| Path | Model calls | Why it is free |
|---|---|---|
| Problem normalisation, subject detection | **0** | character map and word list |
| Stated assumptions | **0** | table lookup per subject |
| Substitution / symbolic / numeric / unit verification | **0** | SymPy and Pint |
| Verification policy decision | **0** | pure function |
| Cross-model comparison | **0** | field comparison over two answers already paid for |
| Twin problem, any batch size | **0** | template transformation with a computed answer |
| Plot, geometry, free-body, block diagram, circuit, TikZ | **0** | rendered from a validated spec |
| Printable mock paper | **0** | text rendering of the student projection |
| Spaced-repetition scheduling | **0** | SM-2 arithmetic |
| MCQ / true-false / numeric grading, any count | **0** | comparison, with unit conversion |
| Exact short-answer match | **0** | normalised comparison |
| Blueprint arithmetic | **0** | duration to question count and marks |
| Weak-topic analysis | **0** | counters and thresholds |

## What costs one call

| Path | Model calls | Note |
|---|---|---|
| Study notes | 1 | one call regardless of source count or section count |
| Essay feedback | 1 | covers five dimensions, paragraph notes and the rubric grade |
| Rubric grading of N subjective questions | 1 | batched: ten questions is one call, not ten |
| Tutoring answer | 1 | unchanged from Phase 02 |

## What costs two

Only this: advanced STEM, LOW confidence, **and** the local check came back
inconclusive. A local REFUTED or VERIFIED ends the pipeline at one call, because
a second model can only agree with the maths or be wrong about it.

## Content-addressed artifacts

`uq_artifact_subject_sha` means asking for the same graph twice stores one blob
and one row. Measured: two identical `render_and_store` calls produce one
`learning_artifacts` row, the second returning `reused: true`.

Card identity is a content hash for the same reason: regenerating a deck from an
unchanged document adds zero cards and zero storage.

## Plan ceilings on generated work

`BlueprintLimits.for_plan()` caps a FREE plan at 30 minutes and 10 questions, PRO
at 180 and 50. A FREE request for a 180-minute paper returns a 30-minute paper
and a `truncated_reason` explaining the reduction, rather than either silently
shortening it or generating a paper the plan does not cover.

Twin batches are capped the same way. Because generation is deterministic the cap
bounds work and keeps a practice set a sensible size; it is not rationing spend,
since the spend is zero at any N.

## Authorship bound as a cost control

Essay rewrite suggestions are capped at 600 characters and a quarter of the
essay's own length. That bound exists for authorship, not for tokens, but it also
caps the output length of the most output-heavy capability in the engine.

---

# Phase 07: enforceable ceilings

Phase 02 built the per-request gate. This phase added the ceilings *above* one
request — the ones that bound the invoice rather than one student's afternoon —
and, more importantly, wired the ones that had been computed and then ignored.

## Three levels, and what each is for

| Level | Bounds | Fires on |
|---|---|---|
| **request** | one message | entitlement, feature flag, capability, context size |
| **student** | one person's day and month | calls, spend, PDF pages, OCR pages, voice seconds, mock tests |
| **system** | the bill | hourly velocity, daily total, per-vendor total, job concurrency, circuit breaker |

Every counter is derived from rows the system already wrote for its own reasons —
`usage_ledger`, `media_objects`, `media_extractions`, `assessments`. Nothing is a
separately maintained counter, because a counter that drifts from the ledger is
worse than no counter at all.

## Precedence

The order in `budget_policy.decide()` is load-bearing. When two ceilings are both
exceeded, the one that fires decides what an operator is told — and "the platform
is over budget" and "this student is over budget" call for different responses.

```
1.  ai_enabled kill switch      cheapest check; an operator pulling it beats everything
2.  plan allows paid AI         the primary cost gate
3.  capability is executable
4.  capability is in the plan
5.  system spend VELOCITY       <- fires while a runaway is still running
6.  system daily budget
7.  per-provider daily budget
8.  student monthly budget
9.  student daily budget
10. student daily call count
11. provider circuit breaker
12. context size                reject rather than silently truncate
--- past this line a request may spend money; now pick the cheapest tier ---
```

**Velocity before the daily total** is the whole reason velocity exists. A daily
ceiling alone is an epitaph: a retry loop can spend the day's budget in four
minutes, and the daily gate only fires once it already has.

## System ceilings

```
TUTORTWIN_SYSTEM_HOURLY_BUDGET_MICROS    10000000   $10/hour
TUTORTWIN_SYSTEM_DAILY_BUDGET_MICROS     50000000   $50/day
TUTORTWIN_PROVIDER_DAILY_BUDGET_MICROS   30000000   $30/day/vendor
TUTORTWIN_PROVIDER_FAILURE_CIRCUIT       5
TUTORTWIN_HEAVY_JOB_MAX_CONCURRENCY      10
```

**Per-vendor, not only global.** A single system ceiling lets one provider's
runaway consume the whole budget and leave the service with no working fallback —
the fallback is then refused for a budget it did not spend. Per-vendor ceilings
keep the second vendor available for exactly the incident that needs it.

**Where the per-vendor ceiling is enforced.** Not in `decide()`: the tier decision
picks an *alias*, and only the catalog knows which vendor an alias resolves to. So
TX1 computes spend and failures for every vendor in one statement, derives a
blocked set, and the **gateway** skips a blocked vendor and falls through to the
fallback. Skipped, not refused — falling through to a healthy vendor is the point.

**Heavy-job concurrency is counted in Postgres**, not in process memory. There is
no single process; there are as many containers as Cloud Run decided to start. A
per-process semaphore would bound one container and let the platform start twenty
more. A `RUNNING` row older than 15 minutes stops counting, so a container killed
mid-job cannot wedge the ceiling permanently.

## Per-student media ceilings

```
TUTORTWIN_STUDENT_DAILY_PDF_PAGES        100
TUTORTWIN_STUDENT_DAILY_OCR_PAGES        60
TUTORTWIN_STUDENT_DAILY_VOICE_SECONDS    900
TUTORTWIN_STUDENT_DAILY_MOCKS            5
TUTORTWIN_STUDENT_MONTHLY_BUDGET_MICROS  20000000
```

Checked at **intake**, immediately after the entitlement gate and before the brief
gate schedules any work. That is the last point at which refusing is free: one row
further on the bytes have been fetched, and a page already read by vision has
already been paid for.

The check is **projected**, not measured — "would one more cross the line". The
real page count is not known until the file is read, and fetching a file to decide
whether we are allowed to fetch it is the cost this gate exists to avoid.

A student who crosses one is told which allowance ran out and that it resets,
rather than being told the feature does not exist.

The monthly ceiling is deliberately **not** thirty times the daily one. $2/day for
30 days is $60; the daily cap exists to bound a bad day, not to be a licence.

## Retry accounting

The brief calls this out by name, and it has two halves that pull in opposite
directions:

> Retry must not double-charge internal quota if the provider call never
> succeeded. Actual provider cost must still be recorded if the provider billed
> it.

Every attempt is written to `usage_ledger`, including failures — that is what makes
a retry storm visible instead of invisible until the invoice arrives. But
`calls_today`, the number checked against the daily call limit, counts only
attempts that **reached a working provider**:

```sql
-- "reached a provider" is read from the evidence, never asserted
(output_tokens > 0) OR (estimated_cost_micros > 0)
```

A timeout that still billed the input tokens is charged, because it was. A
connection refused is not, because it was not. Three failed retries therefore cost
the student zero calls for an answer they never received.

Asserted by `test_provider_failures_do_not_consume_the_students_call_quota`.

## Provider retry backoff

Exponential with full jitter, capped at 8 seconds, and only *between* attempts —
sleeping after the final failure delays the error message and buys nothing.

Jitter matters because a provider outage fails every in-flight request at once.
Without it, all of them retry on the same second and the vendor's recovery is met
with the same spike it is recovering from.

## Prompt caching

Both adapters implement provider-side prompt caching, and until this phase nothing
set `cacheable_prefix` — so the support was dead code and the stable half of the
system prompt was re-read at full price on every turn.

The split is made in the orchestrator, the only layer that knows what is stable
for *this* conversation: safety rules, tutor identity and persona go in the
cacheable prefix; the capability and pedagogy blocks change per turn and would
bust the cache.

Measured by `scripts/cost_trace.py`: **1,546 of 1,938 system-prompt characters**
are now marked cacheable — about 80%, on every turn after the first.

## Auditing the shape of the spend

```bash
python scripts/cost_trace.py            # call graph per scenario, plus findings
python scripts/cost_trace.py --json     # for the acceptance report
```

It runs representative conversations through the real orchestration path with a
scriptable fake vendor and reports the call graph each produced. The numbers are
not production numbers — the fake's token counts are fixed. What *is* real is the
call count, the call order and which prompt bytes were marked cacheable, and those
three decide the bill.

It answers the audit questions by construction:

| Question | Signal | Measured |
|---|---|---|
| A classifier LLM where a rule would do? | `classifier_calls` | 0 — routing is rules |
| Persona tokens re-sent uncached? | `cacheable_chars` | 1,546 cached |
| A second model when the first was confident? | `verifier_calls` | 0 unless LOW confidence |
| A duplicate delivery paid for twice? | calls per turn | 1 call for 2 deliveries |
| An unentitled student reaching a provider? | `provider_calls` | 0 |
| Whole history re-sent every turn? | message count | grows by 2 per turn, capped by the context budget |

It exits non-zero when any of those regress, so it can run in CI as a cost
regression test rather than a report somebody remembers to read.

## Batch APIs

Not used. Every capability in this phase is interactive — a student is waiting —
and a batch API trades latency for price. The retention sweep is the only
non-interactive work, and it makes no model calls at all.
