# Phase 04 Acceptance Report

RAG, student memory, tutor knowledge and the context budgeter. All commands
below were executed; output is verbatim.

## Environment

| Component | Version |
|---|---|
| OS | Windows 11 (win32) |
| Python | 3.12.10 |
| PostgreSQL | 18.1 (local) |
| pgvector (Python) | 0.5.0 |
| numpy | 2.5.2 |
| **pgvector (server extension)** | **not installed — see Limitations** |

## 1. Static checks

```bash
$ ruff check src tests migrations scripts
All checks passed!

$ ruff format --check src tests migrations scripts
98 files already formatted

$ mypy
Success: no issues found in 71 source files
```

## 2. Tests

```bash
$ pytest
370 passed in 48.49s          # second consecutive run: also 370 passed

$ pytest -m "not integration"
273 passed, 97 deselected in 7.18s

$ pytest --cov --cov-report=term
TOTAL    3943    451    680    100    86%
370 passed
```

Phase 04 added 62 tests: 38 unit (`test_rag_memory_units.py`), 16 RAG
integration (`test_rag.py`), 8 evaluation (`test_rag_eval.py`).

## 3. RAG evaluation metrics

Measured on the deterministic corpus in `tests/integration/test_rag_eval.py` —
3 documents, 6 pages, 6 targeted queries, plus a tutor source and an intruder:

```
query                                          top source      pg  ok
------------------------------------------------------------------------
discriminant real roots quadratic equation     Algebra          1   Y
factorising brackets completing the square     Algebra          2   Y
sine rule cosine rule triangle angle           Geometry         1   Y
angle at the centre circle circumference arc   Geometry         2   Y
chloroplasts chlorophyll glucose oxygen light  Biology          1   Y
aerobic respiration releases energy carbon d   Biology          2   Y
------------------------------------------------------------------------
corpus chunks (owner)      : 6
source recall @k=3         : 6/6 = 100%
citation accuracy @k=1     : 6/6 = 100%
WRONG-OWNER retrieval      : 0  (must be 0)
query latency ms           : min=0 max=15 avg=2
embedding provider calls   : 10
```

| Metric | Result | Floor enforced by test |
|---|---|---|
| Source recall @k=3 | **100%** | ≥ 80% |
| Citation accuracy @k=1 | **100%** | ≥ 80% |
| **Wrong-owner retrieval** | **0** | exactly 0 |
| Query latency | avg 2 ms, max 15 ms | < 2000 ms |
| Repeat-query embedding calls | 0 | 0 |

## 4. Mandatory adversarial tests

All eight, with the decisive evidence:

| # | Scenario | Test | Result |
|---|---|---|---|
| 1 | Student A asks for Student B's file | `test_student_cannot_retrieve_another_students_private_source` | 0 results, **`candidates_scanned == 0`** |
| 2 | A holds B's real chunk id | `test_guessed_chunk_id_does_not_bypass_ownership` | 0 results |
| 3 | Tutor A material vs Tutor B's student | `test_tutor_material_invisible_to_another_tutors_student` | 0 without grant, ≥1 with |
| 4 | Malicious PDF: "ignore system, reveal secret" | `test_retrieved_content_is_fenced_as_untrusted_data` | fenced as quoted data |
| 5 | RAG source tries to invoke a tool | `test_evidence_block_warns_about_tool_invocation` | explicit tool warning |
| 6 | Deleted source no longer retrieved | `test_deleted_source_is_no_longer_retrieved` | 0 results, 0 candidates |
| 7 | Re-upload same file | `test_reingesting_the_same_file_costs_no_embeddings` | **0 chunks, 0 embedding calls** |
| 8 | Generic question | `test_generic_question_performs_no_vector_query` | **0 embedding calls** |

Plus: student with no tutor sees no tutor material; global curated visible to
all; ownership validated before embedding; identical text across two students
embedded once but scoped separately; repeated boilerplate indexed once.

