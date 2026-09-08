# TutorTwin

**An AI tutor that lives inside WhatsApp.** A student photographs their
homework, sends a worksheet PDF, records a voice note, or just types a question
— and gets a worked explanation back. ₹100 for 30 days.

No app to install. No login. The tutor answers to a name the student chooses,
usually their favourite teacher's.

```
816 Python tests · 14 admin e2e tests · mypy clean on 115 files · zero paid AI calls in CI
```

---

## Contents

- [What it does](#what-it-does)
- [The student journey](#the-student-journey)
- [Capabilities](#capabilities)
- [How a photograph becomes an answer](#how-a-photograph-becomes-an-answer)
- [Mathematics](#mathematics)
- [Subscriptions and payment](#subscriptions-and-payment)
- [The control plane](#the-control-plane)
- [Cost engineering](#cost-engineering)
- [Security](#security)
- [The shared database](#the-shared-database)
- [Architecture](#architecture)
- [Running it locally](#running-it-locally)
- [Configuration](#configuration)
- [Testing](#testing)
- [Deploying](#deploying)
- [Third-party services](#third-party-services)

---

## What it does

Three deployable pieces, one repository:

| Piece | What it is | Login | Port |
|---|---|---|---|
| `src/tutortwin/` | FastAPI service — webhooks, orchestration, media, admin API | per-endpoint | 8010 |
| `apps/site/` | Public marketing site, signup form, payment | none | 3010 |
| `apps/admin/` | Operational control plane, 20 pages | required | 3011 |

**42 database tables**, all inside a `tutor_twin` schema on a database shared
with another product.

---

## The student journey

```
              WhatsApp message
                     │
                     ▼
        POST /webhooks/whatsapp
        HMAC-SHA256 over the raw body ─── unsigned → dropped, £0 spent
                     │
                     ▼
        entitlement lookup (database)
                     │
         ┌───────────┴────────────┐
         │                        │
    no subscription          subscribed
         │                        │
    subscribe link          ┌─────┴──────┐
    £0 spent                │            │
                          text       attachment
                            │            │
                            │      brief gate ── no brief → held
                            │                    nothing fetched
                            │                    nothing paid
                            │            │
                            │      queued job
                            │            │
                            │      download → validate → sniff MIME
                            │            │
                            │      ┌─────┴──────┬──────────┐
                            │    image        PDF        audio
                            │      │            │          │
                            │   Tesseract   digital     Whisper
                            │   3 modes     text →      transcript
                            │      │        OCR →          │
                            │   confident?  vision         │
                            │    no → vision  │            │
                            │      │          │            │
                            │      └──────────┴────────────┘
                            │            extracted text
                            └────────────┬─────────────┘
                                         ▼
                            SymPy solves it exactly, if it can
                                         ▼
                            intent routing → budget gate → model
                                         ▼
                            deterministic verification (SymPy)
                                         ▼
                    answer + typeset equation images → WhatsApp
```

Every arrow that costs money has a gate in front of it.

---

## Capabilities

**23 routable capabilities**, chosen by an intent router, each with its own plan
entitlement and budget ceiling:

| Group | Capabilities |
|---|---|
| Tutoring | `GENERAL_TUTORING` `EXPLAIN_CONCEPT` `HOMEWORK_SOLVE` |
| STEM | `MATH` `PHYSICS` `CHEMISTRY` `BIOLOGY` `CODING` |
| Language | `WRITING_FEEDBACK` `LANGUAGE_HELP` |
| Assessment | `ANSWER_CHECK` `GRADE_WORK` `MOCK_TEST` `QUIZ` |
| Practice | `PRACTICE_GENERATION` `TWIN_PROBLEM` `FLASHCARDS` `REVISION` |
| Media | `DOCUMENT_QA` `IMAGE_QA` |
| Planning | `STUDY_PLAN` `RESEARCH_HELP` |

**Six pedagogy modes** decide *how* it answers, not what:
`HINT_FIRST` · `GUIDED` · `STEP_BY_STEP` · `SOCRATIC` · `ANSWER_AND_EXPLAIN` ·
`EXAM_REVISION`

The default is hint-first. A student who wants the answer can ask for it; a
student who wants to learn gets a question back.

**Learning engine** — eleven modules under `learning/`:

| Module | What it does |
|---|---|
| `mathsolver` | computes exact answers with SymPy before the model speaks |
| `verification` | checks a model's answer symbolically and numerically |
| `mathrender` | typesets equations to images for a channel with no maths |
| `solver` | the STEM pipeline and its escalation policy |
| `visuals` | plots, free-body diagrams, circuits, geometry — deterministic |
| `assessment` | quizzes and mock tests, delivery and grading |
| `practice` | spaced repetition and the progress engine |
| `twin` | parallel problems: same method, different numbers |
| `notes` | structured study notes from a topic or conversation |
| `essay` | writing feedback |
| `homework` | homework task state and the code-execution boundary |

**Retrieval** — `rag/` handles chunking, embeddings, ingestion and a pgvector
store, with owner-scoped retrieval so one student's documents never surface in
another's answer.

---

## How a photograph becomes an answer

A photo of homework is the commonest thing a student sends and the hardest
thing to get right.

**Fourteen media states**, and the pipeline cannot skip one:

```
RECEIVED_REFERENCE → ENTITLEMENT_CHECKED → WAITING_FOR_BRIEF → BRIEF_RECEIVED
→ FETCH_QUEUED → FETCHED → VALIDATED → EXTRACTION_PLANNED → EXTRACTING
→ READY_FOR_CAPABILITY → COMPLETED
                              (or REJECTED / FAILED / EXPIRED)
```

**The brief gate** is the cost control that matters most. An attachment with no
instruction is held at `WAITING_FOR_BRIEF` — not downloaded, not OCR'd, not sent
to a vision model. The only cost is one database row. A caption counts as a
brief, so "solve Q3" with a photo goes straight through.

**Four extraction methods**, always cheapest-first:

| Method | Cost | When |
|---|---|---|
| `DIGITAL_TEXT` | free | the PDF already has a text layer |
| `LOCAL_OCR` | free | Tesseract, tried in three segmentation modes, best kept |
| `VISION` | paid | only when OCR output fails its confidence check |
| `TRANSCRIPTION` | paid | voice notes |

**OCR is scored, not trusted.** Every result is measured for mean character
confidence, symbol-garbage ratio and minimum length — and **mathematics is held
to a higher bar than prose**, because a misread equation is worse for a student
than a slower answer. When the score fails, the page escalates and the reason is
written to the extraction row, so *"why did this request cost money"* is
answerable afterwards.

**Photo repair.** `media/enhance.py` measures focus (variance of the Laplacian),
brightness and contrast, then fixes what is fixable: EXIF rotation, bounded
resize, exposure lift, denoise, then sharpen — in that order, because sharpening
before denoising bakes noise into permanent artefacts that OCR reads as
punctuation. It says so honestly when a photo is beyond rescue rather than
returning the same blur.

**Extraction is content-addressed.** The same file sent twice is read once and
paid for once, scoped to its owner so identical bytes from two students never
cross.

---

## Mathematics

Most tutoring bots ask a language model to do arithmetic. This one does not.

```
student question → SymPy computes the exact answer → model explains it
```

`learning/mathsolver.py` handles **eight operations** — `SOLVE`,
`DIFFERENTIATE`, `INTEGRATE`, `SIMPLIFY`, `FACTOR`, `EXPAND`, `EVALUATE`,
`LIMIT` — and hands the model a result that is already correct, with an explicit
instruction not to recompute or contradict it.

Where it can, it **verifies by substitution**: roots are substituted back into
the equation, an integral is differentiated to check it returns the integrand.
The `verified` flag is only set when that check actually ran.

```
integrate (3x^3 + 3x^2) dx     →  3*x**4/4 + x**3 + C     [VERIFIED]
solve x^2 - 5x + 6 = 0         →  x = 2, x = 3            [VERIFIED]
expand (x+2)(x+3)              →  x**2 + 5*x + 6          [VERIFIED]
```

It reads what students actually type — `2x`, `3x^2`, `(x+2)(x+3)` — and LaTeX
too, so `\int (3x^3+3x^2)\,dx` routes the same way.

**Then it typesets the answer.** WhatsApp renders no mathematics, so
`x = (-b ± sqrt(b²-4ac))/2a` arrives as the exact string students misread.
Display equations are rendered to images with matplotlib's mathtext and sent
alongside the text.

**Never real LaTeX.** A LaTeX subprocess would execute model-authored markup,
and `\input{/etc/passwd}` turns a rendering step into arbitrary file access.
mathtext parses and draws; it never executes. Parsing reuses one hardened
function with a character filter, an exponent-bomb guard and a name whitelist —
`sympy.sympify` is called nowhere in this codebase.

---

## Subscriptions and payment

```
signup form → Cashfree order → payment → signed webhook → entitlement → WhatsApp template
```

**The webhook is the only thing that grants access.** The browser returning to a
success page proves nothing and grants nothing — that page asks the server,
which asks Cashfree.

- Callbacks are verified by HMAC over `timestamp + raw body`. The timestamp is
  inside the signed material, so a captured genuine callback cannot be replayed.
- Activation hangs on one column, `payments.activated_at`. Cashfree delivers at
  least once and the success page also triggers a check, so **two or three
  concurrent activations of one order is the normal case** — it grants one
  subscription and sends one message.
- Money is integer paise. Rupees as a float eventually charges somebody
  99.99999.
- `+91 99990 00001`, `919999000001` and `09999000001` normalise to one identity.
- Expiry is enforced **on read**, not by a sweeper, so a lapsed subscription
  stops granting access at the moment it lapses.

An unsubscribed student who messages the agent gets a subscribe link, not
silence — and costs nothing to refuse.

---

## The control plane

`apps/admin` — **20 pages, 24 permissions, 10 high-risk actions**.

| Section | Pages |
|---|---|
| Overview | Dashboard |
| People | Students · Tutors · **Grant subscription** |
| Activity | Conversations · Documents & RAG · Learning · Jobs |
| Configuration | Plans · Models & routing · Prompt versions · Feature flags |
| Governance | Costs · Audit log · Administrators |

**Grant a subscription from the panel** and the student gets the same WhatsApp
message a paying student gets — same tables, same supersede rule, same template.
Only `source` differs, which is what tells an operator later that no money was
involved.

**Every dangerous mutation** requires a typed reason of at least 8 characters
and an explicit confirmation, both re-checked server-side, and lands in an audit
log against the operator's account. The UI is not the control; the API is.

**RBAC is enforced twice**: the sidebar hides what a role cannot use, and the
server refuses it anyway with a real 403.

---

## Cost engineering

The product must be profitable at ₹100/month. Three independent ceilings:

| Level | Bounds |
|---|---|
| Request | tokens, attempts, output length, verifier passes |
| Student | daily and monthly spend, PDF pages, OCR pages, voice seconds, mocks |
| System | hourly and daily budget, per-provider budget, failure circuit |

**Ceilings derive from the subscription price**, not from a literal:

```python
SUBSCRIPTION_PRICE_USD_MICROS = 1_200_000   # ₹100
MODEL_SPEND_SHARE = 0.45                    # the rest is infra, fees, margin
```

A ceiling above revenue is decoration. These were once $20/month against $1.20
of income — 37× the price, so no student could reach them before the account had
lost money many times over.

**Velocity fires before the daily total.** A daily ceiling alone lets a retry
loop spend the whole day's budget in four minutes and only notice afterwards.

**Prompt caching is real.** The stable half of the system prompt — safety rules,
tutor identity, persona — is marked cacheable and sent once, not twice.

---

## Security

**Every public endpoint states what stops it being abused.**

| Endpoint | Guard |
|---|---|
| `POST /webhooks/whatsapp` | HMAC-SHA256 over the raw body; fails closed with no secret |
| `POST /webhooks/cashfree` | HMAC over `timestamp + body`, base64; fails closed |
| `POST /public/signup` | validation before any gateway call; one row per attempt |
| `GET /public/orders/{id}` | reveals only paid/not-paid; confirms against Cashfree |
| `POST /internal/*` | Google OIDC token, or the shared secret |

- **Rejected webhooks return 200.** Meta and Cashfree retry non-2xx, so a 403
  invites a forged payload back on a backoff schedule.
- **Untrusted markup never executes.** SymPy parsing is filtered and whitelisted;
  mathtext draws rather than runs; no `eval`, no `sympify`, no LaTeX subprocess.
- **`X-Forwarded-For` is not trusted by default.** Its leftmost entry is
  client-controlled — taking it turns a login throttle into unlimited password
  guesses. `TUTORTWIN_TRUSTED_PROXY_HOPS` says how many proxies are real.
- **Admin sessions** use hashed tokens, CSRF headers, Argon2id passwords,
  lockout after failed attempts, and revocation on password change.
- **CSP with a per-request nonce** on the control plane; no `unsafe-inline` for
  scripts.
- **Student message text never reaches logs** unless explicitly enabled.

---

## The shared database

Production runs on a database **shared with another product**. `public` holds 61
tables that are not ours — including its own `alembic_version` and a
`prompt_versions` table whose name TutorTwin also uses.

TutorTwin owns `tutor_twin` and nothing else, reached through a per-connection
`search_path` of `tutor_twin,public` — ours first so `CREATE` lands there,
`public` second so the `vector` extension type still resolves.

Two guards make that safe:

- **`db/migration_guard.py`** hides every foreign table from autogenerate.
  Without it, a routine `alembic revision --autogenerate` writes `op.drop_table`
  for 61 tables belonging to somebody else, and the next `upgrade head` runs it.
- **A boot-time check** resolves every declared table through `::regclass` — the
  same way Postgres resolves an unqualified name in a real query — and refuses
  to serve if any lands outside `tutor_twin`. A table missing from our schema
  would silently read another product's rows: nothing errors, the data is just
  wrong, in the direction of a privacy incident.

---

## Architecture

```
src/tutortwin/
├── api/              routes: events, whatsapp, public, jobs, admin/*
│   └── dependencies.py   the composition root — every adapter chosen here
├── orchestration/    entry_service.py — TX1 → provider call → TX2
├── domain/           events, models, capabilities, budget, media, provider
├── policies/         budget_policy, retry_policy
├── capabilities/     executor, placeholder
├── learning/         11 modules — solver, verification, visuals, mathsolver…
├── media/            pipeline, extractor, ocr, enhance, audio, pdf, validation
├── rag/              chunking, embeddings, ingestion, vector_store
├── integrations/     whatsapp/{client,webhook}, cashfree
├── services/         entitlements, subscriptions, retention, prompts
├── providers/        gateway, registry, anthropic/openai adapters
├── repositories/     conversations, media, admin, learning, knowledge
├── security/         auth, passwords, sessions
└── db/               42 tables across 5 model modules
```

**No transaction is held across a network call.** The entry service commits TX1,
calls the provider with nothing held, then opens TX2 — because holding a
Postgres connection for the full model latency is how a connection pool dies
under load.

---

## Running it locally

**Requirements:** Python 3.12 · Node 20.9+ · PostgreSQL 18 with `pgvector` ·
Tesseract 5 (optional, strongly recommended)

```bash
# Tesseract — without it every photo costs a vision call
winget install UB-Mannheim.TesseractOCR        # Windows
apt-get install tesseract-ocr tesseract-ocr-eng # Debian
```

```bash
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"
cp .env.example .env          # then fill it in
.venv/Scripts/python -m alembic upgrade head
.venv/Scripts/python -m tutortwin              # API

cd apps/site  && npm install && npm run dev    # :3200
cd apps/admin && npm install && npm run dev    # :3100
```

Create the first administrator:

```bash
python -m tutortwin.cli.admin hash-password    # paste into .env
python -m tutortwin.cli.admin bootstrap
```

**The admin panel is the whole app at the root** — `/`, `/students`, `/tutors`,
`/subscriptions`, `/costs`, `/audit`. There is no `/admin` path: `(dashboard)`
is a Next.js route group and does not appear in URLs.

---

## Configuration

`.env` holds **all 65 settings the code reads**, in thirteen numbered sections,
one comment line per key.

The two that decide whether anything works:

```bash
TUTORTWIN_ANTHROPIC_API_KEY=      # preferred when both are set
TUTORTWIN_OPENAI_API_KEY=         # also the ONLY vendor that does speech
```

With neither set there is no model gateway: every reply is *"TutorTwin is being
set up"* and zero paid calls are made. That is a valid state, not a crash.

---

## Testing

```bash
.venv/Scripts/python -m pytest tests/          # 816 tests
.venv/Scripts/python -m mypy src/tutortwin/    # 115 files
.venv/Scripts/python -m ruff check src/ tests/
cd apps/admin && npx playwright test           # 14 e2e
```

Integration tests need a local PostgreSQL and run against `tutortwin_test`,
never the shared production database — `tests/conftest.py` pins the schema to
`public` for exactly that reason.

Chaos, load, threat-model and cost-ceiling suites are included.
**The suites make zero paid AI provider calls.**

---

## Deploying

Three hosts, deliberately separate — the admin session cookie must not share an
origin with a page that loads Cashfree's third-party checkout script:

```
nxtutortwin.nxtutors.com      apps/site     no login
tutortwinadmin.nxtutors.com   apps/admin    login
api.nxtutors.com              FastAPI       webhooks + APIs
```

`.github/workflows/` holds a deploy workflow per piece. Each builds and tests in
CI, ships over SSH, restarts, and **verifies the service is actually serving** —
the API workflow also checks the Meta webhook still answers, because a 502 there
is a student's paid answer lost.

See **[`docs/tutortwin/GO_LIVE.md`](docs/tutortwin/GO_LIVE.md)** for DNS records,
the Meta message templates you must get approved, the Cashfree webhook, and the
order to do it in. Template approval takes 24–48 hours — start it first.

---

## Third-party services

Exactly six, and no more:

| Service | For | Notes |
|---|---|---|
| Meta WhatsApp Cloud API | messages, media | the channel |
| OpenAI | text, vision, embeddings, **speech** | only vendor that transcribes |
| Anthropic | text, vision | preferred for tutoring |
| Cashfree | payments | ₹100 / 30 days |
| Cloudflare R2 | media storage | deploy only; S3 protocol, **not** AWS S3 |
| Google Cloud Tasks | job dispatch | deploy only, serverless target |

**No OCR vendor. No maths vendor. No search vendor.** OCR is Tesseract plus a
vision model already paid for; mathematics is SymPy.

No OCR is 100% accurate — not Tesseract, not any vision model, not any paid API,
and least of all on handwriting. This design measures its own confidence and
escalates rather than pretending otherwise.

`RESEARCH_HELP` answers from model knowledge and your own ingested documents. It
does **not** browse the web; live search would be a genuine seventh vendor and a
product decision, not a gap.

---

## Documentation

| Document | Contents |
|---|---|
| [`GO_LIVE.md`](docs/tutortwin/GO_LIVE.md) | DNS, Meta templates, Cashfree webhook, order of operations |
| [`PHASE_08_WHATSAPP_AND_MEDIA.md`](docs/tutortwin/PHASE_08_WHATSAPP_AND_MEDIA.md) | the Meta integration and media pipeline audit |
| [`ADMIN_PANEL.md`](docs/tutortwin/ADMIN_PANEL.md) | the control plane |
| [`RUNBOOK.md`](docs/tutortwin/RUNBOOK.md) | operating it |
| [`COST_CONTROLS.md`](docs/tutortwin/COST_CONTROLS.md) | the three levels of ceiling |
| [`SECURITY.md`](docs/tutortwin/SECURITY.md) | threat model |
| [`API.md`](docs/tutortwin/API.md) | endpoint reference |

---

<sub>TutorTwin is an AI study assistant. It is not a human teacher, it is named
after one at the student's choosing, and it never claims to be that person.</sub>
