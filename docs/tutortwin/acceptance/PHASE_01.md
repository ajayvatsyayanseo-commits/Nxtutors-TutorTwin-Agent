# Phase 01 Acceptance Report

Standalone TutorTwin foundation. All commands below were executed; output is
reproduced verbatim.

## Environment

| Component | Version |
|---|---|
| OS | Windows 11 (win32) |
| Python | 3.12.10 |
| PostgreSQL | 18.1 (local, `127.0.0.1:5432`) |
| Package manager | uv 0.11.28 |
| Docker | not available - image not built, see Limitations |
| pgvector | not installed - not required until Phase 04 |

Package versions were checked against PyPI at build time and pinned exactly in
`pyproject.toml`: FastAPI 0.141.1, Pydantic 2.13.5, SQLAlchemy 2.0.52,
Alembic 1.19.1, psycopg 3.3.4, structlog 26.1.0, uvicorn 0.52.4, pytest 9.1.1,
ruff 0.16.5, mypy 2.3.1.

## Setup

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
createdb tutortwin && createdb tutortwin_test
```

## 1. Migrations from empty database

```bash
$ TUTORTWIN_DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:5432/tutortwin \
    alembic upgrade head
INFO  [alembic.runtime.migration] Running upgrade  -> f80deb6c45fc, phase 01 foundation
```

Tables created (16 + `alembic_version`):

```
alembic_version        outbound_actions
audit_events           plan_policies
conversations          request_events
entitlements           request_states
feature_flags          tutor_assignments
idempotency_keys       tutor_persona_versions
messages               tutors
model_catalog          tutortwin_subjects
                       usage_ledger
```

## 2. Downgrade / upgrade round-trip

```bash
$ alembic downgrade base
INFO  [alembic.runtime.migration] Running downgrade f80deb6c45fc -> , phase 01 foundation
# remaining public tables: 1  (alembic_version only)

$ alembic upgrade head
INFO  [alembic.runtime.migration] Running upgrade  -> f80deb6c45fc, phase 01 foundation
# public tables: 17
```

Downgrade is supported and verified.

## 3. Static checks

```bash
$ ruff check src tests migrations
All checks passed!

$ ruff format --check src tests migrations
46 files already formatted

$ mypy
Success: no issues found in 34 source files
```

mypy runs in `strict` mode.

## 4. Tests

```bash
$ pytest -m "not integration"
29 passed, 28 deselected in 0.92s

$ pytest
57 passed in 10.72s

$ pytest --cov --cov-report=term
TOTAL    946    48    58    9    94%
57 passed
```

### Mandatory proofs

| # | Requirement | Test | Result |
|---|---|---|---|
| 1 | Empty DB migrations work | `test_migrations_apply_to_empty_database` | PASS |
| 2 | Valid event creates conversation | `test_valid_event_creates_conversation_and_messages` | PASS |
| 3 | Duplicate returns idempotent result | `test_duplicate_event_returns_idempotent_result` | PASS |
| 4 | Non-Pro causes zero provider calls | `test_non_pro_student_makes_zero_provider_calls` | PASS |
| 5 | Pro reaches orchestration | `test_pro_student_reaches_orchestration` | PASS |
| 6 | Wrong owner cannot read conversation | `test_wrong_owner_cannot_read_conversation` | PASS |
| 7 | DB error becomes controlled error | `test_database_error_becomes_controlled_error` | PASS |
| 8 | Request IDs propagate | `test_request_id_propagates_from_caller` | PASS |
| 9 | Secrets absent from logs | `test_logging_redaction.py` (9 tests) | PASS |
| 10 | Downgrade/upgrade supported | `test_migrations_downgrade_and_reapply` | PASS |

## 5. Live scenario trace

Service started with `python -m tutortwin` against the real database.

```bash
$ curl -s http://127.0.0.1:8101/healthz
{"status":"ok"}

$ curl -s http://127.0.0.1:8101/readyz
{"status":"ready","checks":{"database":"ok"}}
```

**Scenario 1 - FREE student (cost gate)**
```json
{"conversation_id":null,"status":"REJECTED",
 "outbound_actions":[{"type":"SHOW_UPGRADE","text":"AI Tutor is available on the Pro plan."}],
 "usage":{"paid_model_calls":0},"idempotent_replay":false}
```

**Scenario 2 - unauthenticated request** → `status=401`

**Scenario 3 - PRO student**
```json
{"conversation_id":"455aa492-0897-416a-932b-34d0e875f0bb","status":"COMPLETED",
 "outbound_actions":[{"type":"SEND_TEXT",
   "text":"TutorTwin - AI Assistant for Anita Sharma received your message (27 characters)."}],
 "usage":{"paid_model_calls":0},"idempotent_replay":false}
```

Identity is `TutorTwin - AI Assistant for <Tutor>` - the human tutor is never
impersonated.

**Scenario 4 - duplicate of scenario 3** → same `conversation_id`,
`"idempotent_replay":true`, no new work.

**Scenario 5 - PDF with no brief**
```json
{"status":"COMPLETED","outbound_actions":[{"type":"ASK_FILE_BRIEF",
 "text":"I can see your attachment. Tell me what you would like me to do with it - ..."}],
 "usage":{"paid_model_calls":0}}
