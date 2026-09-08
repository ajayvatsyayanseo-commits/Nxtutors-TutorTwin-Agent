# TutorTwin API

Status: Phase 01. Base URL is the service root; OpenAPI at `/docs`.

## Authentication

Internal callers present a shared secret:

```
x-internal-key: <TUTORTWIN_INTERNAL_API_KEY>
```

Required in production (startup fails without it). If unset outside production
the check is skipped for local convenience. Phase 07 replaces this with OIDC.

## Correlation headers

| Header | Behaviour |
|---|---|
| `x-request-id` | Honoured if sent, generated otherwise; echoed on response |
| `x-correlation-id` | Same; survives Lead Intake -> TutorTwin in Phase 08 |

## `POST /v1/events`

Ingests one normalized event. See [EVENT_MODEL.md](EVENT_MODEL.md) for the
contract.

Request:
```json
{
  "event_id": "evt_1", "request_id": "req_1", "correlation_id": "corr_1",
  "source": "test_harness",
  "subject": {"external_type": "test_phone", "external_id": "+919999999999"},
  "message": {"message_id": "msg_1", "type": "TEXT", "text": "Explain quadratic equations"},
  "occurred_at": "2026-01-01T00:00:00Z"
}
```

Response `200`:
```json
{
  "conversation_id": "455aa492-0897-416a-932b-34d0e875f0bb",
  "status": "COMPLETED",
  "outbound_actions": [{"type": "SEND_TEXT", "text": "..."}],
  "handoff": null,
  "usage": {"paid_model_calls": 0},
  "idempotent_replay": false
}
```

`status` is `COMPLETED` | `REJECTED` | `FAILED`. A duplicate returns the stored
response with `idempotent_replay: true` and performs no new work.

## `GET /v1/conversations/{id}`

Ownership-checked read. The caller states who is asking:

```
GET /v1/conversations/{id}?subject_external_type=test_phone&subject_external_id=%2B919999999999
```

A conversation belonging to another subject returns **404, not 403** - existence
of another student's data is not disclosed.

## `GET /healthz`

Liveness. Touches no dependency, so a database blip cannot cause healthy
containers to be killed. Always `{"status": "ok"}` while the process runs.

## `GET /readyz`

Readiness. Checks Postgres under a short timeout
(`TUTORTWIN_DB_HEALTH_TIMEOUT_SECONDS`, default 2s).

- `200 {"status": "ready", "checks": {"database": "ok"}}`
- `503 {"status": "not_ready", "checks": {"database": "error"|"timeout"}}`

## Errors

Every error shares one envelope:

```json
{"error": {"code": "VALIDATION_FAILED", "message": "Request failed validation.", "fields": ["message.type"]}}
```

| Code | HTTP |
|---|---|
| `VALIDATION_FAILED` | 422 |
| `PAYLOAD_TOO_LARGE` | 413 |
| `UNAUTHENTICATED` | 401 |
| `FORBIDDEN` | 403 |
| `NOT_FOUND` / `IDENTITY_UNRESOLVED` | 404 |
| `ENTITLEMENT_INACTIVE` | 403 |
| `DEPENDENCY_UNAVAILABLE` | 503 |
| `INTERNAL_ERROR` | 500 |

Validation errors return field *locations* but never submitted values, since
those may carry student content. Database failures become
`DEPENDENCY_UNAVAILABLE` with no driver text, host or credential leaked.

## Limits

Request bodies above `TUTORTWIN_MAX_REQUEST_BYTES` (default 256 KiB) are
rejected with 413 before parsing. Content-Length is checked first, then the
streamed body, so a chunked request cannot bypass the header check.

## Not yet implemented

`POST /v1/conversations/{id}/messages`, `/internal/jobs/{job_id}` (Phase 03),
and the Phase 08 handoff endpoints. `/v1/admin/*` arrived in Phase 06 - see
below.

---

# Phase 05: no new HTTP surface

The learning engine adds **no endpoints**. `POST /v1/events` remains the single
ingress: a student asking for a quiz, a graph, a twin problem or essay feedback
sends a message like any other, and the meta-agent routes it. A `/v1/quizzes`
endpoint would create a second way into the same orchestration, with its own
authentication, idempotency and budget handling to keep in step.

What changed is the shape of what comes back.

## Artifacts in outbound actions

An answer that produced a diagram or a printable paper carries a reference, never
bytes:

```json
{
  "type": "SEND_MEDIA",
  "artifact": {
    "id": "3f1c...", "kind": "FUNCTION_PLOT", "artifact_format": "PNG",
    "sha256": "ed3f4ec2...", "width": 704, "height": 440,
    "generated_by": "deterministic"
  }
}
```

`generated_by` is `deterministic` or `model_spec` and records whether a model
produced the *specification*. It never records a model producing the image.

Bytes are fetched from the BlobStore by key, which encodes the owning subject, so
an artifact reference cannot be redeemed by another student.

## Test mode never carries answers

A delivered paper is a list of `StudentQuestion`: number, type, prompt, marks,
options. The type has no field able to hold `correct_option`, `expected_answer`,
`rubric` or `worked_solution`, so no serialisation of it can leak a key, and the
delivery query does not select the `answer_key` column at all.

The printable artifact is rendered from the same projection, so the stored bytes
carry the same guarantee as the JSON.

## Cost fields

`usage.paid_model_calls` continues to count every provider call, verifier calls
included. A graded attempt additionally stores its own `model_calls`, so a
ten-question paper graded with one batched call is visible as such in the
database rather than only in a log line.

---

# Phase 06: the admin control plane API

`/v1/admin/*`, consumed by the Next.js control plane in `apps/admin`. It is a
separate surface from `/v1/events` with a separate authentication scheme: the
event API is called by internal services with a shared secret, this one is
called by named human operators with sessions and roles.

