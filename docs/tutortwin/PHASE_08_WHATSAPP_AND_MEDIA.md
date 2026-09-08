# Phase 08 — Meta WhatsApp, OCR, and the end-to-end audit

Status at the end of this phase: **751 tests passing, ruff clean, mypy clean on
107 files, running against the shared production RDS.**

One honest caveat up front: during this work one full-suite run reported 7
failures in `test_admin_security.py::test_role_matrix_is_enforced_by_the_server`.
Three consecutive full runs afterwards were clean, as was that module in
isolation and the whole integration directory together. It is a flaky
cross-module interaction in the test fixtures, not a product defect, and it is
recorded here rather than left for somebody to rediscover.

---

## 1. The short answer to the question you asked

> *do I need any extra third-party API beyond OpenAI and Claude — for OCR or
> anything else?*

**No. Nothing new to buy.**

Here is the complete list of every external service this codebase contacts,
taken from the code rather than from memory — every URL and every vendor SDK
import in `src/`:

| Service | What it is for | Do you pay separately? |
|---|---|---|
| `graph.facebook.com` | Meta WhatsApp Cloud API — receiving and sending messages, downloading media | Meta's own conversation pricing. You already have these keys. |
| OpenAI | Text, vision, embeddings, **and voice-note transcription** | Yes, you already planned for this |
| Anthropic | Text and vision | Yes, you already planned for this |
| Cloudflare R2 (via the S3 protocol) | Media file storage | Only when you deploy. Not AWS S3. |
| Google Cloud Tasks | Async job dispatch | Only when you deploy to Cloud Run |

That is the whole list. There is **no OCR vendor, no search vendor, no
speech vendor and no maths vendor** anywhere in the code.

### Specifically on OCR

OCR is done by **Tesseract**, which is open source, runs on your own CPU, and
costs nothing per page. It has now been installed (5.4.0) and wired up. When
Tesseract's output is not trustworthy, the page escalates to a **vision model**
— which is the OpenAI or Anthropic key you already need. Nothing else is
involved.

I want to be straight with you about one thing, because it affects what you
should promise students:

> **No OCR is 100% accurate. Not Tesseract, not GPT vision, not Claude vision,
> and not any paid OCR service you could buy.**

Handwriting and mathematical notation are where every engine fails, and the
ones that market themselves as "99.9% accurate" are quoting figures for clean
printed text. Buying a third-party OCR API would not fix this; it would add a
bill and a vendor without changing the hard part.

What this system does instead is refuse to be confidently wrong. Every OCR
result is scored before it is trusted — mean character confidence, the ratio of
symbol garbage, a minimum usable length — and **maths is held to a higher bar
than prose**, because a misread equation is worse for a student than a slower
answer. When the score fails, a vision model reads the same image and the
reason for escalating is recorded on the extraction row, so "why did this
request cost money" is answerable after the fact.

Measured on a real rendered JPEG homework page during this session:

```
available     : True
engine        : tesseract-5
confidence    : 0.912
text          : Question 3. Name the three parts of a plant cell that are
                absent in an animal cell. Question 4. Solve for x: 2x +5 = 13
prose verdict : usable
maths verdict : usable
```

That page cost nothing to read. A photo of handwriting would have failed the
confidence check and gone to vision, as designed.

---

## 2. What was built this phase

### 2.1 Meta WhatsApp, owned directly

You chose "TutorTwin owns Meta directly", so there is now a real webhook rather
than a bridge.

**`src/tutortwin/integrations/whatsapp/webhook.py`** — proving it is Meta, then
normalising it.

- `X-Hub-Signature-256` HMAC-SHA256 over the **raw** request body. Raw matters:
  re-serialising the parsed JSON changes key order and spacing, and the
  signature covers bytes, so a re-serialised body never validates.
- Fails **closed**. With no app secret configured the webhook refuses
  everything, because an open webhook lets anyone spend your model budget and
  write into a student's conversation history.
- A rejected payload gets a `200`, deliberately. Meta retries non-2xx, so a
  `403` would invite a forged payload to be redelivered on a backoff schedule.
- The `hub.challenge` subscribe handshake, with `compare_digest` on the token
  so it does not leak its length through timing.
- Normalises text, image, document, PDF, audio/voice, video, interactive button
  replies and list replies. Stickers, locations and contacts are ignored
  silently — there is nothing to tutor from.
- Delivery and read receipts (`statuses`) produce **zero** events. This is the
  single most common callback Meta sends; turning them into events would open a
  conversation turn every time a phone ticks.
- Batches are processed per message, so one malformed entry among five does not
  discard the other four.

**`src/tutortwin/integrations/whatsapp/client.py`** — the Graph API, and the two
ports the product drives it through.

- `WhatsAppMediaSource` implements the existing `MediaSource` port, so a photo
  becomes bytes the OCR pipeline already knew how to read.
