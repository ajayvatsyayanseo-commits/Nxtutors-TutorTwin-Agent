# Phase 03 Acceptance Report

Multimodal media intake: PDF, image and voice, with hard cost gates. All
commands below were executed; output is verbatim.

## Environment

| Component | Version |
|---|---|
| OS | Windows 11 (win32) |
| Python | 3.12.10 |
| PostgreSQL | 18.1 (local) |
| PyMuPDF | 1.28.2 |
| Pillow | 12.3.0 |
| filetype | 1.2.0 |
| pytesseract | 0.3.13 |
| **Tesseract binary** | **not installed on this machine** |

The missing Tesseract binary is a genuine constraint, not an oversight: OCR is
capability-gated throughout, and both the available and unavailable paths are
tested. See Limitations.

## 1. Static checks

```bash
$ ruff check src tests migrations scripts
All checks passed!

$ ruff format --check src tests migrations scripts
83 files already formatted

$ mypy
Success: no issues found in 60 source files
```

## 2. Tests

```bash
$ pytest
281 passed in 29.09s

$ pytest -m "not integration"
208 passed, 73 deselected in 4.75s

$ pytest --cov --cov-report=term
TOTAL    3029    365    482    67    86%
281 passed
```

Coverage of the new subsystems:

| Module | Coverage |
|---|---|
| `media/pipeline.py` | 95% |
| `domain/media.py` | 93% |
| `media/extractor.py` | 91% |
| `repositories/media.py` | 80% |
| `media/pdf.py` | 77% |
| `media/validation.py` | 70%+ |

Lower figures on `blobstore.py`, `adapters.py` and `ocr.py` are the R2, Cloud
Tasks and Tesseract production paths, which cannot execute without those
services. Their local counterparts are fully covered.

## 3. Mandatory scenarios

All fifteen, in `tests/integration/test_media_pipeline.py`, asserting exact
counters from the fake source, OCR engine and provider:

| # | Requirement | Test | Downloads / OCR / Vision | Result |
|---|---|---|---|---|
| 1 | non-Pro PDF → nothing | `test_non_pro_pdf_downloads_nothing` | **0 / 0 / 0** | PASS |
| 2 | Pro PDF no brief → WAITING | `test_pro_pdf_without_brief_waits_at_zero_cost` | **0 / 0 / 0** | PASS |
| 3 | brief says page 13 | `test_brief_targets_the_named_page` | 1 / 0 / 0, page **[13]** of 20 | PASS |
| 4 | digital PDF → no OCR | `test_digital_pdf_never_runs_ocr` | 1 / **0** / **0** | PASS |
| 5 | scanned page → OCR that page | `test_scanned_page_ocrs_only_that_page` | 1 / **1** / 0 | PASS |
| 6 | OCR sufficient → no vision | same test | 1 / 1 / **0** | PASS |
| 7 | OCR insufficient → one vision | `test_poor_ocr_escalates_once_to_vision` | 1 / 1 / **1** | PASS |
| 8 | image no brief → nothing | `test_image_without_brief_costs_nothing` | **0 / 0 / 0** | PASS |
| 9 | image + "solve Q4" | `test_image_with_brief_runs_the_pipeline` | 1 / 0 / 0 | PASS |
| 10 | duplicate event → one job | `test_duplicate_media_event_creates_one_job` | 1 job, not 2 | PASS |
| 11 | cached extraction → no re-OCR | `test_cached_extraction_skips_ocr_the_second_time` | OCR stays **1** | PASS |
| 12 | oversized → reject early | `test_oversized_file_is_rejected_before_any_processing` | **0 / 0** after fetch | PASS |
| 13 | malformed PDF → controlled | `test_malformed_pdf_fails_in_a_controlled_way` | **0 / 0** | PASS |
| 14 | voice non-Pro → no transcription | `test_non_pro_voice_makes_zero_transcriptions` | **0 transcriptions** | PASS |
| 15 | eligible voice → one | `test_eligible_voice_transcribes_exactly_once` | **1 transcription** | PASS |

Additional adversarial coverage: brief-arrives-later resumes the same media,
`process()` refuses unbriefed media, vision refused when budget forbids it,
executable disguised as PDF, OCR-unavailable degrades to vision, extraction
cache is owner-scoped, overlong and oversized voice, job endpoint authentication.

## 4. Migrations

```bash
$ alembic upgrade head
INFO  Running upgrade 71e302352594 -> 51d0cb4c3013, phase 03 media pipeline and jobs

$ alembic downgrade -1   # 1 downgrade applied
$ alembic upgrade head   # 1 upgrade applied
$ alembic current
51d0cb4c3013 (head)
```

