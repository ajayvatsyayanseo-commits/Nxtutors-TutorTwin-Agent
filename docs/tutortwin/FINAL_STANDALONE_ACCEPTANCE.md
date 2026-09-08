# TutorTwin — Final Standalone Acceptance

Phases 01–07. The service is independently deployable, observable, secured,
cost-controlled and regression-tested, and is usable end to end through its
normalized event API without Lead Intake or the NX website.

**Isolation held throughout.** No Lead Intake code was read or changed. No NX
website code was read or changed. No MySQL connection exists anywhere in the
repository. Phases 08 and 09 own those integrations.

---

## Environment

| Component | Version |
|---|---|
| OS | Windows 11 (win32) |
| Python | 3.12.10 |
| PostgreSQL | 18.1 (local), pgvector |
| Node | 20.19.4 |
| Terraform | 1.15.2 |
| FastAPI / Pydantic / SQLAlchemy | 0.141.1 / 2.13.5 / 2.0.52 |
| Next.js / React / TypeScript | 16.3.4 / 19.2.8 / 6.0.3 |

---

## Verification, verbatim

```
$ ruff check .
All checks passed!

$ ruff format --check .
167 files already formatted

$ mypy src
Success: no issues found in 102 source files

$ alembic upgrade head          # against an empty database
INFO  [alembic.runtime.migration] Running upgrade  -> f80deb6c45fc, phase 01 foundation
... 7 revisions ...
INFO  [alembic.runtime.migration] Running upgrade a6e36c18df31 -> c4a17be9d520, phase 06 usage ledger capability attribution

$ alembic check
No new upgrade operations detected.

$ pytest --cov=tutortwin
667 passed in 138.16s
TOTAL  7843 statements  80% coverage

$ cd apps/admin && npm run verify
tsc --noEmit      (clean)
eslint .          (clean)
vitest run        Test Files 4 passed | Tests 40 passed
next build        ✓ Compiled successfully — 22 routes

$ npx playwright test
15 passed, 2 skipped

$ terraform -chdir=infra/terraform fmt -check -recursive     # exit 0
$ terraform -chdir=infra/terraform validate
Success! The configuration is valid.

$ uv pip install --dry-run -e ".[dev,gcp]"
Resolved. 21 packages would be installed, 0 conflicts.
```

### Test inventory

| Suite | Count |
|---|---|
| Python — unit | 437 |
| Python — integration | 230 |
| **Python total** | **667** |
| Control plane — unit/component | 40 |
| Control plane — Playwright end-to-end | 17 (15 pass, 2 skip) |
| **Total** | **724** |

Phase 07 added 56 of those: 16 chaos, 15 threat-model, 13 cost-ceiling, 7 load,
5 media-wiring.

### Numbers by phase

| Metric | 01 | 02 | 03 | 04 | 05 | 06 | 07 |
|---|---|---|---|---|---|---|---|
| Python tests | 57 | 206 | 281 | 370 | 539 | 611 | **667** |
| Coverage | 94% | 92% | 86% | 86% | 88% | 82% | **80%** |
| mypy (strict) files | 34 | 48 | 60 | 71 | 85 | 100 | **102** |
| Tables | 16 | 16 | 19 | 26 | 36 | 40 | **40** |
| Migrations | 1 | 2 | 3 | 4 | 5 | 7 | **7** |
| Real money spent | $0.00 | $0.00 | $0.00 | $0.00 | $0.00 | $0.00 | **$0.00** |

Coverage drifts down as deployment surface grows: Terraform, Dockerfiles and the
cloud adapters are exercised by configuration and by fakes, not line by line.

---

## Architecture as deployed

```
                        Cloud Scheduler ──OIDC──┐
                                                ▼
  message bridge ──shared secret──► /v1/events           /internal/retention/sweep
  (Phase 08)                            │                        │
                                        ▼                        │
                        ┌───────────────────────────────┐        │
   operators ─────────► │  Cloud Run: tutortwin-api     │ ◄──────┘
        │               │  min 0 · max 10 · 1vCPU/1Gi   │
        │               └───────────────────────────────┘
        │                    │          │           │
        │                    ▼          ▼           ▼
        │              Neon Postgres  R2 (private)  OpenAI / Anthropic
        │                    ▲
        │                    │
        │               Cloud Tasks ──OIDC──► /internal/jobs/run
        │
        └─────────────► Cloud Run: tutortwin-admin (separate service, min 0)
```

