# Phase 07 Acceptance — Serverless Hardening, Cost Engineering, Security, Autonomous QA

Standalone. **No Lead Intake, no NX website, no MySQL.** Phases 08 and 09 own
those integrations.

The full standalone sign-off — architecture, the 46-item acceptance matrix, cost
traces, load and chaos results, deploy and smoke commands — is
[FINAL_STANDALONE_ACCEPTANCE.md](../FINAL_STANDALONE_ACCEPTANCE.md). This report
covers what *this phase* changed and why.

---

## Environment

| Component | Version |
|---|---|
| OS | Windows 11 (win32) |
| Python | 3.12.10 |
| PostgreSQL | 18.1 (local), pgvector |
| Node | 20.19.4 |
| Terraform | 1.15.2 |
| uv | 0.11.28 |

## Verification

```
$ ruff check .                        All checks passed!
$ ruff format --check .               167 files already formatted
$ mypy src                            Success: no issues found in 102 source files
$ alembic upgrade head                7 revisions applied to an empty database
$ alembic check                       No new upgrade operations detected.
$ pytest --cov=tutortwin              667 passed — 80% coverage
$ (apps/admin) npm run verify         tsc, eslint, 40 tests, next build — all clean
$ (apps/admin) npx playwright test    15 passed, 2 skipped
$ terraform fmt -check -recursive     exit 0
$ terraform validate                  Success! The configuration is valid.
$ uv pip install --dry-run -e ".[dev,gcp]"   Resolved, 0 conflicts
```

Phase 07 added **56 tests**: 16 chaos, 15 threat-model, 13 cost-ceiling, 7 load,
5 media-wiring.

---

## Infrastructure constraints

Every forbidden item verified absent by grep across `src/`, `infra/`,
`apps/admin/src/`, `.github/`, `pyproject.toml` and `Dockerfile`. The only
matches are a source comment stating there is no Celery or Redis, and the
Terraform provider binary (which contains every GCP service name by nature).

No NAT Gateway · no AWS S3 · no Redis · no Fargate/ECS · no Kubernetes · no EC2 ·
no polling worker · no RabbitMQ/Kafka · no Celery.

**Why no VPC:** every dependency — Neon, R2, OpenAI, Anthropic — is a public TLS
endpoint. A connector would add a fixed hourly charge and a NAT gateway to a
design whose point is costing nothing while idle, and would buy no isolation that
authentication does not already provide.

---

## What was built

### Deployment

| Piece | Detail |
|---|---|
| Cloud Run — API | min 0, max 10, 1 vCPU / 1Gi, concurrency 80, `cpu_idle` (request-based billing), 600s timeout |
| Cloud Run — control plane | **separate service and image**, min 0, max 3, 512Mi |
| Probes | startup on `/readyz` (gates traffic on the database), liveness on `/healthz` (touches nothing) |
| Shutdown | SIGTERM drains in-flight requests, bounded by `shutdown_grace_seconds` |
| Cloud Tasks | OIDC push, 3 attempts, 30s→600s backoff, `max_concurrent_dispatches` as the real load ceiling |
| Cloud Scheduler | nightly retention sweep, same OIDC identity |
| Terraform | 562 lines, 3 files, `validate` clean; creates secret *containers*, never values |
| Images | API installs `.[gcp]` and Tesseract; control plane uses Next standalone output |

`max_instances` is documented as a **budget control**: it bounds concurrent
Postgres connections (`4 × max_instances`) and concurrent provider spend at once.

### Environment profiles

`local | test | staging | production`. No business logic branches on the
environment name. Which adapter is used is decided once, in `dependencies.py`,
from whether the credentials exist:

| Configured | Storage | Queue |
|---|---|---|
| no | filesystem | recorder (dispatches nothing) |
| yes | Cloudflare R2 | Cloud Tasks + OIDC |

`require_deployable()` runs before the port opens in staging and production and
refuses four mis-wirings that would otherwise fail **silently**: no internal key
(anonymous `/v1/events`), no R2 (media to a disk that disappears), no Cloud Tasks
(jobs recorded, never dispatched), no migration DSN (DDL through a pooled
endpoint).

### Security

- **OIDC on `/internal/*`**, checking the token's audience *and* its service
  account. Audience alone accepts any Google-signed token for the URL.
- `/v1/events` keeps the shared secret via a **separate method**, so enabling
  OIDC for the queue can never silently stop authenticating the event ingress.
- Twelve-threat model, each row naming the test that fails if the mitigation is
  removed.
- Code execution remains disabled, and the requirements for a real boundary are
  written down rather than approximated with a subprocess.

### Cost controls