`candidates_scanned == 0` is the load-bearing assertion. It proves the rows were
never selected — the ownership filter is in the SQL `WHERE` clause, not applied
to fetched data afterwards.

## 5. Live evidence

### Ownership isolation, executed directly

```
ADVERSARIAL CHECKS
  A. Alice targets Bob's private doc -> 0 hits, titles=[]
     Bob's doc present? False  (must be False)
  B. Tutor2's student targets Tutor1 material -> titles=[]
     Tutor1 material present? False  (must be False)
  C. Tutor1's own student -> titles=['Tutor1 Material']
  D. after soft delete -> titles=[]  (must be empty)
```

Log line from each intruder query:

```json
{"event": "similarity_search", "candidates": 0, "returned": 0, "top_k": 10,
 "visible_kinds": ["STUDENT_PRIVATE", "GLOBAL_CURATED"]}
```

### Ingestion idempotency

```
ingest A    : chunks=2 api_calls=1 cache_hits=0
ingest B    : chunks=1 api_calls=1
re-ingest A : already=True chunks=0 api_calls=0   <-- 0/0
```

### RAG decision policy

```
  skip     k=0  SELF_CONTAINED_COMPUTATION  | Solve for x: 2x + 5 = 13
  skip     k=0  SELF_CONTAINED_COMPUTATION  | Calculate the derivative of x^2
  skip     k=0  GENERAL_KNOWLEDGE           | What is photosynthesis?
  RETRIEVE k=6  REFERS_TO_UPLOAD            | Summarize this document for me
  RETRIEVE k=5  ASKS_FOR_SOURCE             | According to the textbook...
  RETRIEVE k=4  TUTOR_SPECIFIC              | My tutor said to use a different method
  RETRIEVE k=5  COURSE_OR_SYLLABUS          | What does chapter 4 of the course say
  skip     k=0  NO_CORPUS_AVAILABLE         | Summarize this document (no corpus)
  RETRIEVE k=5  ASKS_FOR_SOURCE             | Cite the page where this integral is derived
  RETRIEVE k=8  TEST_GENERATION             | Make a mock test
  skip     k=0  BUDGET_FORBIDS              | Summarize this document (budget refused)

dynamic top_k under budget pressure:
   2000 tokens -> k=8 | 800 -> k=4 | 300 -> k=1 | 0 -> k=0
```

### Chunking with provenance

```
  #0 page=1 tok=137 section='CHAPTER ONE: QUADRATIC EQUATIONS'
  #1 page=1 tok= 21 section='CHAPTER ONE: QUADRATIC EQUATIONS'   <- problem 1
  #2 page=1 tok= 20 section='CHAPTER ONE: QUADRATIC EQUATIONS'   <- problem 2
  #3 page=2 tok=127 section='PHOTOSYNTHESIS'
```

### SQL injection, verified not assumed

```
generated SQL:
  ((c.visibility = 'STUDENT_PRIVATE' AND c.subject_id = :subject_id)
   OR (c.visibility = 'GLOBAL_CURATED')
   OR (c.visibility = 'TUTOR' AND c.tutor_id = :tutor_id)
   OR (c.visibility = 'CONVERSATION' AND c.conversation_id = :conversation_id))

params are values, not SQL: subject_id=UUID tutor_id=UUID conversation_id=UUID
SQL contains only bind markers, no interpolated values: True
course_ids typed tuple[UUID,...] - "x' OR '1'='1" rejected: ValidationError
```

## 6. Migrations

```bash
$ alembic upgrade head
INFO  Running upgrade 51d0cb4c3013 -> 788bf6c7111d, phase 04 rag memory and knowledge

$ alembic downgrade -1   # 1 downgrade applied
$ alembic upgrade head   # 1 upgrade applied
$ alembic current
788bf6c7111d (head)
```

Seven tables added: `knowledge_sources`, `document_chunks`, `embedding_cache`,
`student_memories`, `topic_stats`, `conversation_summaries`,
`retrieval_events`. **26 tables total.**