Three tables added: `media_objects`, `media_extractions`, `jobs`. 19 tables total.

## 5. Live cost trace

Executed against real PostgreSQL and a real 20-page PDF built with PyMuPDF:

```
====================================================================
SCENARIO 1  non-Pro sends a 20-page PDF with a brief
[info] media_rejected_entitlement  fetched=False
  state=REJECTED  downloads=0  ocr=0  jobs=0
====================================================================
SCENARIO 2  Pro sends the same PDF with NO brief
[info] media_waiting_for_brief  fetched=False ocr=0 vision=0
  state=WAITING_FOR_BRIEF  downloads=0  ocr=0  jobs=0
====================================================================
SCENARIO 3  the brief arrives: 'explain the graph on page 13'
[info] extraction_planned  ocr_pages=0 planned_pages=1
                           targeting='brief named page(s) 13' total_pages=20 vision_pages=0
[info] extraction_complete cache_hits=0 ocr_pages=0 pages=1 vision_pages=0
  state=READY_FOR_CAPABILITY  downloads=1  ocr=0  vision=0
  pages extracted from a 20-page document: [13]
====================================================================
```

The core invariant, demonstrated on real files: an ineligible student and an
unbriefed attachment both cost **zero downloads**, and a targeted brief reads
**one page out of twenty**.

### Planner behaviour on a real 20-page PDF

```
'explain the graph on page 13'         -> pages=[13]              ocr=0 vision=0
'summarize pages 2-3'                  -> pages=[2, 3]            ocr=0 vision=0
'help me with the quadratic equations' -> pages=[1, 5, 9, 13, 17] ocr=0 vision=0
'what is photosynthesis here'          -> pages=[2, 6, 10, 14, 18] ocr=0 vision=0
'solve this'                           -> pages=[1..5]            ocr=0 vision=0
```

### Mixed digital/scanned document

```
digital pages: [1, 3] of 3
'explain page 2'  -> [(2, 'LOCAL_OCR')]     ocr=1 vision=0
  with no Tesseract -> [(2, 'VISION')]      reason: no text layer and local OCR unavailable
'summarize whole'  -> [(1,'DIGITAL_TEXT'), (2,'LOCAL_OCR'), (3,'DIGITAL_TEXT')]
```

## 6. Security scan

```bash
$ grep -rnE "TODO|FIXME|NotImplementedError|shell=True|eval\(|verify=False|pickle\.loads" src scripts
# no matches

$ grep -rniE "\bcelery\b|\bredis\b|aws_s3|amazonaws" src --include="*.py"
src/tutortwin/media/adapters.py:101:    No Celery, no Redis, no permanently running worker: ...
# the only hit is a comment stating we use none of them
```

File-security behaviour, verified directly:

```
filename sanitisation:
  '../../etc/passwd'              -> 'passwd'
  'C:\Windows\system32\evil.exe'  -> 'evil.exe'
  '  ..hidden'                    -> 'hidden'
  '...'                           -> 'upload'

validation:
  real PNG   ok=True  kind=IMAGE
  EXE        ok=False reason=EXECUTABLE
  ZIP        ok=False reason=ARCHIVE
  gzip       ok=False reason=ARCHIVE
  shebang    ok=False reason=EXECUTABLE
  empty      ok=False reason=CORRUPT
  garbage    ok=False reason=UNSUPPORTED_MIME
  20000px    ok=False reason=DIMENSIONS

blobstore ownership:
  owner reads : True
  cross-read  : DENIED
  content-addressed idempotency : True
```

## 7. Cost accounting

| Metric | Count |
|---|---|
| OpenAI calls | 0 |
| Anthropic calls | 0 |
| Vision calls (real) | 0 |
| OCR pages (real Tesseract) | 0 |
| Transcriptions (real) | 0 |
| **Estimated cost** | **$0.00** |

All provider interaction ran through counting fakes.

## 8. Defects found and fixed during this phase

1. **Vision would have received a page reference, not pixels.** `ModelMessage`
   was text-only, so the first draft of the extractor sent
   `"[page 3 rendered at N bytes]"` to a vision model — a call that costs money
   and cannot work. *Fixed:* added `ImagePart` to the provider contract and
   image serialisation to both vendor adapters (base64 content blocks for
   Anthropic, data-URL parts for OpenAI), with the image before the text as both
   vendors recommend. Verified by direct serialisation inspection.

