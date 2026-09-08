# TutorTwin Implementation Status

Last updated: end of Phase 07.

## Phases

| Phase | Scope | Status |
|---|---|---|
| 01 | Standalone foundation | **COMPLETE** |
| 02 | Meta-agent orchestration, model routing | **COMPLETE** |
| 03 | Multimodal media, OCR, PDF, audio | **COMPLETE** |
| 04 | RAG, memory, cache | **COMPLETE** |
| 05 | Education engine, tests, practice | **COMPLETE** |
| 06 | Next.js admin control plane | **COMPLETE** |
| 07 | Serverless hardening, cost QA | **COMPLETE** |
| 08 | Lead Intake handoff | Not started |
| 09 | Website/MySQL entitlement | Not started |

## Built and tested

### Phase 01
Normalized event contract v1.0 · idempotency · identity/entitlement ordering ·
media brief gate · conversation persistence with ownership checks · 16 tables ·
serverless engine tuning · structured logging with redaction · health/readiness ·
shared-secret internal auth · 10 ports with deterministic fakes.

### Phase 02
- 23-member capability registry; 11 text capabilities execute
- Deterministic intent router (scored rule table, **zero model calls**)
- `ExecutionBudgetDecision` cost gate: 9 ordered checks, 100% covered
- Provider gateway with OpenAI + Anthropic adapters behind one internal schema
- DB-driven model catalog; **no vendor model ID in business code**
- Bounded retry + fallback that reserves an attempt per alias
- Usage ledger: exactly one row per provider attempt, failures included
- Versioned modular prompts with a structural prompt-injection boundary
- Six pedagogy modes with student-request override
- Confidence from deterministic signals; selective second-model verification
- Context assembly under a token budget; summaries cost no model call
- Split-transaction pipeline: TX1 → provider call (no transaction) → TX2
- CLI conversation harness with per-turn cost tracing

### Phase 03
- Media state machine: fetching is unreachable without a brief
- Local-first PDF pipeline: digital text -> local OCR -> vision, never reordered
- Deterministic page targeting ("page 13" reads 1 page of 20); local BM25
  scoring otherwise, no embeddings
- Honest OCR assessment; vision escalation only on a named failure signal
- BlobStore: content-addressed, private, owner-checked; filesystem + R2 adapters
- File security: magic-byte MIME, executable/archive rejection, bomb guards,
  filename sanitisation
- Owner-scoped extraction cache
- Jobs table + authenticated internal endpoint; Cloud Tasks carries only an id
- Voice: entitlement-gated, exempt from the brief gate by design
- `ImagePart` added to the provider contract; both vendors send real pixels

### Phase 04
- Visibility model (GLOBAL_CURATED / TUTOR / COURSE / STUDENT_PRIVATE /
  CONVERSATION) enforced **in SQL**, before ranking - wrong-owner retrieval is
  exactly zero, proved by `candidates_scanned == 0`
- Idempotent ingestion: re-upload costs 0 chunks and 0 embedding calls
- Content-aware chunking with page and section provenance
- Content-addressed embedding cache; identical text embedded once ever
- Hybrid retrieval: free keyword search first, then vectors, fused by RRF
- RAG decision policy - retrieval is OFF by default, on only when earned
- Prompt-injection containment: retrieved text fenced as quoted evidence in the
  user turn, never the system prompt
- Student memory with data minimisation for minors; observed stats kept strictly
  apart from inferred conclusions
- Priority-based context budgeter; whole units dropped, never sliced
- RAG evaluation harness: 100% recall, 100% citation accuracy, 0 leaks

### Phase 05
- STEM solver pipeline: normalize -> subject -> assumptions -> solve -> local
  verification -> confidence -> selective second model, with every step outside
  "solve" costing nothing
- Unicode operator normalisation, which is what makes a photographed worksheet
  verifiable at all
- `VerificationPolicy` returns a decision **and its reason**, so tests assert why
  a paid call happened rather than only counting calls; a local REFUTED or
  VERIFIED ends the pipeline before the second model
- Cross-model comparison on final result, assumptions and named intermediates;
  disagreement produces a qualified answer, never a coin flip