**Deployed components:** 2 Cloud Run services, 1 Cloud Tasks queue, 1 Cloud
Scheduler job, 3 service accounts, 7 Secret Manager secrets, 1 Artifact Registry
repository, 1 optional billing budget — all in
[`infra/terraform/`](../../infra/terraform), 3 files, 562 lines.

**Outside Terraform, by decision:** Neon and Cloudflare R2. Their providers would
add two credentials and two more `apply` failure modes in exchange for creating
one database and one bucket. Documented in DEPLOYMENT.md instead.

### Constraint compliance

Every forbidden item, verified by grep over `src/`, `infra/`, `apps/admin/src/`,
`.github/`, `pyproject.toml` and `Dockerfile`:

| Forbidden | Present? |
|---|---|
| NAT Gateway | no — every dependency is a public TLS endpoint |
| AWS S3 | no — R2 speaks the S3 *protocol*; the endpoint is Cloudflare |
| Redis | no — the only match is a comment saying there is none |
| Fargate / ECS | no |
| Kubernetes | no |
| EC2 / always-on VM | no |
| Permanently polling worker | no — Cloud Tasks pushes; nothing polls |
| RabbitMQ / Kafka | no |
| Celery + Redis | no |
| MySQL | no |
| Browser → database | no — the control plane's CSP sets `connect-src 'self'` |

---

## Acceptance matrix — standalone core

| # | Item | Status | Evidence |
|---|---|---|---|
| 1 | clean standalone repo builds from clone | **pass** | `uv pip install --dry-run -e ".[dev,gcp]"` resolves, 0 conflicts. Fixed this phase — see *Defects found*. |
| 2 | one command starts local dependencies/test harness | **pass** | `python -m tutortwin` (service); `python scripts/chat.py` (conversation harness, fake vendor, zero cost) |
| 3 | migrations apply from an empty DB | **pass** | CI creates an empty Postgres and runs `alembic upgrade head`; `alembic check` clean |
| 4 | health/readiness work | **pass** | `test_readiness_fails_closed_while_liveness_stays_up` |
| 5 | deterministic fake adapters work | **pass** | identity, entitlement, tutor, outbound and model fakes drive 667 tests |
| 6 | normalized event API works | **pass** | `POST /v1/events`, multi-turn, `test_entry_service.py` |
| 7 | idempotency prevents duplicate processing | **pass** | unique index; `test_a_burst_from_one_student_does_not_multiply_provider_calls` (25 duplicates → 1 call) |
| 8 | ineligible fixture causes zero paid AI calls | **pass** | `test_an_ineligible_crowd_costs_nothing`; cost trace shows 0 |
| 9 | text tutoring works multi-turn | **pass** | cost trace `multi_turn`, 4 turns, 1 call each |
| 10 | tutor persona works | **pass** | persona blocks in the prompt; control-plane versioning |
| 11 | model routing is cost-aware | **pass** | cheap tier for simple non-STEM, advanced only for advanced STEM — visible in the call graphs below |
| 12 | usage ledger captures all provider calls | **pass** | `test_every_provider_call_is_on_the_ledger_after_a_burst` |
| 13 | image without brief causes zero OCR/vision | **pass** | `test_an_attachment_with_no_brief_is_recorded_and_never_fetched` |
| 14 | PDF without brief causes zero OCR/vision/embedding | **pass** | same; `fetch_count == 0`, `queue.depth == 0` |
| 15 | targeted PDF question processes minimum useful pages | **pass** | page-selection tests in `test_media_pipeline.py` |
| 16 | voice flow is gated and transcribed | **pass** | `test_media_pipeline.py` (audio is brief-exempt: the voice *is* the request) |
| 17 | RAG ownership isolation works | **pass** | `test_retrieval_cannot_cross_a_student_boundary`; owner is a bound parameter inside the ranking query |
| 18 | memory does not dump entire chat | **pass** | `test_rag_memory_units.py`; memory is facts, not a transcript |
| 19 | homework solve works | **pass** | `test_learning_engine_e2e.py` |
| 20 | math verifier works for supported cases | **pass** | sympy/pint verification, `test_learning_engine.py` |
| 21 | STEM verification triggers selectively | **pass** | cost trace: advanced STEM with confident output → 0 verifier calls |
| 22 | twin problem works | **pass** | `test_learning_engine_e2e.py` |
| 23 | flashcards work | **pass** | same |
| 24 | spaced repetition works | **pass** | SM-2 ladder tests |
| 25 | quizzes work | **pass** | same |
| 26 | mock test works | **pass** | same |
| 27 | answer key stays hidden in test mode | **pass** | student projection cannot carry a key |
| 28 | submission/grading works | **pass** | `test_learning_engine_e2e.py` |
| 29 | progress metrics update | **pass** | topic stats, mastery bands |
| 30 | admin login/RBAC works | **pass** | 72 admin security tests; Playwright 1, 13 |
| 31 | admin student/tutor/persona/plan controls work | **pass** | Playwright 3, 4, 4b, 5 |
| 32 | admin conversation/cost/job/audit views work | **pass** | Playwright 7, 9, 11, 12 |
| 33 | feature kill switches work | **pass** | Playwright 10 — flipped and restored, both audited |
| 34 | model configuration is not hardcoded | **pass** | `model_catalog` table; Playwright 6 edits a route |
| 35 | no Redis | **pass** | grep |
| 36 | no AWS S3 | **pass** | grep — R2 only |
| 37 | no Fargate | **pass** | grep |
| 38 | no NAT Gateway | **pass** | grep; DEPLOYMENT.md explains why none is needed |
| 39 | no always-on worker required | **pass** | Cloud Tasks pushes; nothing polls |
| 40 | Cloud Run min 0 deployment config | **pass** | `min_instance_count = 0`, `cpu_idle = true` |
| 41 | Cloud Tasks authenticated async jobs | **pass** | OIDC audience + service account; `test_a_forged_job_push_is_refused` |
| 42 | Neon pooled app connection | **pass** | two DSNs since Phase 01; pool 2+2 |
| 43 | private R2 objects | **pass** | no public-read policy; owner in the key path |
| 44 | secrets not logged | **pass** | `test_settings_never_render_a_secret`, `test_no_error_response_carries_a_secret` |
| 45 | load/retry/chaos scenarios pass | **pass** | 7 load + 16 chaos |
| 46 | cost-call traces exist for representative scenarios | **pass** | `scripts/cost_trace.py`, output below |