## Authentication

```
POST /v1/admin/auth/login   { "email": "...", "password": "..." }
 ->  200 { "actor": {...}, "csrf_token": "...", "expires_at": "..." }
     Set-Cookie: tt_admin_session=<token>; HttpOnly
```

Every subsequent call carries both:

| Header | Meaning |
|---|---|
| `x-admin-session` | The session token (also accepted as the `tt_admin_session` cookie) |
| `x-admin-csrf` | The token from the login response |

Both are required on **every** request, not only mutations. One rule is easier
to keep right than a rule with an exception, and the e2e suite proves a call
without the CSRF header is refused.

Sessions last 12 hours, or 60 minutes idle. Changing a password revokes them
all.

## Errors

Same envelope as the rest of the API. The statuses that matter here:

| Status | Meaning |
|---|---|
| 401 | No session, expired session, or a failed login |
| 403 | Authenticated, but the role does not include this permission |
| 404 | No such row - also what a wrong-owner read returns |
| 422 | Validation, including a `group_by` outside its closed set |
| 429 | Login rate limit (account lockout or IP throttle) |

## Endpoints

Every path below is prefixed `/v1/admin`. "Perm" is the permission
`require(...)` checks; **HR** marks an action that additionally requires
`confirm: true` and a `reason` of at least 8 characters, and that writes an
audit event with before/after state.

### Session

| Method | Path | Perm |
|---|---|---|
| POST | `/auth/login` | - |
| POST | `/auth/logout` | session |
| GET | `/auth/me` | session |
| POST | `/auth/change-password` | session |
| GET | `/roles` | session |

### Overview and governance

| Method | Path | Perm |
|---|---|---|
| GET | `/dashboard?window_days=` | `dashboard:read` |
| GET | `/health-summary` | `dashboard:read` |
| GET | `/costs?window_days=&group_by=` | `cost:read` |
| GET | `/audit?page=&action=&actor_id=&target_id=&high_risk_only=&since=&until=` | `audit:read` |

`group_by` is a closed `Literal`: `model`, `provider`, `capability`, `student`,
`tutor`, `media`, `verification`, `day`. Anything else is a 422 before a query
is built.

### Students

| Method | Path | Perm |
|---|---|---|
| GET | `/students?q=&plan_code=&status=&page=&page_size=` | `student:read` |
| GET | `/students/{id}` | `student:read` |
| POST | `/students/{id}/entitlement` | `student:write` **HR** |
| POST | `/quota/reset` | `student:write` **HR** |
| POST | `/students/{id}/delete-data` | `student:write` **HR**, plus the typed identity |

### Tutors and personas

| Method | Path | Perm |
|---|---|---|
| GET | `/tutors?q=` | `tutor:read` |
| GET | `/tutors/{id}` | `tutor:read` |
| POST | `/tutors` | `tutor:write` |
| POST | `/tutors/{id}/personas` | `tutor:write` |
| POST | `/tutors/{id}/personas/{version_id}/activate` | `persona:activate` **HR** |
| POST | `/tutors/{id}/students` | `tutor:write` |

A persona version is **immutable once activated**. Editing a live persona would
make last week's answer unexplainable, so every change is a new version and
activation is the only mutation. A student has one active tutor; assigning
retires the previous assignment rather than deleting it.

### Catalog

| Method | Path | Perm |
|---|---|---|
| GET / POST | `/plans` | `plan:read` / `plan:write` **HR** |
| GET / POST | `/models` | `model:read` / `model:write` **HR** |
| GET / POST | `/prompts` | `prompt:read` / `prompt:write` |
| POST | `/prompts/{version_id}/activate` | `prompt:write` **HR** |
| GET | `/flags`, `/flags/summary` | `flag:read` |
| POST | `/flags/{key}` | `flag:write` **HR** |

`GET /models` reports whether each provider credential is **present**. The value
is never in the response.

### Activity

| Method | Path | Perm |
|---|---|---|
| GET | `/conversations`, `/conversations/{id}` | `conversation:read` |
| GET | `/documents`, `/documents/{id}` | `document:read` |
| POST | `/documents/{id}/reprocess` | `document:write` **HR** |
| POST | `/documents/{id}/delete` | `document:write` |
| GET | `/learning/assessments`, `/learning/assessments/{id}` | `learning:read` |
| GET | `/jobs`, `/jobs/{id}` | `job:read` |
| POST | `/jobs/{id}/retry`, `/jobs/{id}/cancel` | `job:write` |

A document response carries chunk counts and embedding status. **Raw vectors are
never returned.**

### Administrators

| Method | Path | Perm |
|---|---|---|
| GET / POST | `/admins` | `admin_user:read` / `admin_user:write` **HR** |
| POST | `/admins/{id}/role` | `admin_user:write` **HR** |
| POST | `/admins/{id}/status` | `admin_user:write` **HR** |
| POST | `/admins/{id}/reset-password` | `admin_user:write` **HR** |

A newly created administrator always lands with `must_change_password`, because
the password was typed by somebody else.

## Pagination

Every list endpoint pages server-side and answers
`{ total, page, page_size, items }`. `page_size` is capped; the tables behind
these endpoints are unbounded.

## Money

Costs are **micros** (millionths of a unit), integers, at the rate stored on the
ledger row when the call happened. A price change does not rewrite last month's
bill.

## Latency

`/dashboard` reports p50 and p95 of the whole request, measured from the inbound
event row to the terminal state row - queueing and extraction included, not just
the provider call. Both are **null**, never zero, when nothing completed in the
window: zero would read as "instant" on a dashboard whose job is to say what
students are experiencing.