- Deterministic visuals: plots, geometry, free-body, block/flow diagrams, series
  circuits, sanitised SVG and TikZ source - **zero model calls, ever**
- `VisualArtifactService` with content-addressed storage: the same graph twice is
  one blob and one row
- SVG sanitiser hardened against entity expansion; billion-laughs and XXE both
  rejected in under 0.02 ms, asserted on elapsed time
- Twin problems with computed, verified answers; batch generation free at any N
- Structured notes in one call, with **citations allow-listed against the sources
  actually supplied** and hallucinated ones dropped and recorded
- Persistent decks with SM-2 scheduling (1, 6, 15, 38, 95, 238 days) and an
  append-only review log
- Assessments: answer key withheld structurally, in the type *and* in the select
  list; objective grading free at any count, rubric grading batched into one call
- Printable mock papers rendered from the student projection, so the artifact
  cannot contain the key
- Essay feedback in one call with an authorship bound enforced in code, not asked
  for in a prompt
- `DisabledSandbox` refuses all execution; `ArithmeticCalculator` is a separate
  type under the same two-layer parse filter
- Mastery bands with no fake precision; thin-evidence topics excluded from
  recommendations entirely
- 10 new tables, one migration, `alembic check` clean for the first time

### Phase 06

- A production Next.js 16 control plane at `apps/admin`, built and deployed
  independently of the Python service; four runtime dependencies, no UI kit and
  no chart library
- Argon2id admin passwords, hashed session and CSRF tokens, row-backed login
  rate limiting, forced first-login password change, no default production
  password
- Six roles over 24 permissions, enforced by `require(...)` on all 46 admin
  endpoints; a refused section answers a real HTTP 403 from the shell
- Ten high-risk actions gated on a typed reason and an explicit confirmation,
  each writing an audit event with before/after state
- Fourteen sections over real data, with server-side pagination, filtering and
  search on every unbounded table
- Content-Security-Policy with a per-request nonce; no `unsafe-inline` for
  scripts, which is also why the dashboard draws its bars with a `<div>`
- Cost attribution by capability (a new nullable `usage_ledger` column recorded
  at call time), tutor, media and verification; p50/p95 request latency and
  prompt-cache hit share on the dashboard
- 72 admin security tests plus 17 Playwright scenarios against a real API and a
  real database

### Phase 07

- Two Cloud Run services (API and control plane, separate images), a Cloud Tasks
  queue and a Cloud Scheduler retention job, described by 562 lines of Terraform
  that `validate`s clean
- OIDC on `/internal/*`, checking the token's audience **and** its service
  account; the shared secret stays for `/v1/events`, whose caller is not on
  Google's identity plane
- A deployed environment refuses to start mis-wired: `require_deployable()`
  rejects missing R2, Cloud Tasks, migration DSN or internal key before the port
  opens
- Per-job-type retry policy with exponential backoff and full jitter, bounded by
  the queue *and* by the row, so a queue misconfiguration cannot produce a retry
  storm
- Fleet-wide heavy-job concurrency ceiling, counted in Postgres because there is
  no single process; saturation sheds load with 429 rather than piling on
- Cost ceilings at three levels, all reachable at runtime: spend velocity, daily
  and per-vendor system budgets, monthly and daily student budgets, and per-student
  PDF/OCR/voice/mock allowances checked before a byte is fetched
- Retry accounting: a failed attempt is on the ledger but does not consume the
  student's call quota unless the vendor actually billed it
- Retention sweeper behind an authenticated endpoint, idempotent and bounded,
  blob-before-row so a failure cannot strand an object
- Graceful SIGTERM drain, startup probe on `/readyz`, liveness on `/healthz`
- 56 new tests: 16 chaos, 15 threat-model, 13 cost-ceiling, 7 load, 5 media-wiring
- Five-job CI gating the image build on lint, types, migrations, tests, the
  control-plane build, the end-to-end suite and `terraform validate`
