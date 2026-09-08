# TutorTwin Testing

## Running

```bash
# Unit only - no database needed
pytest -m "not integration"

# Everything (needs PostgreSQL)
TUTORTWIN_DATABASE_MIGRATION_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:5432/tutortwin_test \
  alembic upgrade head
pytest

# With coverage
pytest --cov --cov-report=term-missing
```

## Current state

| Metric | Value |
|---|---|
| Tests | 57 passing |
| Unit (no DB) | 29 |
| Integration (real Postgres) | 28 |
| Coverage | 94% |
| Runtime | ~15s full, ~1s unit-only |

## Approach

**Real database, real migrations.** Integration tests run against live
PostgreSQL, and `test_migrations.py` builds the schema with actual Alembic
commands on a throwaway database. `create_all` is never used, so schema drift
between models and migrations is caught by the tests rather than in production.

**Fakes are production code.** The adapters in `providers/fakes.py` ship in
`src/`, not `tests/`. Phases 01-07 run the real product on them. They are
deterministic: `uuid5` from a fixed namespace means the same external id always
maps to the same UUID across runs and processes.

**Tripwires over stubs.** `ForbiddenLLMProvider` raises when called instead of
returning canned text, so "zero paid calls" is proved by the test failing loudly
if the cost gate ever breaks.

**Truncate, don't drop.** The `session` fixture truncates tables between tests,
so the schema under test stays the migrated one.

## Mandatory proofs (Prompt 01)

| # | Requirement | Test |
|---|---|---|
| 1 | Migrations work on empty DB | `test_migrations_apply_to_empty_database` |
| 2 | Valid event creates conversation | `test_valid_event_creates_conversation_and_messages` |
| 3 | Duplicate returns idempotent result | `test_duplicate_event_returns_idempotent_result` |
| 4 | Non-Pro causes zero provider calls | `test_non_pro_student_makes_zero_provider_calls` |
| 5 | Pro reaches orchestration | `test_pro_student_reaches_orchestration` |
| 6 | Wrong owner cannot read conversation | `test_wrong_owner_cannot_read_conversation` |
| 7 | DB error becomes controlled error | `test_database_error_becomes_controlled_error` |
| 8 | Request IDs propagate | `test_request_id_propagates_from_caller` |
| 9 | Secrets absent from logs | `test_logging_redaction.py` (9 tests) |
| 10 | Downgrade/upgrade round-trip | `test_migrations_downgrade_and_reapply` |

## Adversarial coverage

Empty/malformed input, unknown fields, overlong text, duplicate events,
concurrent duplicate events, wrong owner, unresolved identity, oversized body,
missing/wrong credentials, database unreachable, media without brief,
cross-source id collision.

`test_concurrent_duplicates_execute_work_once` races two identical events
through separate sessions and asserts exactly one conversation and two messages
result - the idempotency guarantee under real concurrency.

## Layout

```
tests/
  conftest.py                     engine/session fixtures, loop policy
  unit/
    test_events_contract.py       contract + brief gate (20)
    test_logging_redaction.py     secret + PII redaction (9)
  integration/
    test_entry_service.py         orchestration slice (10)
    test_api.py                   HTTP surface (11)
    test_failure_paths.py         adversarial (4)
    test_migrations.py            migrations (3)
```

## Gaps

No load or chaos testing yet (Phase 07). No provider-failure simulation, since
Phase 01 has no providers.

---

# Phase 06: the control plane suites

Three layers, run from `apps/admin`:

```bash
npm run verify   # tsc --noEmit, eslint, vitest (40 component/unit tests), next build
npm run e2e      # Playwright, 17 scenarios
```

`npm run verify` is the gate. **A phase is not complete on a failing production
build.**

## End-to-end runs against the real stack

Nothing is mocked: the real Next.js server, the real Python API, a real
PostgreSQL. A control-plane test that stubbed the API would prove the buttons
render, not that an operator can change a plan - and the second is the claim
being made.

Playwright starts only the Next.js server. Starting the Python service from the
harness would hide a configuration failure behind a test fixture.

## Use a separate database

`tests/conftest.py` truncates every table in `tutortwin_test` before each test.
Point the end-to-end API at its own database - `tutortwin_e2e` - or a `pytest`
run will silently erase the fixtures mid-suite:

```bash
TUTORTWIN_DATABASE_MIGRATION_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:5432/tutortwin_e2e   alembic upgrade head
```

`ADMIN_BASE_URL` and `PORT` let the suite run beside a development server rather
than reusing it and testing the wrong API.

## Fixtures come from the API

`e2e/seed.ts` creates its administrator, tutor, plan, entitlement and
conversation **through the real endpoints**, including walking the support
operator's forced first-login password change. A seeder that wrote to Postgres
directly would still pass if every endpoint were broken.

A student exists because a message arrived, so the seeder posts real events to
`POST /v1/events`. There is deliberately no admin endpoint that invents one.

## Scenarios that skip, and why

`8. mock test inspection` and `11. failed job retry` need a configured model
provider and object storage respectively. They call `test.skip` with a stated
reason rather than asserting against rows inserted to make a tick appear.


---

# Phase 07: load, chaos, threat model and CI

## The suites