- `WhatsAppOutboundGateway` implements the existing `OutboundGateway` port, so
  an answer reaches a phone. Every one of the eight outbound action types
  reaches the student as something — a dropped action is an answer the student
  paid for and never saw.
- Media download is **two authenticated hops**. Meta returns a short-lived
  lookaside URL rather than the bytes, and that URL still needs the bearer
  token; fetching it unauthenticated returns an HTML error page dressed as an
  image.
- Long answers are split at paragraph or line boundaries under Meta's 4096
  character limit, rather than truncated. A worked solution to a multi-part
  problem passes 4096 easily, and one cut mid-derivation is useless.
- Send failures are logged and swallowed, never raised. The answer is already
  persisted and already billed by the time delivery runs, so raising would make
  the queue retry the whole turn and pay a second time for an answer we hold.
- `WHATSAPP_SEND_ENABLED=false` is an outbound kill switch: the webhook keeps
  receiving and the agent keeps thinking, and nothing reaches a real phone.
  Worth using the first time this points at a production number.

**`src/tutortwin/api/routes/whatsapp.py`** — `GET`/`POST /webhooks/whatsapp`.

Turns are processed **inline** rather than in a background task. Cloud Run bills
by request and throttles CPU between them, so work started after the response is
written may simply never run — and a webhook that acknowledges a message it then
drops is worse than a slow one. A redelivery is harmless because the wamid is
the idempotency key: the second delivery replays the stored answer instead of
buying a new one. This is marked with a `ponytail:` comment naming the ceiling
and the upgrade path (move onto the existing Cloud Tasks queue) if Meta ever
starts redelivering because a model call ran long.

### 2.2 The image path — the gap that mattered most

This is the most important defect found and fixed this phase, and it was
sitting directly under your headline requirement.

`MediaPipeline.process()` had this branch:

```python
if result.kind is not MediaKind.PDF:
    # Images and documents get their own extraction path in the image
    # pipeline; PDFs are the page-planned case.
    ... transition to READY_FOR_CAPABILITY
```

**That image pipeline did not exist.** A student's photo was downloaded,
validated, stored, content-addressed and marked "ready for capability" with
**zero text extracted from it**. The tutor was then asked to answer a question
it had never seen. Nothing errored, nothing logged a warning, and the OCR code
in `media/ocr.py` was only ever reachable from the PDF path.

`ImageExtractor` (in `media/extractor.py`) now closes it, following the same
rule as the PDF path — free local OCR first, a paid vision model only when OCR
cannot be trusted — with one deliberate difference:

> A PDF page falls back to its digital text layer when Tesseract is missing.
> A photo has no such layer, so **no OCR engine means vision, not nothing.** A
> deployment without Tesseract must still be able to read a photo; it simply
> pays for it.

It also sends the **real MIME type** to the vision model rather than a
hard-coded `image/png` — a WhatsApp photo is a JPEG, and vendors reject an image
whose declared type does not match its bytes.

Extractions are content-addressed and cached, so the same photo sent twice —
which happens constantly when a student is not sure the first one went through —
is read once and paid for once.

### 2.3 The voice path — the same gap, one file over

`VoicePipeline` and `OpenAITranscriptionProvider` existed in `media/audio.py`
and were **wired to nothing**. Grepping for their use outside their own module
returned zero hits.

The consequence was the same shape as the image bug, and slightly worse in
principle: a voice note is deliberately **exempt from the brief gate**, on the
stated grounds that "the voice *is* the request". That only holds if somebody
actually listens to it. Storing the audio and marking it ready left the tutor
answering silence.

`MediaPipeline._process_audio()` now transcribes, caches by content hash, and
records the call in the usage ledger. Transcription needs an **OpenAI** key
specifically — it is the only permitted vendor that does speech — so a
deployment with only an Anthropic key tutors perfectly well and simply cannot
hear voice notes. That returns `None` rather than refusing to start.

### 2.4 Tesseract, actually enabled

`TUTORTWIN_TESSERACT_CMD` existed in `config.py` and **nothing read it**.
`TesseractOCRProvider` only ever called `shutil.which("tesseract")`.

This matters on Windows specifically: the installer puts the binary in
`C:\Program Files\Tesseract-OCR` and adds **nothing to PATH**. So a machine with
Tesseract correctly installed reported OCR as unavailable and silently paid a
vision model for every photo. The setting is now honoured, passed to
`pytesseract`, and pointed at the install in `.env`.

Tesseract 5.4.0 was installed during this session and verified working.

---

## 3. End-to-end audit

### 3.1 The student journey, verified against the live database