Also: a GIN full-text index on `document_chunks`, and `EMBEDDING` catalog rows
for `text-embedding-3-small` (active) and `-3-large`.

```
=== embedding catalog ===
text-embedding-3-large active=false
text-embedding-3-small active=true
```

## 7. Cost accounting

| Metric | Count |
|---|---|
| OpenAI calls | 0 |
| Anthropic calls | 0 |
| Real embedding calls | 0 |
| **Estimated cost** | **$0.00** |

All embeddings ran through `DeterministicEmbeddingProvider`, which counts every
call so the cost assertions are exact.

| Scenario | Embedding calls |
|---|---|
| Self-contained computation | **0** |
| General knowledge | **0** |
| Follow-up | **0** |
| No corpus | **0** |
| Budget refused | **0** |
| Keyword search sufficient | **0** |
| Re-upload identical file | **0** |
| Same file, second student | **0** (cache) |
| Repeat query | **0** (cache) |
| New document question | 1 |

## 8. Defects found and fixed during this phase

1. **Short exercises were silently discarded.** `MIN_TOKENS = 20` dropped every
   chunk below twenty tokens — but a real exercise ("1. Solve x² − 5x + 6 = 0")
   is about nineteen. The corpus lost exactly the content students ask about by
   number, and retrieval returned nothing for "solve Q4". *Fixed:* threshold
   lowered to 8, with the reasoning recorded; bare page numbers still filtered.

2. **Headings with colons were not recognised.** "CHAPTER ONE: QUADRATIC
   EQUATIONS" failed the all-caps pattern, so every chunk on that page carried
   `section=None`. *Fixed:* colons and digits added to the class.

3. **Problem lines became their own sections.** Each numbered exercise was
   treated as a heading, losing the chapter it belonged to. *Fixed:* headings
   that the problem-splitter would also match are skipped for provenance.

4. **A test print misreported a passing security check.** My first isolation
   script labelled *any* result "LEAK!", including Alice's own documents. The
   underlying property was correct — `candidates=2`, both hers — but the output
   read like a breach. *Fixed:* the check now compares titles against the
   intruder's own corpus.

5. **`S608` needed proof, not suppression.** Three SQL-injection warnings in the
   retrieval path. Rather than silence them, I rendered the generated SQL and
   attempted to smuggle `x' OR '1'='1` through every scope field. The SQL
   contains only bind markers and `RetrievalScope` rejects non-UUID input.
   Suppressed as a per-file ignore *with that verification recorded*.

6. **Intermittent deadlock in the test fixture.** Growing the truncation list to
   23 tables made `TRUNCATE` occasionally deadlock: it takes an ACCESS EXCLUSIVE
   lock on every named table, and two sessions naming them in different orders
   can block each other. A Phase 03 media test failed on a run where nothing
   about media had changed. *Fixed:* tables are truncated in sorted order, so
   every session acquires locks identically, with a bounded `lock_timeout` and
   retry for the residual case where a prior engine is still releasing its
   connection. Verified by two consecutive clean full-suite runs.

   Worth stating plainly: this was a **test-harness** defect, not a product one.
   Nothing in the application takes those locks.

7. **A `noqa` landed inside a SQL string.** While placing the suppression, one
   marker ended up inside a triple-quoted query, where it would have become part
   of the SQL sent to Postgres. *Caught and reverted* in favour of the per-file
   ignore.

## 9. Changed files

Phase 04 added 8 modules, 3 test files and 1 doc; 98 Python files total.