2. **Dotfile survived filename sanitisation.** `"  ..hidden"` became
   `"__..hidden"` because leading dots were stripped *before* substitution.
   *Fixed:* strip after, so it becomes `"hidden"`.

3. **Dead branch in media intake.** mypy's `comparison-overlap` caught
   `needs_brief=state is MediaState.WAITING_FOR_BRIEF` inside a branch that had
   already excluded that state — always `False`. *Fixed and the resumable case
   documented.*

4. **Half-built job endpoint.** The first draft authenticated, tracked attempts
   and returned `RUNNING` without processing anything. *Fixed:* it now loads the
   media object and runs the pipeline, marking the job `SUCCEEDED` or
   `FAILED_PERMANENT`.

5. **Dead code in the page scorer** — a no-op loop left from an abandoned
   approach. *Deleted.*

6. **A test, not the product, was wrong.** The cache test called `scanned_pdf()`
   twice; PyMuPDF stamps a creation timestamp, so the two files had different
   hashes and correctly missed a content-addressed cache. *Fixed by reusing one
   byte string* — the cache was behaving properly all along.

## 9. Changed files

Phase 03 added 8 modules, 2 test files and 2 docs; 83 Python files total.

```
src/tutortwin/
  domain/media.py                                    (new) state machine, limits
  domain/provider.py                                 (modified) ImagePart
  media/  __init__.py  validation.py  blobstore.py
          pdf.py  ocr.py  extractor.py  pipeline.py
          audio.py  adapters.py                      (new)
  repositories/media.py                              (new)
  api/routes/jobs.py                                 (new) internal job endpoint
  api/app.py  api/dependencies.py  config.py         (modified)
  db/models.py                                       (modified) 3 tables
  providers/anthropic_adapter.py openai_adapter.py   (modified) image support
migrations/versions/51d0cb4c3013_phase_03_...py      (new)
tests/integration/test_media_pipeline.py             (new) 23 tests
tests/unit/test_media_security.py                    (new) 48 tests
tests/integration/test_api.py                        (modified) job endpoint auth
docs/tutortwin/ MEDIA_PIPELINE.md  DEPLOYMENT.md     (new)
docs/tutortwin/ COST_CONTROLS.md  SECURITY.md        (extended)
```

`git diff --stat` is unavailable: the workspace is not a git repository.

## 10. Isolation compliance

The Lead Intake repository, the NX Tutors website and production MySQL were not
cloned, opened, inspected or connected. Media sources are local/in-memory only;
WhatsApp media fetching is explicitly deferred to Phase 08.

## Limitations

1. **No Tesseract binary on this machine**, so no real OCR ran. The provider
   reports `available=False` honestly and the planner routes those pages to
   vision. Both paths are tested with a scriptable fake. To enable:
   ```bash
   apt-get install -y tesseract-ocr tesseract-ocr-eng   # or the Windows installer
   python -c "from tutortwin.media.ocr import TesseractOCRProvider as T; print(T().available)"
   ```

2. **R2 and Cloud Tasks have not been exercised against live endpoints.** Both
   adapters are implemented and lazily imported; `boto3` and
   `google-cloud-tasks` are deliberately not in the dependency set. Smoke
   commands are in [DEPLOYMENT.md](../DEPLOYMENT.md).

3. **No live vision or transcription call was made.** No vendor key was
   available. Request shapes are asserted in unit tests; a real call would need:
   ```bash
   TUTORTWIN_ANTHROPIC_API_KEY=sk-ant-... python scripts/chat.py --live ...
   ```

4. **Images are stored and validated but not yet OCR'd or sent to vision in the
   pipeline.** `process()` marks a validated image `READY_FOR_CAPABILITY`; the
   image→OCR→vision execution path shares the extractor but is wired for PDFs
   only. Image extraction lands with the capability that consumes it.

5. **Compressed audio duration is unknown.** Only WAV is parsed exactly; MP3,
   OGG and AMR rely on the byte cap instead. Decoding them locally would need a
   codec dependency this phase does not justify.

6. **The retention sweeper is not built.** Blobs carry `expires_at` and the R2
   bucket is expected to enforce a lifecycle rule; a sweeper for
   `media_extractions` rows arrives in Phase 07.

## Result

**Phase 03 complete.** 281 tests passing, 86% coverage, ruff and strict mypy
clean, migrations reversible, all 15 mandatory scenarios proven with exact
counters, $0.00 spent. Stopping here per the loop protocol.
