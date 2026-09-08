# Phase 06 Acceptance — Production Next.js TutorTwin Admin Control Plane

Standalone. **No Lead Intake, no NX website, no MySQL.** The control plane talks
only to the TutorTwin FastAPI service, which talks only to TutorTwin's
PostgreSQL. The browser reaches neither the API nor the database directly.

## Environment

| Component | Version |
|---|---|
| OS | Windows 11 (win32) |
| Python | 3.12.10 |
| PostgreSQL | 18.1 (local) |
| Node | 20.19.4 |
| npm | 10.8.2 |
| Next.js | 16.3.4 (App Router, Turbopack) |
| React | 19.2.8 |
| TypeScript | 6.0.3 |
| Vitest | 4.1.11 |
| Playwright | 1.62.1 |

Compatibility was checked before pinning: Next 16 requires Node ≥ 20.9 and pairs
with React 19; `engines.node` in `apps/admin/package.json` states it.

## Verification commands and results

```
$ ruff check .
All checks passed!

$ mypy src
Success: no issues found in 100 source files

$ alembic check
No new upgrade operations detected.

$ pytest
611 passed in 116.88s

$ pytest tests/integration/test_admin_security.py
72 passed in 45.45s
```

```
$ cd apps/admin && npm run verify        # typecheck + lint + tests + production build

> tsc --noEmit
(no output)

> eslint .
(no output)

> vitest run
 Test Files  4 passed (4)
      Tests  40 passed (40)

> next build
✓ Compiled successfully
  ✓ authInterrupts
✓ Generating static pages using 11 workers (4/4)
Route (app)  —  22 routes, all ƒ (server-rendered on demand) except /_not-found
```

```
$ npx playwright test          # real Next server, real API, real PostgreSQL
  ok  1  1. an administrator signs in and lands on the dashboard
  ok  2  1b. a wrong password is refused with one generic message
  ok  3  2. student search filters server-side and paginates
  ok  4  3. entitlement override requires a reason and is audited
  ok  5  4. a student is assigned to a tutor
  ok  6  4b. a tutor is assigned from the student's own page
  ok  7  5. a persona draft is created and then activated
  ok  8  6. a model route is edited, and no secret is displayed
  ok  9  7. a conversation shows its turns, requests, latency, retrieval and cost
  -  10  8. an assessment shows the key and the grading evidence      (skipped)
  ok 11  9. costs group by a closed set and total correctly
  ok 12 10. a kill switch is flipped and restored, with both changes audited
  -  13 11. a failed job is retried without erasing its attempt history (skipped)
  ok 14 12. the audit log shows who did what, why, and what changed
  ok 15 13. a support operator is offered less, and refused the rest
  ok 16 14. signing out revokes the session server-side
  ok 17 15. the API rejects a direct browser call without the CSRF header

  2 skipped
  15 passed (23.0s)
```

**The production build passes.** Phase 06 is not declared complete on a failing
build, and it is not.

### The two skips, stated plainly

Scenarios 8 and 11 skip in this environment because the fixture data cannot be
created without inventing it:

- **8 (mock test inspection)** needs an assessment, which the learning engine
  produces from a real model response. No provider credential is configured
  here, so every request degrades to the deterministic "being set up" reply.
- **11 (failed job retry)** needs a job in `FAILED`, which the media pipeline
  produces from a real upload against real object storage (Phase 07).

Both tests are written, both assert real behaviour, and both call `test.skip`
with a stated reason. The alternative — inserting rows straight into Postgres to
make a green tick appear — would make the suite pass for the wrong reason,
which is exactly what `e2e/seed.ts` exists to avoid. Both scenarios' underlying
endpoints are covered by the Python suite.

## Isolation confirmed

| Claim | How it is true |
|---|---|
| No Lead Intake | No import, table, endpoint or configuration reference anywhere in `apps/admin` or `src/tutortwin/api/routes/admin`. |
| No NX website | Same. Entitlements edited here are local rows marked `source='admin_override'` for Phase 09 to reconcile. |
| No MySQL | The only DSNs in the project are `postgresql+psycopg://`. |
| No browser → Postgres | The browser's CSP `connect-src` is `'self'`. The only client of the API is the Next.js **server**. |

## Architecture delivered

`apps/admin/` — a Next.js 16 App Router application, built and deployed
independently of the Python service. Its sole dependency on the API is
`TUTORTWIN_API_URL`.

Runtime dependencies: `next`, `react`, `react-dom`, `zod`. No UI kit, no chart
library, no state manager, no identity SaaS.

Pages are server components; mutations are server actions. That is the security
design, not a preference: the session token lives in an httpOnly cookie only the
Next server reads, so no credential ever enters a client bundle.