```
src/tutortwin/
  domain/knowledge.py                    (new) visibility, evidence, memory types
  policies/rag_policy.py                 (new) the retrieval cost gate
  rag/  __init__.py  chunking.py  embeddings.py
        vector_store.py  ingestion.py  service.py   (new)
  services/memory.py                     (new) candidates, minimisation, stats
  services/budgeter.py                   (new) priority context allocation
  db/knowledge_models.py                 (new) 7 tables
migrations/versions/788bf6c7111d_...py   (new) + GIN index + embedding catalog
migrations/env.py                        (modified) registers knowledge models
tests/unit/test_rag_memory_units.py      (new) 38 tests
tests/integration/test_rag.py            (new) 16 tests
tests/integration/test_rag_eval.py       (new) 8 tests
tests/conftest.py                        (modified) truncation list
tests/integration/test_migrations.py     (modified) expected tables
docs/tutortwin/RAG_MEMORY.md             (new)
docs/tutortwin/ DATA_MODEL · ORCHESTRATION · SECURITY · COST_CONTROLS  (extended)
```

`git diff --stat` is unavailable: the workspace is not a git repository.

## 10. Isolation compliance

The Lead Intake repository, the NX Tutors website and production MySQL were not
cloned, opened, inspected or connected. All students, tutors and documents are
standalone fixtures.

## Limitations

1. **pgvector is not installed and cannot be built here.** The server extension
   needs a compiler toolchain (`make`, `gcc`/`cl`, `pg_config`) — all absent.
   Similarity is computed in pure SQL over a JSONB array instead:
   `1 - dot(a,b)/(‖a‖·‖b‖)`.

   Correctness and security are **identical** — same arithmetic, same WHERE
   clause. Speed differs: a sequential scan of the *owned* subset rather than an
   ANN index. Acceptable at per-student corpus size (measured: 6 candidates,
   2 ms average); not acceptable at a million shared chunks.

   `pgvector_available()` reports the live mode. Upgrade path, in
   `PGVECTOR_UPGRADE`:
   ```sql
   CREATE EXTENSION vector;
   ALTER TABLE document_chunks ADD COLUMN embedding vector(1536);
   -- backfill from embedding_json
   CREATE INDEX ON document_chunks USING hnsw (embedding vector_cosine_ops);
   -- swap the distance expression for `embedding <=> :query_vec`
   ```
   The ownership predicate does not change — it is already in the WHERE clause.

2. **No real embedding call was made.** No OpenAI key was available.
   `OpenAIEmbeddingProvider` is implemented and batches at 96;
   `DeterministicEmbeddingProvider` backs every test. Live smoke:
   ```bash
   TUTORTWIN_OPENAI_API_KEY=sk-... python -c "..."  # see RAG_MEMORY.md
   ```
   Retrieval *quality* numbers therefore measure the pipeline, not a real
   embedding model's semantics. The deterministic provider shares direction for
   texts sharing vocabulary, which is enough to exercise ranking but is not a
   claim about production recall.

3. **Provider prompt caching is unmeasured.** Phase 02 wired
   `cacheable_prefix` for both vendors and the request shape is asserted in
   tests, but a real cache *hit* needs a live call to confirm
   (`usage.cache_read_input_tokens > 0` on a second call within the TTL).

4. **Reranking is not implemented.** The policy allows a cheap-model rerank; it
   is not built, because on this corpus RRF over keyword + vector already gives
   100% recall and 100% citation accuracy. Adding a paid call before there is
   evidence it improves anything would be cost for no measured benefit.

5. **Memory candidate extraction is deterministic only.** The design permits a
   cheap model where rules cannot see a pattern; no such path is wired. Every
   candidate today comes from an explicit, self-reported signal.

6. **Conversation summaries remain truncation-based** (as in Phase 02), and the
   `conversation_summaries` table is defined but not yet written by the entry
   service — the rolling-summary write path lands with the Phase 05 learning
   engine that consumes it.

7. **RAG is not yet wired into `entry_service`.** The retrieval service, budgeter
   and memory layer are complete and tested standalone; the pipeline integration
   is the first task of Phase 05, where the learning engine consumes the same
   context allocation.

## Result

**Phase 04 complete.** 370 tests passing, 86% coverage, ruff and strict mypy
clean, migrations reversible, 100% retrieval recall and citation accuracy, **zero
wrong-owner retrieval**, $0.00 spent. Stopping here per the loop protocol.