- Six pre-existing defects found and fixed - see
  [FINAL_STANDALONE_ACCEPTANCE.md](FINAL_STANDALONE_ACCEPTANCE.md#defects-found-and-fixed-during-this-phase)

## Verified numbers

| Metric | 01 | 02 | 03 | 04 | 05 | 06 | 07 |
|---|---|---|---|---|---|---|---|
| Python tests | 57 | 206 | 281 | 370 | 539 | 611 | **667** |
| Control-plane tests | - | - | - | - | - | 40 + 17 e2e | **40 + 17 e2e** |
| Coverage | 94% | 92% | 86% | 86% | 88% | 82% | **80%** |
| Ruff | clean | clean | clean | clean | clean | clean | clean |
| Ruff format | - | - | - | - | - | - | **clean** |
| Mypy (strict) | 34 | 48 | 60 | 71 | 85 | 100 | **102 files** |
| Tables | 16 | 16 | 19 | 26 | 36 | 40 | **40** |
| Migrations | 1 | 2 | 3 | 4 | 5 | 7 | **7** |
| Terraform | - | - | - | - | - | - | **validate clean** |
| Real money spent | $0.00 | $0.00 | $0.00 | $0.00 | $0.00 | $0.00 | **$0.00** |

Coverage drifts down as deployment surface grows. Phase 06 added ~1,000 lines of
control-plane API exercised by behaviour rather than line by line; Phase 07 added
cloud adapters that are selected by configuration and covered by fakes. The
control plane's own 40 unit tests, 17 end-to-end scenarios and the Terraform are
counted separately - `coverage` measures the Python package only.

## Stack

Python 3.12.10 · FastAPI 0.141.1 · Pydantic 2.13.5 · SQLAlchemy 2.0.52 ·
Alembic 1.19.1 · psycopg 3.3.4 · structlog 26.1.0 · anthropic 1.2.0 ·
openai 3.6.0 · sympy 1.14.0 · pint 0.25.3 · matplotlib 3.11.1 ·
pytest 9.1.1 · ruff 0.16.5 · mypy 2.3.1. PostgreSQL 18.1.

Control plane: Node 20.19.4 · Next.js 16.3.4 · React 19.2.8 · TypeScript 6.0.3 ·
zod 4.1.13 · Vitest 4.1.11 · Playwright 1.62.1. Four runtime dependencies, no UI
kit, no chart library, no identity SaaS.

## Deliberately deferred

| Item | Phase | Why |
|---|---|---|
| pgvector | 04 | Not needed until embeddings |
| Image OCR/vision execution | 04 | Stored and validated; the extractor is wired for PDFs |
| Retention sweeper | 07 | **Done** - `/internal/retention/sweep`, nightly via Cloud Scheduler |
| Live R2 / Cloud Tasks | 08+ | Adapters written and wired; never run against the real services |
| Cheap-model intent escalation | later | Rules resolve the tested corpus; no measured need |
| Model-generated summaries | later | Would add a paid call per turn |
| Cheap-model rerank | later | RRF already gives 100% recall on the eval corpus |
| Isolated code execution | later | Deliberately still disabled. A subprocess in the API container shares the service account and the database credentials, so it is not a boundary - the requirements for a real one are in SECURITY.md |
| RDKit chemistry structures | later | Optional extension not enabled; nothing claims to draw a molecule |
| Non-series circuits | later | Needs a placement algorithm whose failures are silently wrong diagrams |
| Plan policies in the DB | 06 | **Done** - `plan_policies`, versioned, edited from the control plane |
| Live catalog reads per request | 06 | Static snapshot avoids a hot-path query; the control plane edits `model_catalog`, the service reads it on refresh |
| Admin auth / RBAC | 06 | **Done** - Argon2id, hashed sessions, six roles, server-enforced |
| RAG wired into entry_service | 08+ | Service complete and tested standalone; the learning engine consumes it directly |
| Admin TOTP second factor | later | The login seam exists; half a second factor is worse than none |
| Entitlement override honoured at runtime | 09 | Stored and audited now; the gate reads the website's truth from Phase 09 |
| Rate limiting, retention | 07 | — |

## Constraint compliance

No Redis · No AWS S3 · No Fargate/ECS/Kubernetes · No NAT Gateway ·
No always-on worker · No Gemini or third paid vendor · No binary payloads in
Postgres · No vendor model IDs in business code · No LLM used for a deterministic
decision · No transaction held across a provider call · No Lead Intake or
website coupling · No MySQL · No browser-to-database path · No recurring paid
identity dependency.