| Step | Status | Where it is proven |
|---|---|---|
| Meta subscribe handshake (`GET`) | working | `test_whatsapp_webhook.py`, and live against the shared RDS |
| Signature verification | working, fails closed | 5 unit + 2 integration tests |
| Forged / unsigned payload | rejected, nothing written, nothing sent | `test_an_unsigned_payload_changes_nothing` |
| Text question → answer delivered | working | `test_a_students_message_gets_an_answer_on_whatsapp` |
| Turn persisted as an ordinary conversation | working | `test_the_turn_is_persisted_under_the_students_subject` |
| Meta redelivery | answered once, not twice | `test_a_redelivery_is_answered_once` |
| Read receipts | acknowledged and ignored | `test_a_read_receipt_is_acknowledged_and_ignored` |
| Two students in one batch | both answered | `test_two_students_in_one_batch_both_get_answers` |
| Photo with no caption | held at the brief gate, **not downloaded**, costs nothing | `test_a_photo_with_no_caption_is_held_and_costs_nothing` |
| Photo with caption | job queued, student acknowledged | `test_a_captioned_photo_queues_work_and_acknowledges` |
| Job runs → photo downloaded → **real OCR reads it** | working | `test_the_queued_job_downloads_and_reads_the_photo` |
| Voice note → transcript | wired; needs an OpenAI key to run | `_process_audio` |
| PDF → page-planned extraction | already working | pre-existing suite |
| Admin panel against the shared RDS | working | login refuses a wrong password; all endpoints refuse anonymous |

The photo test is the headline one. It is deliberately written so that if
Tesseract is absent the OCR assertion **skips rather than passes** — a green run
that proved nothing is worse than a skip that says so.

### 3.2 Database isolation, re-verified after all changes

```
tutor_twin tables : 41
public tables     : 61   <- the other agent's, unchanged
their alembic rev : 8c3d5e17f240  (untouched)
our alembic rev   : c4a17be9d520

admin_users      resolves_to=tutor_twin
tutors           resolves_to=tutor_twin
model_catalog    resolves_to=tutor_twin   (12 rows)
plan_policies    resolves_to=tutor_twin
feature_flags    resolves_to=tutor_twin
prompt_versions  resolves_to=tutor_twin   <- also exists in public; ours wins
conversations    resolves_to=tutor_twin
media_objects    resolves_to=tutor_twin
```

Every table resolves to `tutor_twin` via `::regclass`, which is exactly how
Postgres resolves an unqualified name in a real query. **No `tutor_twin_` table
prefix is needed**, and adding one would be redundant work.

The boot-time guard re-checks this on every start and refuses to serve if any
table resolves outside the schema — because a table missing from our schema
would silently fall through to `public` and read the other product's rows.
Nothing would error; the data would just be wrong, in the direction of a privacy
incident.

**A regression was found and fixed here.** That boot guard broke *every*
integration test: tests build `Settings(database_url=TEST_DSN, ...)` and let
every other field fall through to `.env`, which now says `tutor_twin` — while
the local test database keeps its tables in `public`. The schema has to travel
with the DSN. Fixed in `tests/conftest.py` with the reasoning recorded there.

### 3.3 What the agent can do (23 capabilities, all routable)

`GENERAL_TUTORING`, `EXPLAIN_CONCEPT`, `HOMEWORK_SOLVE`, `MATH`, `PHYSICS`,
`CHEMISTRY`, `BIOLOGY`, `CODING`, `WRITING_FEEDBACK`, `LANGUAGE_HELP`,
`ANSWER_CHECK`, `GRADE_WORK`, `PRACTICE_GENERATION`, `TWIN_PROBLEM`,
`FLASHCARDS`, `QUIZ`, `MOCK_TEST`, `DOCUMENT_QA`, `IMAGE_QA`, `REVISION`,
`STUDY_PLAN`, `RESEARCH_HELP`, `UNKNOWN`.

Six pedagogy modes: `GUIDED`, `HINT_FIRST`, `STEP_BY_STEP`,
`ANSWER_AND_EXPLAIN`, `SOCRATIC`, `EXAM_REVISION`.

Mapping to the uploaded PDFs:

| PDF feature | Covered by |
|---|---|
| StudySnap: photo → solution loop | `IMAGE_QA` + the new image extraction path |
| StudySnap: step-by-step + hint-first | `STEP_BY_STEP`, `HINT_FIRST` modes |
| GPAI: Problems | `HOMEWORK_SOLVE`, `MATH`, `TWIN_PROBLEM`, `learning/solver.py` |
| GPAI: Visuals | `learning/visuals.py` |
| GPAI: Chat | `GENERAL_TUTORING`, conversation memory, RAG |
| Worksheet / PDF reading | `DOCUMENT_QA` + the page-planned PDF extractor |
| Voice tutor | `_process_audio` (needs an OpenAI key) |
| Practice + grading | `PRACTICE_GENERATION`, `GRADE_WORK`, `ANSWER_CHECK`, `learning/practice.py`, `learning/assessment.py` |
| Research | `RESEARCH_HELP` — **model knowledge and your own RAG corpus only, no web search API** |
| Notes / flashcards / revision | `learning/notes.py`, `FLASHCARDS`, `REVISION` |