## Authentication delivered

| Required | Delivered |
|---|---|
| Argon2id password hashes | `security/passwords.py`, with transparent rehash |
| Secure session cookies | httpOnly, `SameSite=Strict`, `Secure` in production, 12 h absolute / 60 min idle |
| CSRF protection | Separate 256-bit token, echoed in `x-admin-csrf`, constant-time compare; Next also signs its own action requests |
| TOTP-ready design | Login is a single seam between password verification and session issue. Not enabled — half a second factor is worse than none. |
| Password reset / bootstrap | `tutortwin.cli.admin bootstrap | reset-password | unlock | list`; bootstrap refuses once any administrator exists |
| Login rate limiting | 5 failures → 15 min account lockout; 20 failures / 15 min per IP. Counters are **rows**, so a cold start does not reset them. |
| No default production password | The bootstrap hash comes from configuration; a bootstrapped operator must change it before any page renders |

## RBAC delivered

Six roles — `SUPER_ADMIN`, `ADMIN`, `ACADEMIC_ADMIN`, `SUPPORT`,
`TUTOR_VIEWER`, `USAGE_VIEWER` — over 24 `resource:verb` permissions.

Enforcement is **server-side**: `require(Permission.X)` on every endpoint, and
`test_role_matrix_is_enforced_by_the_server` walks the entire matrix against the
real API. Scenario 13 additionally proves it from a browser: a `SUPPORT`
operator is offered fewer sections, and typing `/costs` returns **HTTP 403**,
not a 200 with an apology inside it.

## Navigation delivered

Fourteen sections, grouped by the question being asked, every one backed by real
data: Dashboard, Students, Tutors, Conversations, Documents & RAG, Learning,
Plans, Models & routing, Prompt versions, Jobs, Costs, Feature flags, Audit log,
Administrators. Full inventory in [ADMIN_PANEL.md](../ADMIN_PANEL.md).

## What changed in this phase

The control plane already existed in draft. This phase closed the gaps between
it and the brief, and the defects found while proving it.

### Backend

| Change | Why |
|---|---|
| `usage_ledger.capability`, migration `c4a17be9d520` | Cost **by capability** was the one grouping the ledger could not answer. It is recorded at call time; deriving it afterwards meant guessing which message belonged to which provider call. Nullable, and left null for historical rows — a backfilled guess would look authoritative and not be. |
| Dashboard **p50 / p95 latency** | Measured from the inbound event row to the terminal state row, so it includes queueing and extraction rather than flattering the service with a provider-only number. Percentiles, not a mean: one 40-second OCR moves a mean and says nothing about what most students saw. Null, never zero, when nothing completed. |
| Dashboard **prompt-cache hit share** | `cached_tokens / input_tokens` from the ledger. The extraction cache still reports *entries written*, because nothing records a hit and reporting an unmeasured rate would be worse than reporting the measured one. |
| Cost `group_by` extended to `capability`, `tutor`, `media`, `verification` | The full set the brief asks for. Still a closed `Literal` choosing a column expression — nothing an operator types reaches SQL as text. |
| Conversation detail gained **per-turn latency, RAG retrieval and verification** | The brief lists all three under Conversations and none were there. A timeline said what was answered but not why it was slow, expensive or thin. Latency is `terminal state - inbound event`, null while a turn is in flight. Retrieval shows the skipped searches too, with their reason: an empty list would read as a missing feature rather than a deliberate decision. Chunk **ids** only - never text, never vectors. |

`tutor` attribution follows each student's current active assignment, because
that is the only tutor link a ledger row resolves through; the response and the
UI say so rather than letting an operator assume otherwise.

### Control plane