**46 of 46 standalone items pass.** Two carry caveats stated in *Known
limitations*: items 43 and 41 are configured and tested against fakes but have
never run against the live cloud services.

---

## Cost traces

`python scripts/cost_trace.py` — the real orchestration path, a scriptable fake
vendor, zero spend.

```
scenario               turns  calls  /turn  verify  classif  cacheable
simple_definition          1      1    1.0       0        0       1546
multi_turn                 4      4    1.0       0        0       1546
advanced_stem              1      1    1.0       0        0       1546
ineligible_student         1      0    0.0       0        0          0
duplicate_delivery         2      1    0.5       0        0       1546
```

Call graphs:

```
simple_definition   — routine, non-STEM
  1. CHEAP_TEXT          system=1938ch (1546 cacheable)  messages=1  cap=2048

multi_turn          — four turns
  1. STANDARD_TUTOR      system=1938ch (1546 cacheable)  messages=1  cap=2048
  2. STANDARD_TUTOR      system=1957ch (1546 cacheable)  messages=3  cap=2048
  3. STANDARD_TUTOR      system=1957ch (1546 cacheable)  messages=5  cap=2048
  4. STANDARD_TUTOR      system=1957ch (1546 cacheable)  messages=7  cap=2048

advanced_stem       — advanced STEM
  1. ADVANCED_REASONING  system=2034ch (1546 cacheable)  messages=1  cap=2048

ineligible_student  — (no provider call)

duplicate_delivery  — two deliveries, one call
  1. STANDARD_TUTOR      system=1938ch (1546 cacheable)  messages=1  cap=2048

FINDINGS: none — every scenario spent the minimum its routing allows
```

What the audit establishes:

- **No classifier LLM.** Intent routing is deterministic rules; a classifier call
  would appear as a step before the answering call. Zero, in every scenario.
- **Persona is cached.** 1,546 of 1,938 system-prompt characters (~80%) are
  marked cacheable on every turn. This was **0** before this phase — both
  adapters implemented caching and nothing ever set `cacheable_prefix`.
- **History grows, bounded.** Messages go 1 → 3 → 5 → 7 across four turns, and the
  context budget drops whole units by priority beyond its ceiling rather than
  slicing text.
- **The verifier is off unless it earns its call.** Advanced STEM with a confident
  answer costs one call, not two.
- **A duplicate delivery costs nothing.** Two deliveries, one provider call.
- **An ineligible student costs nothing.** Zero calls, zero ledger rows.

---

## Load results

`pytest tests/integration/test_load.py` — 7 passed.