Three levels, all reachable at runtime — see
[COST_CONTROLS.md](../COST_CONTROLS.md#phase-07-enforceable-ceilings) for the
precedence order and the reasoning.

- **System:** hourly spend velocity (checked *before* the daily total, because it
  is the one that catches a runaway while it runs), daily total, per-vendor
  daily, per-vendor failure circuit, fleet-wide heavy-job concurrency.
- **Student:** monthly and daily spend, daily calls, and PDF/OCR/voice/mock
  allowances checked at intake before a byte is fetched.
- **Retry accounting:** every attempt is on the ledger; only attempts that
  reached a working provider consume the student's call quota.

### Testing

7 load, 16 chaos, 15 threat-model, 13 cost-ceiling, 5 media-wiring. Every chaos
failure is injected at the seam the real one arrives at — never by patching the
code under test.

Two diagnostics that exit non-zero on regression:
`scripts/cost_trace.py` (call graphs, spend shape) and `scripts/perf_probe.py`
(p50/p95, query counts, N+1, startup, heap).

### CI

Five jobs; the image build gates on all of them, because an image that exists is
an image somebody can deploy. Two steps earn their runtime: `alembic upgrade head`
against a database created empty seconds earlier, and `alembic check` for model
drift that passes on a developer's already-migrated database and fails on the
first fresh deploy.

---

## Defects found and fixed

The work that mattered most was auditing what already existed. Each of these
would have surfaced in production, not in a test.

| # | Defect | Impact |
|---|---|---|
| 1 | **The media pipeline was never connected to the request path.** Built and tested in Phase 03; nothing called it from `handle_event`. An inbound PDF created no row and enqueued no job, while `/internal/jobs/run` processed a job nothing was creating. | Media was non-functional end to end |
| 2 | **Runtime dependencies were undeclared.** `pyproject.toml` still listed Phase 01's eight packages; twelve more were installed locally and never declared. | `pip install` from a clean clone produced a package that imported and then failed |
| 3 | **Prompt caching was dead code.** Both adapters implemented `cacheable_prefix`; nothing set it. | The stable half of every system prompt re-read at full price on every turn |
| 4 | **Per-provider ceiling and circuit breaker never fired.** `provider` was never passed to `load_quota_snapshot`. | Two cost controls computed into a snapshot nobody consulted |
| 5 | **Per-student media ceilings were counted and ignored.** `media_allowance_exceeded` was called only by its own unit test. | Four more controls that reported instead of controlling |
| 6 | **No backoff between provider retries.** A 429 was retried immediately, while the rate limit was still in force. | Paying for rejections during a vendor incident |
| 7 | **A `psycopg2` DSN failed as `ModuleNotFoundError`** eight frames inside SQLAlchemy. This project runs psycopg 3. | A wrong URL read as a broken install and sent the reader to `pip` |

An eighth was in the tooling: `perf_probe.py` reported an N+1 that turned out to
be the probe entitling only the first of thirty identities, so twenty-nine
samples measured the refusal path and were reported as the answering path. Fixed,
and recorded because it is the failure mode of every benchmark — the numbers
looked excellent and described work the service never did.

---

## Measured results

**Cost shape** (`scripts/cost_trace.py`, fake vendor, zero spend):

| Scenario | Turns | Calls | Verifier | Classifier | Cacheable |
|---|---|---|---|---|---|
| simple_definition | 1 | 1 | 0 | 0 | 1,546 ch |
| multi_turn | 4 | 4 | 0 | 0 | 1,546 ch |
| advanced_stem | 1 | 1 | 0 | 0 | 1,546 ch |
| ineligible_student | 1 | **0** | 0 | 0 | — |
| duplicate_delivery | 2 | **1** | 0 | 0 | 1,546 ch |

`FINDINGS: none — every scenario spent the minimum its routing allows`

**Load** (property tests, not benchmarks):

```
20 concurrent students in 0.91s (22.0 req/s)      — pool of 4, no exhaustion
burst of 25 duplicates -> 1 provider call
10 requests x 200ms provider latency in 0.70s     — proves no transaction is
                                                    held across a provider call
```

**Performance** (AI latency excluded):

```
refused (entitlement gate)     p50 17.1ms  p95 21.4ms   5 queries
answered turn (new)            p50 52.9ms  p95 57.5ms  18 queries
answered turn (21st in thread) p50 56.7ms  p95 58.4ms  18 queries
startup to first query 162.1ms · peak heap 2.5MB
N+1: flat at 18 queries whether the thread is 1 turn or 21
```

---

## Known limitations

1. **No live cloud deployment, and none is claimed.** No GCP, Neon or Cloudflare
   credentials exist here. Terraform validates; `plan` against a real project has
   not run, so provider-side rejections are unproven.
2. **R2, Cloud Tasks and OIDC have never run against the live services.** Wired,
   configuration-selected, covered by fakes.
3. **First `terraform apply` is a two-pass operation** — two settings need the
   service's own URL.
4. **No Terraform remote backend.** Add GCS before a second person applies.
5. **Isolated code execution stays disabled.** Static code tutoring is unaffected.
6. **Admin TOTP designed for, not enabled.**
7. **Two Playwright scenarios skip** without a model provider and object storage.
8. **Load numbers are laptop numbers.** The properties are portable; the
   milliseconds are not.

---

## Isolation confirmed

| Claim | How |
|---|---|
| No Lead Intake | Not read, not imported, not referenced anywhere in the repository |
| No NX website | Same |
| No MySQL | Every DSN in the project is `postgresql+psycopg://` |
| No browser → database | The control plane's CSP sets `connect-src 'self'`; the only client of the API is the Next.js server |

**STOP.** Lead Intake and the website are not integrated in this phase.
