# TutorTwin Media Pipeline

Status: Phase 03. This document describes running code.

## The invariant

> **NO BRIEF → NO EXPENSIVE PROCESSING**

For IMAGE, PDF and DOCUMENT arriving without a usable instruction, TutorTwin
performs **zero** downloads, OCR, embeddings, vision calls and transcriptions.
It writes one row and asks what the student wants done.

This is structural, not advisory: the state machine has no edge from
`WAITING_FOR_BRIEF` to `FETCH_QUEUED`. Calling `process()` directly on unbriefed
media returns without fetching.

## State machine

```
RECEIVED_REFERENCE      a pointer, no bytes, no cost
  → ENTITLEMENT_CHECKED plan verified BEFORE anything is fetched
    → WAITING_FOR_BRIEF held indefinitely at zero cost
      → BRIEF_RECEIVED  the student said what they want
        → FETCH_QUEUED  first byte of network cost
          → FETCHED
            → VALIDATED MIME sniffed, limits enforced
              → EXTRACTION_PLANNED   pages and methods chosen locally
                → EXTRACTING
                  → READY_FOR_CAPABILITY
                    → COMPLETED

Terminal: REJECTED · FAILED · EXPIRED
```

Transitions are explicit (`_ALLOWED` adjacency) and **idempotent** — a
self-transition is always legal, so a retried job is harmless. Anything not
listed raises `InvalidTransition`.

`ZERO_COST_STATES` names the states at which nothing has been spent; the cost
tests assert against it directly.

## Audio is deliberately exempt from the brief gate

A voice note **is** the request — there is nothing to ask the student to
clarify. `MEDIA_MESSAGE_TYPES` therefore excludes `AUDIO`. Entitlement, size and
duration caps still apply before a single second is transcribed.

## PDF pipeline

Local-first, cheapest method always tried before a costlier one:

| Method | Cost | When |
|---|---|---|
| `DIGITAL_TEXT` | free | the page already has a usable text layer |
| `LOCAL_OCR` | our own CPU | scanned page, Tesseract |
| `VISION` | a frontier model | local extraction cannot answer |

1. Validate (magic bytes, size).
2. Inspect: page count checked **before** text extraction, so a 5000-page
   document is refused without parsing all of it.
3. Extract the text layer page by page with PyMuPDF.
4. Score usability: `MIN_USABLE_CHARS` (80) and `MIN_ALNUM_RATIO` (0.35). The
   ratio catches broken embedded fonts that decode to punctuation soup — long
   enough to pass a length check, useless to a model.
5. Target pages from the brief.
6. OCR only the selected scanned pages.
7. Escalate to vision only where OCR output is not usable.
8. Cache by content hash + parser version.
9. Record provenance for every page.

### Page targeting

**Deterministic** when the brief names pages: `"explain the graph on page 13"`
selects page 13 out of twenty. Ranges (`"pages 2-3"`) work too.

Otherwise **local BM25-ish keyword scoring** over the extracted text. No
embeddings: they cost money to answer "which page mentions quadratics", which
term overlap already answers well at this document size. Question numbers get a
boost, matched against the raw page text where `4.` survives tokenisation.

With no signal at all, a short document is read whole; a long one takes the
first `max_pages_processed` pages.

**The page budget is applied at planning time**, so executing a plan cannot
overspend. A 100-page PDF never reaches a frontier model because one equation
was asked about.

## OCR and honest escalation

Tesseract is genuinely unreliable for handwriting and mathematical notation, and
pretending otherwise would ship confident nonsense. `assess()` judges the output:

| Signal | Verdict |
|---|---|
| empty output | `ocr_returned_nothing` |
| under 25 characters | `ocr_output_too_short` |
| >45% non-alphanumeric | `ocr_output_mostly_symbols` |
| mean confidence < 0.55 | `ocr_confidence_below_threshold` |
| maths present and confidence < 0.75 | `math_notation_needs_visual_verification` |

Only a failed assessment justifies a vision call, and the reason is recorded on
`ExtractionResult.escalation_reason`.

**Tesseract is optional.** `TesseractOCRProvider.available` reports `False` when
the binary is absent, and the planner routes those pages to vision instead of
crashing. This machine has no Tesseract; the tests cover both paths.

## Image pipeline

Entitlement → brief → validate → dimension/bomb guard → resize and contrast →
optional OCR → vision only as needed.

## File security

- **MIME is sniffed from magic bytes**, never trusted from the filename or the
  sender's claimed type — both are attacker-controlled.
- Executables rejected by magic (`MZ`, `ELF`, Mach-O, shebang) whatever the
  extension says.
- Archives rejected: a container's real contents are unknown until expanded, and
  expanding it is exactly the decompression-bomb surface we decline to have.
- A declared-vs-actual **category** mismatch is a rejection.
- Filenames sanitised: basename under both separators, NFKD-normalised to ASCII,
  non-alphanumerics replaced, leading dots stripped **after** substitution (so
  `"  ..hidden"` cannot survive as `"__..hidden"`).
- Image dimensions and total pixels checked from the header, so a hostile 5KB
  PNG claiming 60000×60000 is refused without decoding.

## Limits

| Limit | Default |
|---|---|
| image / PDF / audio / document bytes | 8 / 20 / 16 / 8 MB |
| PDF pages (reject beyond) | 200 |
| pages processed per request | 5 |
| OCR pages per request | 3 |
| vision pages per request | 2 |
| audio duration | 300 s |
| image pixels / dimension | 40 M / 12 000 px |

## Storage

`BlobStore` with a filesystem fake and a Cloudflare R2 adapter. R2 speaks an
S3-compatible protocol, but that is confined to one module — nothing outside it
imports boto3 or knows the bucket exists. **No AWS S3.**

Objects are **content-addressed**: `media/<subject>/<aa>/<sha256>.<ext>`.

- Re-uploading identical content is free and idempotent.
- Ownership is encoded in the key, so a cross-subject read is refused at the
  storage layer without a lookup.
- The same file from two students is stored twice, deliberately — a shared key
  would let one student's deletion affect another's.
- Private bucket, signed short-lived URLs, 7-day default retention on raw
  uploads. The extraction is what has lasting value; the original bytes are a
  liability that ages badly.

## Cache

`media_extractions`, keyed by `(subject, sha256, parser_version, page, method)`.

**Owner-scoped by design.** Identical bytes belonging to two students are two
private documents; serving one from the other's cache would be a cross-student
leak. The test asserts two OCR runs for the same file sent by two students.

## Jobs

`jobs` table: type, state, attempts, `max_attempts`, idempotency key, next
retry, last error, owner, correlation id.

- Cloud Tasks payload is **only a job id** — the worker reads all durable state
  from Postgres, so a retry cannot act on a stale snapshot.
- `uq_job_idempotency` means a redelivered media event cannot create a second
  job.
- `POST /internal/jobs/run` requires authentication (OIDC in production, shared
  secret locally) and rejects extra payload fields.
- No Celery, no Redis, no permanently running worker — the service still scales
  to zero.

## Media source

Phase 03 ships local/test sources only (`InMemoryMediaSource`,
`LocalFileMediaSource`). WhatsApp media fetching arrives in Phase 08 with the
Lead Intake bridge; adding it now would mean guessing at an API this phase is
forbidden to inspect.