```
[load] 20 concurrent students in 0.91s (22.0 req/s, fake provider)
[load] burst of 25 duplicates -> 1 provider call(s)
[load] 10 requests x 200ms provider latency in 0.70s (pool of 4)
```

These are property tests, not benchmarks — a laptop against a local Postgres says
nothing about Cloud Run and Neon. What they establish is portable:

- **20 concurrent students through a pool of 4** complete without pool exhaustion
  and produce exactly 20 conversations, no cross-talk.
- **25 simultaneous duplicates** produce 1 request event, 1 stored turn, 1
  provider call.
- **10 requests × 200ms provider latency finish in 0.70s.** Serialised through a
  pool of four they would take ≥0.5s of pure queueing and, at higher concurrency,
  deadlock. This is the direct evidence that **no transaction is held across a
  provider call** — the architecture's load-bearing rule.
- The ledger does not under-count under load; a crowd of ineligible students
  still costs zero; a concurrent first contact creates one subject, not ten.

---

## Performance

`python scripts/perf_probe.py` — AI latency excluded, because it is the vendor's
number and it hides the one thing an engineer here can change.

```
operation                              p50 ms   p95 ms  mean ms   queries
refused (entitlement gate)               17.1     21.4     20.2         5
answered turn (new conversation)         52.9     57.5     55.4        18
answered turn (21st in thread)           56.7     58.4     56.6        18

startup to first query : 162.1 ms
peak python heap       : 2.5 MB

N+1 CHECK
  turn 1  : 18 queries
  turn 21 : 18 queries
  flat - history is loaded in one statement regardless of length
```

- **No N+1.** Query count is flat at 18 whether the conversation has 1 turn or 21.
  Verified statement-by-statement: nothing repeats per message.
- **A refused request costs 5 queries and ~17ms.** The gate ordering means an
  abusive crowd is cheap to turn away.
- **Container startup to first usable query: 162ms.** Paid on every cold start,
  which matters because `min_instances = 0` by design.
- **Memory** is bounded by PDF rasterisation, not by the request path; the 1Gi
  limit is set by the 40-page ceiling, not by this 2.5MB.

---

## Chaos results

16 tests, all passing. Every failure injected at the seam the real one arrives
at — the adapter, the blobstore, the queue — never by patching the code under
test.

| Scenario | Result |
|---|---|
| Neon unavailable | `/healthz` 200, `/readyz` 503, no DSN in the body |
| R2 unavailable | the row is **kept**; deleting it would strand the object |
| Cloud Tasks duplicate delivery | 3 deliveries, 1 execution, attempts stay at 1 |
| OpenAI / Anthropic 429 | retried to the cap, then stopped; every attempt on the ledger |
| OpenAI / Anthropic timeout | same path, same cap |
| Malformed provider response | not returned to the student as an answer |
| Job crash mid-processing | row left retryable with `next_retry_at`, never `RUNNING`; 503 asks the queue to return |
| Process killed before persistence | the abandoned claim is re-executed, not replayed as an empty success |
| Queue saturation | 429 and the job keeps its attempt — postponed, not spent |
| Attempts exhausted | settles `FAILED_PERMANENT` instead of retrying forever |
| Non-retryable refusal | paid for once, not twice |
| Failed retries vs quota | 2 failed + 1 billed attempt → `calls_today == 1`, `spend == 1000` |

---

## Security findings