| File | Count | Asks |
|---|---|---|
| `tests/integration/test_load.py` | 7 | does it hold up under concurrency |
| `tests/integration/test_chaos.py` | 16 | does it fail in a way we can recover from |
| `tests/integration/test_threat_model.py` | 15 | is each threat actually mitigated |
| `tests/integration/test_media_wiring.py` | 5 | does an attachment reach the pipeline |
| `tests/unit/test_cost_ceilings.py` | 13 | do the ceilings fire, and in the right order |

Totals: **667 Python tests**, 80% line coverage, plus 40 control-plane unit tests
and 17 Playwright scenarios.

## Load tests are not benchmarks

A wall-clock number measured on a laptop against a local Postgres says nothing
about Cloud Run and Neon. What `test_load.py` asserts is the part that *is*
portable — the invariants that must survive concurrency:

- the connection pool is never asked for more than it has
- concurrent duplicates do the work once
- a burst does not multiply provider calls
- deduplication does not swallow genuinely different messages
- a slow provider does not hold a database transaction open
- the ledger does not under-count under load
- a crowd of ineligible students still costs zero
- a concurrent first contact creates one subject, not ten

The numbers it prints are recorded in the acceptance report, not asserted on.

The load-bearing one is **`test_a_slow_provider_does_not_hold_a_database_transaction`**:
ten concurrent requests against a provider taking 200ms each, through a pool of
four. If any transaction were held across that call they would serialise; with
more concurrency they would deadlock. Measured: 0.70s total, which is overlap,
not queueing.

## Chaos: injected at the seam, never by patching

Every failure is injected where the real one would arrive — the adapter, the
blobstore, the queue — never by monkey-patching the code under test. A test that
patches its subject proves only that the patch works.

| Scenario | Covered by |
|---|---|
| Neon unavailable | `test_readiness_fails_closed_while_liveness_stays_up` |
| R2 unavailable | `test_retention_leaves_the_row_when_the_object_store_is_down` |
| Cloud Tasks duplicate delivery | `test_duplicate_task_delivery_runs_the_job_once` |
| OpenAI / Anthropic 429 | `test_a_failing_vendor_is_retried_within_the_attempt_cap[rate_limit]` |
| OpenAI / Anthropic timeout | `test_a_failing_vendor_is_retried_within_the_attempt_cap[timeout]` |
| Malformed provider response | `test_a_malformed_provider_response_is_not_returned_as_an_answer` |
| Job crash mid-processing | `test_a_job_crash_leaves_the_row_retryable` |
| Process killed before persistence | `test_a_process_killed_before_persistence_can_be_replayed` |
| Queue saturation | `test_heavy_job_concurrency_sheds_load_instead_of_piling_on` |
| Attempts exhausted | `test_an_exhausted_job_settles_instead_of_retrying_forever` |

The vendor scenarios are parameterised over 429 and timeout rather than duplicated
per vendor: the gateway branches on `ErrorCategory`, not on which vendor produced
it, so a per-vendor copy would test the parametrisation and not the policy.

## Running them

```bash
pytest                                  # everything (needs PostgreSQL)
pytest -m "not integration"             # unit only, no database
pytest tests/integration/test_chaos.py -q
pytest tests/integration/test_load.py -s | grep '\[load\]'   # prints its numbers
```

## Diagnostics, not tests

Two scripts that answer questions a test cannot phrase as pass/fail. Both exit
non-zero on a regression, so either can be promoted into CI.

```bash
python scripts/cost_trace.py    # call graph + spend shape per scenario
python scripts/perf_probe.py    # p50/p95, query counts, N+1 check, startup, heap
```

`perf_probe.py` found a bug in **itself** on first run — it reported an N+1 that
turned out to be the probe entitling only the first of thirty identities, so
twenty-nine samples were measuring the refusal path and being reported as the
answering path. Worth stating because it is the failure mode of every benchmark:
the numbers looked excellent and described work the service never did.

## Databases

The integration suite truncates every table in `tutortwin_test` **before each
test**. Point the end-to-end control-plane suite at a *different* database, or a
`pytest` run will erase its fixtures mid-suite:

```bash
createdb tutortwin_e2e
TUTORTWIN_DATABASE_MIGRATION_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:5432/tutortwin_e2e \
  alembic upgrade head
```

## CI

`.github/workflows/ci.yml`. Five jobs; the image build gates on all of them,
because an image that exists is an image somebody can deploy.

```
api     ruff check, ruff format --check, mypy, alembic upgrade (empty DB),
        alembic check (model drift), pytest with coverage floor, pip-audit
admin   tsc, eslint, vitest, next build, npm audit
e2e     migrate, bootstrap an admin, start the API, seed, Playwright   [needs: api, admin]
infra   terraform fmt -check, terraform validate                        (no credentials)
images  docker build for both services                    [needs: api, admin, e2e, infra]
```

Two steps are worth their runtime:

- **`alembic upgrade head` against a database created empty seconds earlier** is
  the acceptance item "migrations apply from an empty DB", proved rather than
  asserted.
- **`alembic check`** catches a model changed without a migration — which passes
  every test on a developer's already-migrated database and fails on the first
  deploy to a fresh one.

Audits (`pip-audit`, `npm audit`) are advisory. A new CVE in a transitive
dependency should be visible on the next run, not block a release that has
nothing to do with it. Promote to blocking once the baseline is clean and stays
clean.