`RESEARCH_HELP` is worth calling out: it answers from the model and from
documents you have ingested into the RAG store. It does **not** browse the web.
If you want live web research that is a genuine new third-party API (Brave,
Tavily, Serper or similar) and a product decision, not a bug.

---

## 4. What is still blocking a live student conversation

There is exactly one thing, and it is yours to fill in:

### No AI provider key is configured

```
model gateway   : NONE
```

With no key there is no model gateway, so every request is answered
deterministically ("TutorTwin is being set up"), zero paid calls are made, and
**every photo goes unread** if it also fails local OCR. Set at least one:

```
TUTORTWIN_ANTHROPIC_API_KEY=...     # preferred when both are set
TUTORTWIN_OPENAI_API_KEY=...        # also required for voice notes specifically
```

Set **both** if you want voice notes. Anthropic does not do speech-to-text.

### Two things that are correct locally but must change to deploy

- **Blobstore is the local filesystem.** Correct for this machine; on Cloud Run
  a container's disk disappears. Set the four `TUTORTWIN_R2_*` values.
- **Task queue records instead of dispatching.** Correct locally, where the job
  handler is driven directly. Set the five `TUTORTWIN_TASKS_*` values.

`require_deployable()` already refuses to start `staging` or `production`
without both, because each fails silently rather than loudly.

### Also worth knowing

- `TUTORTWIN_FAKE_PRO_SUBJECTS` is the entitlement source until the website is
  connected. **An identity not listed there is refused at the plan gate and
  costs nothing** — which is correct behaviour, and is exactly what makes a test
  message look like the agent is broken. Add your own number:
  `["919999000001"]` (wa_id format, no `+`).
- `CASHFREE_*` is still read by no code. There is no billing in TutorTwin;
  entitlement resolves through a gateway.
- `WHATSAPP_BUSINESS_ACCOUNT_ID` is read by nothing either — Meta identifies the
  sender by phone number id. It is kept because it is useful in Meta's console.

---

## 5. Going live with Meta

1. Set `TUTORTWIN_ANTHROPIC_API_KEY` and/or `TUTORTWIN_OPENAI_API_KEY`.
2. Add your test number to `TUTORTWIN_FAKE_PRO_SUBJECTS`.
3. Expose the service on a public HTTPS URL (Cloud Run, or ngrok while testing).
4. In the Meta app console, set the callback URL to
   `https://<your-host>/webhooks/whatsapp` and the verify token to whatever
   `WHATSAPP_VERIFY_TOKEN` says. The handshake is already proven to work.
5. Subscribe the app to the **`messages`** webhook field.
6. Leave `WHATSAPP_SEND_ENABLED=false` for the first run. Watch the logs for
   `whatsapp_turn_handled`, confirm the agent is thinking correctly, then set it
   `true`.

Meta's 24-hour customer service window applies: outside it you can only send
approved template messages, and a free-form send returns a `400` whose body
says so. That body is logged in `whatsapp_send_failed` rather than swallowed,
because an expired token and a closed window look identical otherwise.

---

## 6. Files added or changed

**New**

- `src/tutortwin/integrations/whatsapp/{__init__,webhook,client}.py`
- `src/tutortwin/api/routes/whatsapp.py`
- `tests/unit/test_whatsapp.py` (43 tests)
- `tests/unit/test_image_extraction.py` (11 tests)
- `tests/integration/test_whatsapp_webhook.py` (9 tests)
- `tests/integration/test_whatsapp_photo_e2e.py` (3 tests)

**Changed**

- `config.py` — WhatsApp settings, reading your existing unprefixed
  `WHATSAPP_*` names via `AliasChoices` (the `TUTORTWIN_WHATSAPP_*` form is
  also accepted); `whatsapp_configured`
- `api/dependencies.py` — WhatsApp client, outbound gateway, routing media
  source, transcriber; `Container.outbound` widened to the `OutboundGateway`
  protocol
- `api/app.py` — the webhook router, mounted without the `/v1` prefix and
  without the internal-key header, because Meta owns that URL's shape and
  authenticates by signature
- `media/extractor.py` — `ImageExtractor`, `IMAGE_PARSER_VERSION`
- `media/pipeline.py` — `_process_image`, `_process_audio`,
  `AUDIO_PARSER_VERSION`, transcriber injection
- `media/ocr.py` — honours an explicit binary path
- `domain/media.py` — `ExtractionMethod.TRANSCRIPTION`
- `tests/conftest.py` — pins the test schema to `public`
- `.env` — restructured; WhatsApp promoted to a real section