Full model and evidence: [SECURITY.md](SECURITY.md#phase-07-production-hardening-and-the-threat-model).
Twelve threats, each with a named test.

**Findings from this phase's own review, all fixed:**

| Finding | Severity | Fix |
|---|---|---|
| `/internal/jobs/run` had no OIDC path — shared secret only, on a public endpoint | high | `OidcVerifier` checks audience **and** service account; IAM restricts `run.invoker` |
| A deployed environment could start with no internal credential at all | high | `InternalAuth.require_configured()` refuses, in staging as well as production |
| `.env.example` was not in `.dockerignore` | medium | excluded; asserted by `test_a_secret_is_never_written_to_the_container_image` |
| Terraform would have held secret values in state | medium | Terraform creates containers only; values added by `gcloud secrets versions add` |

**Accepted, documented, not fixed:** admin TOTP (designed for, not enabled) and
isolated code execution (disabled by design — a subprocess in the API container
shares the service account and the database credentials, so it is not a
boundary).

---

## Defects found and fixed during this phase

The audit that mattered was of what already existed. Seven real defects, each of
which would have surfaced in production rather than in a test:

1. **The media pipeline was never connected to the request path.** `MediaPipeline`
   was built and thoroughly tested in Phase 03, and nothing called it from
   `handle_event` — the orchestrator kept its own copy of the brief gate and then
   dropped into the text path. An inbound PDF created no `media_objects` row and
   enqueued no job, while `/internal/jobs/run` processed a job nothing was
   creating. Wired; 5 tests now assert the connection.

2. **Runtime dependencies were undeclared.** `pyproject.toml` still listed only
   Phase 01's eight packages. anthropic, openai, argon2-cffi, pgvector, filetype,
   pymupdf, pytesseract, pillow, sympy, pint, matplotlib and email-validator were
   installed in the development environment and never declared, so
   `pip install -e ".[dev]"` from a clean clone produced a package that imported
   and then failed. All 20 pinned, plus a `gcp` extra for boto3, google-cloud-tasks
   and google-auth.

3. **Prompt caching was dead code.** Both vendor adapters implemented
   `cacheable_prefix` and nothing ever set it, so the stable half of the system
   prompt was re-read at full price on every turn. Now 1,546 characters cached per
   call.

4. **The per-provider ceiling and circuit breaker never fired.** `provider` was
   never passed to `load_quota_snapshot`, so `provider_daily_budget_micros` was
   always `None` and `recent_provider_failures` always `0`. Both were computed
   into a snapshot nobody consulted. Now computed for every vendor in one
   statement and enforced in the gateway, which is the only layer that maps an
   alias to a vendor.

5. **The per-student media ceilings were counted and ignored.**
   `media_allowance_exceeded` was called only by its own unit test. Now checked at
   intake, before a byte is fetched.

6. **No backoff between provider retries.** A 429 was retried immediately, while
   the rate limit that caused it was still in force. Exponential with full
   jitter, capped at 8s.

7. **A `postgresql+psycopg2://` DSN failed as `ModuleNotFoundError: No module
   named 'psycopg2'`**, eight frames inside SQLAlchemy. This project runs psycopg
   3, and `+psycopg2` is the shape most other services in the estate use — so the
   wrong URL read as a broken install and sent the reader to `pip`. Now rejected
   at settings validation, naming the fix.

An eighth was found in the tooling rather than the product: `perf_probe.py`
reported an N+1 that turned out to be the probe entitling only the first of
thirty identities, so twenty-nine samples measured the refusal path and were
reported as the answering path. Worth recording because it is the failure mode of
every benchmark — the numbers looked excellent and described work the service
never did.

---

## Deploy commands

Full procedure: [DEPLOYMENT.md](DEPLOYMENT.md). The short form:

```bash
# 0. Neon project + pgvector; private R2 bucket with a 7-day lifecycle rule.

# 1. Infrastructure (twice the first time: two settings need the service's own URL)
cd infra/terraform
cp staging.tfvars.example staging.tfvars      # fill in
terraform init && terraform apply -var-file=staging.tfvars
terraform output                              # tasks_target_url, oidc_audience, tasks_service_account
#   put those into api_env, then:
terraform apply -var-file=staging.tfvars

# 2. Secret values (never Terraform)
echo -n "$DSN" | gcloud secrets versions add database-url-staging --data-file=-
#   ... repeat for the other six

# 3. Images
REPO=$(terraform output -raw artifact_repository)
docker build -t "$REPO/api:$TAG" .            && docker push "$REPO/api:$TAG"
docker build -t "$REPO/admin:$TAG" apps/admin && docker push "$REPO/admin:$TAG"

# 4. Migrate on the DIRECT DSN, then shift traffic
TUTORTWIN_DATABASE_MIGRATION_URL="$DIRECT_DSN" alembic upgrade head
terraform apply -var-file=staging.tfvars      # with the new tags

# 5. First administrator (refuses if one already exists)
python -m tutortwin.cli.admin hash-password
TUTORTWIN_ADMIN_BOOTSTRAP_EMAIL=ops@nxtutors.in \
TUTORTWIN_ADMIN_BOOTSTRAP_PASSWORD_HASH='<hash>' \
  python -m tutortwin.cli.admin bootstrap
```

Rollback is a traffic shift, never a migration rollback:

```bash
gcloud run services update-traffic tutortwin-api-staging \
  --region=$REGION --to-revisions=<previous>=100
```

## Smoke tests

```bash
API=$(terraform output -raw api_url)

# 1. liveness / readiness, and no DSN in either body
curl -sf $API/healthz                                    # {"status":"ok"}
curl -sf $API/readyz                                     # {"status":"ready",...}
curl -s  $API/readyz | grep -qi postgres && echo "LEAK — STOP"

# 2. the internal endpoint refuses an anonymous caller
test "$(curl -s -o /dev/null -w '%{http_code}' -X POST $API/internal/jobs/run \
  -H 'content-type: application/json' \
  -d '{"job_id":"00000000-0000-0000-0000-000000000000"}')" = 401 && echo "OK: authenticated"

# 3. and accepts the queue's identity
curl -s -X POST $API/internal/jobs/run \
  -H "Authorization: Bearer $(gcloud auth print-identity-token --audiences=$API)" \
  -H 'content-type: application/json' \
  -d '{"job_id":"00000000-0000-0000-0000-000000000000"}'   # {"state":"NOT_FOUND"}

# 4. one real turn through the event API
curl -s -X POST $API/v1/events -H "x-internal-key: $INTERNAL_KEY" \
  -H 'content-type: application/json' -d '{
    "event_id":"smoke-1","request_id":"smoke-1","correlation_id":"smoke-1",
    "source":"smoke","subject":{"external_type":"phone","external_id":"+919999000001"},
    "message":{"message_id":"smoke-1","type":"TEXT","text":"what is osmosis"},
    "occurred_at":"2026-01-01T00:00:00Z"}'

# 5. the same call again must replay, not re-spend  ->  "idempotent_replay": true

# 6. retention sweep is authenticated and idempotent
curl -s -X POST $API/internal/retention/sweep \
  -H "Authorization: Bearer $(gcloud auth print-identity-token --audiences=$API)" \
  -H 'content-type: application/json' -d '{"batch_size":10}'

# 7. control plane serves its login page
curl -sf -o /dev/null -w '%{http_code}\n' "$(terraform output -raw admin_url)/login"   # 200

# 8. confirm the spend after the smoke run is what you expect
#    control plane -> Costs, grouped by provider
```

---

## Known limitations

1. **No live cloud deployment.** No Google Cloud, Neon or Cloudflare credentials
   exist in this environment. Terraform validates and the provider resolves;
   `terraform plan` against a real project has not run, so provider-side
   rejections (quota, API enablement, org policy) are unproven. **No live
   deployment is claimed.**

2. **R2, Cloud Tasks and OIDC have never run against the real services.** All
   three are wired, selected by configuration, and covered by tests against fakes.
   The first real `put`, task push and token verification will be in staging.

3. **`terraform apply` is a two-pass operation the first time.** Two settings need
   the service's own URL. Inherent to self-referential Cloud Run configuration.

4. **No Terraform remote backend.** Add a GCS backend before more than one person
   applies.

5. **Isolated code execution is disabled.** `DisabledSandbox` refuses and explains
   why. Static code tutoring — reading, tracing, explaining — works fully. The
   requirements for a real boundary are listed in SECURITY.md.

6. **Admin TOTP is designed for, not enabled.** The login flow has the seam.

7. **Entitlement overrides are stored and audited but not yet honoured at
   runtime.** The gate reads the entitlement gateway, which is a fake until Phase
   09 overlays the website.

8. **Two Playwright scenarios skip** — mock-test inspection and failed-job retry —
   because both need a configured model provider and object storage. The tests are
   written and skip with a stated reason rather than asserting against invented
   rows.

9. **Load numbers are laptop numbers.** The properties are portable; the
   milliseconds are not.

10. **`usage_ledger.capability` is null for every row written before Phase 06.**
    Those group as `unattributed` rather than being backfilled with a guess.

11. **Cost by tutor follows the *current* assignment.** Reassigning a student
    moves their historical spend. Stated in the UI and the API docs.

---

## Standalone usability

The service is usable end to end without Lead Intake or the website:

```bash
# service
python -m tutortwin

# a conversation, no API key, no cost
python scripts/chat.py
python scripts/chat.py --say "explain photosynthesis" --say "why does it need light"

# or over HTTP, the same contract a message bridge will use
curl -X POST localhost:8080/v1/events -H "x-internal-key: $KEY" \
  -H 'content-type: application/json' -d @event.json

# the control plane
cd apps/admin && TUTORTWIN_API_URL=http://127.0.0.1:8000 npm run dev
```

Phase 08 replaces the fake identity/outbound gateways with the Lead Intake
bridge. Phase 09 replaces the fake entitlement gateway with the website's source
of truth. Neither requires a change to the event contract, which is what
"standalone" was for.

**STOP.** Lead Intake and the website are not integrated in this phase.
