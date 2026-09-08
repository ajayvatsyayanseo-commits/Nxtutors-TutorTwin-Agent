# TutorTwin Admin Control Plane

Status: **Phase 06, delivered.** A production Next.js application at
`apps/admin/`, deployed independently of the Python API.

It is an operational control plane, not a dashboard mock: every number is read
from the rows the system wrote for its own reasons, and every button performs a
real, audited mutation through the real API.

---

## Isolation

The control plane touches **TutorTwin standalone data only**.

- No Lead Intake code, tables or endpoints.
- No NX website code, tables or endpoints.
- No MySQL connection.
- **No browser-to-Postgres path of any kind.** Every read and write goes through
  the TutorTwin FastAPI service.

Student, tutor and entitlement records here are standalone TutorTwin rows.
Phase 09 overlays the website's source of truth; until then an entitlement
edited here is marked `source='admin_override'` so the two can be told apart
later rather than silently merged.

---

## Architecture

```
browser ──► Next.js (apps/admin, server components + server actions)
                │   httpOnly cookies, no token in page script
                └─► FastAPI /v1/admin/*  ──► PostgreSQL
```

| Piece | Choice | Why |
|---|---|---|
| Framework | Next.js 16 (App Router), React 19, TypeScript 6 | Server components mean the session token never enters a client bundle. |
| Data access | `src/lib/api.ts` only | One function owns the API call, the timeout and the error shape. |
| Mutations | Server actions | Next signs and verifies its own action requests, which closes the cross-site POST a plain form would open. |
| Styling | One plain CSS file with custom properties | Fourteen dense table pages; a utility framework would add a build step and thirty classes a row. |
| Charts | None | Every comparison here is one row against a total, which is a `<div>` width. Shipping a chart runtime would also mean loosening the CSP. |

Deployment is independent: `apps/admin` builds and runs on its own, and the
only thing it needs from the API is `TUTORTWIN_API_URL`.

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `TUTORTWIN_API_URL` | `http://127.0.0.1:8000` | The FastAPI service. Server-side only. |
| `TUTORTWIN_API_TIMEOUT_MS` | `15000` | Per-request timeout; a hung API becomes a 504 page, not a spinner. |
| `TUTORTWIN_ENVIRONMENT` | `local` | Printed in the sidebar so nobody edits staging thinking it is local. |
| `PORT` | `3000` (`3100` in dev) | `next start` reads it, so the suite can run beside a dev server. |

---

## Authentication

Local, standalone, no recurring paid identity dependency.

| Control | Implementation |
|---|---|
| Password storage | **Argon2id** (`security/passwords.py`); rehashed transparently when parameters change. |
| Session token | 256-bit random, stored as SHA-256. A database dump yields no login. |
| Session lifetime | 12 hours absolute, 60 minutes idle. |
| CSRF | A second 256-bit token, issued at login, echoed in `x-admin-csrf`, compared constant-time. |
| Cookie storage | Both tokens in **httpOnly** cookies read only by the Next server. `document.cookie` holds nothing useful. |
| Login rate limit | 5 failures locks the account for 15 minutes; 20 failures from one IP in 15 minutes throttles the IP. Counters are **rows**, so a cold start does not reset them. |
| Failure message | One generic message for unknown email, wrong password, disabled and locked, after the same amount of work. |
| Bootstrap | `python -m tutortwin.cli.admin bootstrap`, reading an Argon2id hash from configuration. **There is no default production password.** |
| First login | A bootstrapped operator is redirected to change their password before any page renders. |
| Password change | Revokes every session for the account, this one included. |
| TOTP | Not enabled. The schema and the login flow are shaped for it — a second factor is a step between password verification and session issue — but shipping a half-built second factor would be worse than an honest gap. |

### Recovery

```bash
python -m tutortwin.cli.admin reset-password --email ops@example.com   # prints a new password once
python -m tutortwin.cli.admin unlock --email ops@example.com           # clears a lockout
python -m tutortwin.cli.admin list                                     # who exists, and their roles
```

`bootstrap` **refuses to run** once any administrator exists. A bootstrap that
also works on a live system is a back door with a friendly name.

---

## RBAC

Six roles, each a fixed set of `resource:verb` permissions
(`domain/admin.py`). Read and write are always separate permissions.