```

**Scenario 6 - PDF with brief "solve question 4"** → `SEND_TEXT`, same
conversation reused.

### Persisted state after the run

```
conversations=1     messages=6          request_events=5
idempotency_keys=5  outbound_actions=3  usage_ledger=0
```

```
request_states:
  REJECTED  ENTITLEMENT_INACTIVE
  REJECTED  ENTITLEMENT_INACTIVE
  COMPLETED -
  COMPLETED -
  COMPLETED -
```

`usage_ledger=0` is the cost evidence: **zero paid provider calls across every
scenario.**

Media stored as reference only, no bytes:
```json
{"filename":"homework.pdf","media_id":"media_abc","provider":"test",
 "size_hint":91234,"mime_type_hint":"application/pdf"}
```

### Log safety

```bash
$ grep -c "Explain quadratic\|solve question 4" server.log
0        # student content NOT logged
$ grep -c "local-dev-key" server.log
0        # internal secret NOT logged
```

Sample line - correlation IDs present, content absent:
```json
{"conversation_id":"455aa492-...","message_type":"PDF","paid_model_calls":0,
 "event":"event_completed","request_id":"req_f0b83a...","correlation_id":"corr_1a5212...",
 "event_id":"evt_p2","level":"info","timestamp":"2026-08-31T04:11:32.719632Z"}
```

## 6. Static cleanup scan

```bash
$ grep -rnE "TODO|FIXME|NotImplementedError|console\.log|shell=True|eval\(|verify=False|allow_origins=\[\"\*\"\]|pickle\.loads" src tests migrations
# no matches

$ grep -rnE "sk-[A-Za-z0-9]|gpt-4|claude-3|claude-opus|api_key *= *\"" src
# no matches - no hardcoded secrets or vendor model IDs
```

The single `pass` in `src/` is the SQLAlchemy `Base` declarative class body.

## 7. Cost accounting

| Metric | Count |
|---|---|
| OpenAI calls | 0 |
| Anthropic calls | 0 |
| Embedding calls | 0 |
| Transcription calls | 0 |
| Vision calls | 0 |
| OCR calls | 0 |
| Pages processed | 0 |
| Estimated cost | $0.00 |

Structurally enforced: entitlement precedes any capability, and
`ForbiddenLLMProvider` / `ForbiddenEmbeddingProvider` raise on any call.

## Changed files

New repository, 89 files (46 Python). Principal modules:

```
pyproject.toml  alembic.ini  Dockerfile  README.md  .env.example  .gitignore
src/tutortwin/
  config.py  runtime.py  __main__.py
  api/        app.py  dependencies.py  middleware.py  routes/{events,health}.py
  domain/     events.py  models.py  ports.py  errors.py
  db/         models.py  engine.py
  repositories/conversations.py
  orchestration/entry_service.py
  capabilities/placeholder.py
  providers/  fakes.py
  security/   auth.py
  observability/logging.py
migrations/  env.py  versions/f80deb6c45fc_phase_01_foundation.py
tests/       conftest.py  unit/(2)  integration/(4)
docs/tutortwin/  ARCHITECTURE  DATA_MODEL  API  EVENT_MODEL  SECURITY  TESTING
                 IMPLEMENTATION_STATUS  acceptance/PHASE_01
```

`git diff --stat` is not available: the workspace is not a git repository. The
file inventory above stands in its place.

## Isolation compliance

The Lead Intake repository, the NX Tutors website and production MySQL were not
cloned, opened, inspected or connected. No code was imported from either system.
A `leadintakedb` database exists on the same local PostgreSQL server and was
deliberately left untouched; TutorTwin uses its own `tutortwin` /
`tutortwin_test` databases.

## Limitations

1. **Docker image not built.** Docker is unavailable in this environment. The
   Dockerfile is written (non-root, single worker, `python -m tutortwin`) but
   unverified. Smoke command:
   `docker build -t tutortwin-api . && docker run -p 8080:8080 --env-file .env tutortwin-api`
2. **pgvector absent.** Not installed on the dev server and not needed before
   Phase 04. Install then: `CREATE EXTENSION vector;`
3. **Neon not exercised.** The pooled/direct DSN split is implemented and
   configurable but tested only against local PostgreSQL 18.1. Smoke command:
   `TUTORTWIN_DATABASE_MIGRATION_URL=<neon-direct> alembic upgrade head` then
   `TUTORTWIN_DATABASE_URL=<neon-pooled> python -m tutortwin` and `curl /readyz`.
4. **`apps/admin/` is an empty directory.** Phase 06 owns it; no placeholder
   scaffolding was generated.
5. **No load or chaos testing.** Deferred to Phase 07.
6. **Windows event-loop workaround.** `python -m tutortwin` exists because
   uvicorn's own loop setup breaks async psycopg on Windows. No effect on Linux.

## Result

**Phase 01 complete.** 57 tests passing, 94% coverage, ruff and strict mypy
clean, migrations reversible, zero paid AI calls. Stopping here per the loop
protocol.