| Change | Why |
|---|---|
| **Content-Security-Policy with a per-request nonce** (`src/middleware.ts`) | `next.config.ts` claimed a nonce-based CSP; no middleware existed and no CSP was sent. The claim is now true: `script-src 'self' 'nonce-…' 'strict-dynamic'`, no `unsafe-inline` for scripts. |
| **`HighRiskFields` generates its ids** | It hard-coded `id="reason"`. Ten kill switches on one page produced ten identical ids, so every label pointed at the *first* field: the flags page looked correct and could not be used, and a screen reader read three concatenated labels for one input. Now a client component using `useId`. |
| **Confirmation checkbox is no longer `required` in HTML** | The API is the control. A native validation bubble inside a collapsed `<details>` hid the server's own answer instead of delivering it. |
| **`ActionForm` dispatches the submit by hand** | React resets an uncontrolled form once its action returns, so a refused high-risk action blanked the reason the operator had just typed. A reason that must be typed twice becomes "asdf" on the second attempt. |
| **Refused reads answer 403** | `forbidden()` from the authenticated shell, using the same section list the sidebar renders from. Refusing three fetches deep can only produce a 200, which a monitor cannot tell from success. |
| **Expired session redirects to `/login`** | It previously logged an error and rendered a failed panel. A session ending after a working day is the ordinary case, not a fault. |
| **Tutor assignment from the student page** | The direction an operator actually works in — they arrive at a student from a support ticket, not at a tutor. Same endpoint, same audit event, a `<select>` of tutors instead of a pasted UUID. The tutor page keeps its own form, now with a `datalist` of identities that still accepts a pasted id. |
| **Persona draft prefilled, `signature_phrases` editable** | The field existed in the API and not in the form, and a new draft started blank — retyping nine fields to change a tone is how a persona loses its other eight. |
| **Dashboard and visual system rebuilt** | Headline KPIs, tone rails derived from the numbers, proportion bars drawn with a `<div>`, grouped sidebar, one token set for light and dark. No chart library: every comparison here is one row against a total, and loading a chart runtime would also mean loosening the CSP. |
| **`next start` honours `PORT`; Playwright honours `ADMIN_BASE_URL`** | So the suite runs beside a development server instead of silently reusing it and testing the wrong API. |

### Test harness

| Change | Why |
|---|---|
| `e2e/seed.ts` walks the support operator's first-login password change | A newly created administrator always lands with `must_change_password`. Every test signing in as that operator was being redirected to the change-password page. |
| `e2e/seed.ts` publishes a plan, entitles the fixture student, and posts real events | Without an entitlement every request is refused at the gate — correct behaviour, and useless as a fixture. Conversations now exist for the inspection scenario. Still entirely through the API. |
| `confirmHighRisk` ticks the confirmation **by name** | The model routing panel has a second checkbox. Ticking whichever came first is how a test silently confirms nothing. |
| `errorBanner()` replaces bare `getByRole("alert")` | Next renders a permanently present, usually empty route announcer with that role. |
| Dedicated `tutortwin_e2e` database | `tests/conftest.py` truncates `tutortwin_test` on every test, which erases the e2e fixtures. |

## Security tests

Proved by `tests/integration/test_admin_security.py` (72 tests) and the
Playwright suite:

| Claim | Where |
|---|---|
| Unauthenticated requests are rejected | Parametrised over every protected GET |
| The whole role matrix is enforced by the server | `test_role_matrix_is_enforced_by_the_server` |
| Privilege escalation is blocked | Self-demotion refused; `admin_user:write` separated from everything else |
| Secret fields are absent from responses and HTML | API assertion, plus scenario 6 scanning the rendered page for `sk-` and `api_key` |
| CSRF is required on every call | Scenario 15: a direct browser call without the header is refused |
| Session security | 12 h / 60 min lifetimes, hashed tokens, logout revokes server-side (scenario 14) |
| Student ownership and visibility are enforced by the backend | `test_admin_reads_are_scoped_by_the_requested_student` |
| Every high-risk action is audited | Reason and before/after asserted per action; scenario 12 reads them back |
| Malicious query and filter input is safe | `group_by` closed set (422 on injection), escaped `LIKE` patterns, bound parameters throughout |
| Every offered cost grouping actually answers | `test_every_offered_cost_grouping_answers` — including the two that are not plain columns |
| Latency is null, not zero, when nothing completed | `test_dashboard_reports_latency_and_cache_share` |

## Documentation

- [ADMIN_PANEL.md](../ADMIN_PANEL.md) — architecture, auth, RBAC, every section, high-risk actions, how to run it, known gaps.
- [SECURITY.md](../SECURITY.md) — new "Phase 06: admin control plane security" section; the stale claim that admin auth was unimplemented is corrected.
- [API.md](../API.md) — new "Phase 06: the admin control plane API" section listing all 46 endpoints with their permissions and high-risk markers.

## Known limitations

1. **TOTP is designed for, not enabled.** The seam exists in the login flow.
2. **An entitlement override does not yet change what a student may do.** The
   runtime gate reads the entitlement gateway, which is a fake until Phase 09
   overlays the website. The override is stored and audited so Phase 09 can
   honour it; the students page says the mapping is standalone.
3. **A persona's notification preference is stored and never acted on.** There is
   no delivery channel to act on it with. The UI labels it as such.
4. **Cost by tutor moves with reassignment.** Stated in the UI and the API docs.
5. **`capability` is null for every ledger row written before this phase.** They
   group as `unattributed`.
6. **Scenarios 8 and 11 skip** without a model provider and object storage, as
   described above.
7. **The extraction cache reports entries, not hits.** Nothing records a hit.