| Role | Can |
|---|---|
| `SUPER_ADMIN` | Everything, including creating administrators and changing roles. |
| `ADMIN` | Everything except administrator management. |
| `ACADEMIC_ADMIN` | Students, tutors, personas, learning, plans, prompts, documents. |
| `SUPPORT` | Read students, conversations, documents, learning, tutors, plans, flags; retry and cancel jobs. |
| `TUTOR_VIEWER` | Read tutors, students, learning, conversations. |
| `USAGE_VIEWER` | Read the dashboard, costs, models, plans. |

**Enforcement is server-side.** `require(Permission.X)` guards every endpoint,
and `test_role_matrix_is_enforced_by_the_server` walks the whole matrix against
the real API.

The control plane refuses in two places, neither of which is the real control:

1. The sidebar omits sections the operator cannot use.
2. The authenticated layout refuses the section outright, from the same list the
   sidebar renders from, so a typed URL returns a **real HTTP 403** and
   `app/forbidden.tsx` — not a 200 with an apology inside it.

A super administrator cannot reduce their own role: an account that removes its
own last authority can lock the whole team out.

---

## Sections

| Section | Shows | Can change |
|---|---|---|
| **Dashboard** | Active/total standalone students, requests, questions, media, mocks, model calls, spend, **p50/p95 latency**, verifier rate, **prompt-cache hit share**, quota blocks, local-vs-vision extraction split, RAG sources and chunks, failures by code, spend by alias | — |
| **Students** | Search by identity or name, plan and status filters, server-side pagination; per student: identity, entitlement history, assigned tutor, usage, conversations, documents, assessments, topic progress, memories | Entitlement override, quota reset, tutor assignment, learning-data deletion |
| **Tutors** | Tutor records, persona version history, active persona (avatar, subjects, tone, pedagogy, language, response style, signature phrases, notification preference), assigned students | Create tutor, draft a persona version, activate a version, assign a student |
| **Plans** | Feature matrix and limits per plan, version history | Publish a new plan version |
| **Models & routing** | Alias, provider, vendor model id, enabled, cost rates, rate version, capability and verifier routes, **which credentials are present** | Change a route |
| **Prompt versions** | Immutable version history, draft/active state, author, reason | Draft, activate, roll back |
| **Conversations** | Timeline of turns, input type, normalized text, safe media metadata, capability, model calls, RAG sources, cost, latency, errors, verification | — |
| **Documents & RAG** | Owner, hash, status, extraction method, OCR pages, chunk count, embedding status, visibility | Reprocess, delete |
| **Learning** | Progress, weak topics, quizzes, flashcards, mock tests, attempts, grading evidence and answer key | — |
| **Jobs** | Queue state, attempts, last error, correlation id | Retry, cancel |
| **Costs** | Spend grouped by model, provider, capability, student, tutor, media, verification or day, over 7/30/90/365 days; cached-token savings | — |
| **Feature flags** | Ten kill switches: PDF, image, voice, mock tests, verifier, each provider, advanced model, RAG, code sandbox | Flip any switch |
| **Audit log** | Every mutation with actor, action, target, reason, before/after, timestamp; filterable, high-risk-only view | — |
| **Administrators** | Accounts, roles, status, last login | Create, change role, enable/disable, reset password |

### What is deliberately not shown

- **API keys and secrets.** The models page reports *whether* a provider
  credential is configured. The value is not in the response, so it cannot be in
  the page. Asserted by the e2e suite, which scans the rendered HTML.
- **Raw vector arrays.** A document shows chunk counts and embedding status.
- **Student message text in logs.** Unchanged from Phase 01.

### Cost attribution, honestly

Two groupings carry a caveat, and the page states it rather than letting an
operator assume otherwise:

- `capability` is recorded on the ledger row at call time. Rows written before
  that column existed group as **`unattributed`** rather than being guessed into
  a capability that did not spend them.
- `tutor` follows each student's **current active assignment**, because that is
  the only tutor link a ledger row resolves through. Reassigning a student moves
  their historical spend with them.

---

## High-risk actions

Ten actions require a typed reason of at least 8 characters **and** an explicit
confirmation, both checked by the API and not merely by the form:

`ENTITLEMENT_OVERRIDE`, `QUOTA_RESET`, `MODEL_ROUTE_CHANGE`,
`FEATURE_KILL_SWITCH`, `PERSONA_ACTIVATION`, `STUDENT_DATA_DELETE`,
`DOCUMENT_REPROCESS`, `ADMIN_ROLE_CHANGE`, `PLAN_POLICY_CHANGE`,
`PROMPT_ACTIVATION`.

Each writes an audit event carrying the actor, the reason, and the before/after
state. Deleting a student's learning data additionally requires typing that
student's identity, and the API checks the typed value against the row it is
about to erase.

The confirmation checkbox is deliberately **not** `required` in HTML. The API is
the control; a native validation bubble inside a collapsed panel would hide the
server's own answer instead of delivering it.

---

## UI behaviour

- **Loading states** — `loading.tsx` streams skeleton rows per section.
- **Empty states** — "no results for this filter" and "nothing exists yet" are
  different messages. An operator who cannot tell them apart assumes the tool is
  broken.
- **Errors** — a failed action returns a sentence on the form **and keeps the
  operator's input**. `ActionForm` dispatches the submit by hand because React
  resets an uncontrolled form once its action returns, and a reason that has to
  be typed twice becomes "asdf" on the second attempt.
- **No optimistic updates.** Every mutation here changes cost, entitlement or
  routing. Showing success before the server agrees is a lie exactly when a lie
  is most expensive.
- **Server-side pagination, filtering and search** on every unbounded table; the
  page links carry the active filters, so paging never silently changes the
  result set.
- **Accessibility** — one visible focus ring, a skip link, labelled fields with
  generated (never duplicated) ids, `aria-current` on the active section, and
  tables with real column headers and captions.
- **Responsive** — the shell collapses to a single column below 900px; wide
  tables scroll inside their own container rather than the page.
- **Theme** — light and dark from one token set.
- **No fake data.** Every page renders what the API returned, including zero.

---

## Running it

```bash
cd apps/admin
npm install
TUTORTWIN_API_URL=http://127.0.0.1:8000 npm run dev     # http://127.0.0.1:3100
```

```bash
npm run verify     # typecheck + lint + unit/component tests + production build
npm run e2e        # Playwright, against a real API and a real database
```

### End-to-end suite

Nothing is mocked. Playwright starts only the Next.js server; the Python API
must already be running, because starting it from the harness would hide a
configuration failure behind a test fixture.

```bash
# 1. a database and an administrator
TUTORTWIN_DATABASE_MIGRATION_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:5432/tutortwin_test \
  alembic upgrade head
TUTORTWIN_ADMIN_BOOTSTRAP_EMAIL=e2e-super@example.com \
TUTORTWIN_ADMIN_BOOTSTRAP_PASSWORD_HASH="$(python -m tutortwin.cli.admin hash-password)" \
  python -m tutortwin.cli.admin bootstrap --no-force-change

# 2. the API, pointed at that database, with the fixture identity entitled
TUTORTWIN_DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:5432/tutortwin_test \
TUTORTWIN_FAKE_PRO_SUBJECTS='["+919999000001"]' \
PORT=8001 python -m tutortwin

# 3. fixtures, then the suite
cd apps/admin
TUTORTWIN_API_URL=http://127.0.0.1:8001 TUTORTWIN_INTERNAL_API_KEY=<key> npx tsx e2e/seed.ts
TUTORTWIN_API_URL=http://127.0.0.1:8001 ADMIN_BASE_URL=http://127.0.0.1:3101 npx playwright test
```

`ADMIN_BASE_URL` picks the port, so the suite runs beside a development server
instead of colliding with it.

The seeder creates everything **through the API**, including walking the support
operator's first-login password change — a seeder that wrote to Postgres
directly would still pass if every endpoint were broken.

---

## Known gaps

| Gap | Why it is a gap, not a bug |
|---|---|
| TOTP second factor | Designed for, not enabled. Half a second factor is worse than none. |
| Entitlement override does not change what a student may do **yet** | The runtime gate reads the entitlement gateway, which is a fake until Phase 09 overlays the website. The override is stored and audited so Phase 09 can honour it. |
| Notification preference on a persona | Stored, never acted on. There is no delivery channel to act on it with. |
| Mock-test and failed-job e2e scenarios skip without fixtures | Both need a configured model provider and media storage. The tests are written and skip with a stated reason rather than asserting against invented rows. |
| Prompt-cache **hit rate**, not extraction-cache hit rate | The extraction cache records entries written, not hits. Reporting a hit rate the system never measured would be worse than reporting the number it does. |
