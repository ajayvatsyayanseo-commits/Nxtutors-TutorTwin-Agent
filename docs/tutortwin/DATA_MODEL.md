# TutorTwin Data Model

Status: Phase 01. 16 tables, one migration (`f80deb6c45fc`).

## Conventions

- **Primary keys**: UUID v4, generated in Python (not `DEFAULT gen_random_uuid()`),
  so the ID is available before flush for correlation logging.
- **Timestamps**: `TIMESTAMPTZ`, server-defaulted to `now()`.
- **JSON**: `JSONB` throughout.
- **No binary payloads.** Media lives in the BlobStore; Postgres holds only
  references, hashes and metadata.

## Tables

### Identity and entitlement
| Table | Purpose | Key indexes |
|---|---|---|
| `tutortwin_subjects` | Student identity in TutorTwin's terms | `uq_subject_external (type, value)` |
| `entitlements` | Plan snapshot per subject | `ix_entitlements_subject (subject_id, status)` |
| `plan_policies` | Local plan matrix (`allows_paid_ai`, limits) | PK `(plan_code, version)` |

`entitlements.fetched_at` and `source` exist because entitlement may be cached
or stale; Phase 09 needs to reason about staleness.

### Tutor
| Table | Purpose | Key indexes |
|---|---|---|
| `tutors` | Tutor record | - |
| `tutor_persona_versions` | Versioned persona JSON | `uq_persona_tutor_version`, `ix_persona_active` |
| `tutor_assignments` | Subject -> tutor | `ix_assignment_subject_active` |

Persona is immutable once activated: a change creates a new version row, which
keeps past conversations auditable against the persona actually used.

### Conversation
| Table | Purpose | Key indexes |
|---|---|---|
| `conversations` | Open/closed conversation | `ix_conversation_subject_status` |
| `messages` | Turn history | `ix_message_conversation_created`, `ck_message_role` |
| `request_events` | Inbound normalized event, as received | `ix_request_event_correlation` |
| `request_states` | Terminal state + error code per request | `ix_request_state_event` |
| `idempotency_keys` | Dedupe + stored response | `uq_idempotency_key` |
| `outbound_actions` | Actions produced, with delivery status | `ix_outbound_conversation` |

Indexes follow actual queries: `ix_conversation_subject_status` serves the hot
"find this subject's open conversation" lookup; `ix_message_conversation_created`
serves ordered history reads.

### Cost and configuration
| Table | Purpose |
|---|---|
| `usage_ledger` | Per-call tokens and cost, with `rate_version` |
| `model_catalog` | alias -> provider/model_id/price |
| `feature_flags` | Kill switches |
| `audit_events` | Sensitive mutations |

`usage_ledger.rate_version` and the stored cost exist so historical spend is
never recomputed with today's prices. `model_catalog` is why no vendor model ID
appears in business code.

`usage_ledger.capability` (Phase 06, migration `c4a17be9d520`) records what a
call was *for*, written at call time. It cannot be recovered afterwards - a
ledger row points at a request event, not at the message whose capability
produced it - so it is recorded rather than derived. It is nullable and left
null for every row written before the column existed; the control plane groups
those as `unattributed` instead of backfilling a guess that would look
authoritative.

## Idempotency

The guarantee is a database constraint, not application logic:

```sql
CONSTRAINT uq_idempotency_key UNIQUE (key)   -- key = "<source>:<message_id>"
```

`claim_idempotency_key` does `INSERT ... ON CONFLICT DO NOTHING RETURNING id`.
The winner processes; a loser replays the stored response. This holds under
concurrency - proved by `test_concurrent_duplicates_execute_work_once`.

Scoping by `source` prevents a message id from one channel colliding with the
same id from another.

## Not yet created

Media, RAG/embeddings (needs pgvector), memory, learning, practice, mock tests,
jobs, admin/RBAC, and integration tables. They arrive with the phase that uses
them, per the blueprint's "do not create every table on day one".

---

# Phase 04: knowledge and memory tables

Seven tables in `db/knowledge_models.py`. 26 tables total.

## Knowledge and retrieval

| Table | Purpose | Key constraints |
|---|---|---|
| `knowledge_sources` | An ingested document | `uq_source_content`, `ck_source_visibility_owner` |
| `document_chunks` | One retrievable passage | `uq_chunk_ordinal`, `ck_chunk_visibility_owner`, GIN full-text index |
| `embedding_cache` | Content-addressed vectors | PK `(normalized_sha256, embedding_model)` |
| `retrieval_events` | Audit of every decision, skips included | `ix_retrieval_subject_created` |

**Ownership is denormalized onto chunks** — `visibility`, `subject_id`,
`tutor_id`, `course_id`, `conversation_id` are duplicated from the source. This
is deliberate: the retrieval predicate filters on them directly, in the same
WHERE clause that ranks, without a join the planner might reorder.

`ck_*_visibility_owner` makes an unowned private row impossible to insert:

```sql
(visibility = 'STUDENT_PRIVATE' AND subject_id IS NOT NULL)
 OR (visibility = 'TUTOR' AND tutor_id IS NOT NULL)
 OR (visibility = 'COURSE' AND course_id IS NOT NULL)
 OR (visibility = 'CONVERSATION' AND conversation_id IS NOT NULL)
 OR visibility = 'GLOBAL_CURATED'
```

`uq_source_content` spans `(content_sha256, visibility, subject_id, tutor_id,
parser_version, chunker_version, embedding_model)`. Re-ingesting identical
content under identical versions is a no-op; changing the chunker or embedding
model creates a *new* source rather than mixing incompatible vectors.

`embedding_json` is JSONB, not a `vector` column, so the schema works with or
without the pgvector extension. See [RAG_MEMORY.md](RAG_MEMORY.md) for the
trade-off and the upgrade path.

## Memory and learning

| Table | Purpose | Key constraints |
|---|---|---|
| `student_memories` | Durable learning facts | `uq_memory_statement` |
| `topic_stats` | Observed counters only | `uq_topic_stat` |
| `conversation_summaries` | Rolling summary + watermark | PK `conversation_id` |

`uq_memory_statement (subject_id, statement_sha256)` means re-observing a fact
increments `observed_count` instead of inserting a duplicate — which is what
keeps memory small enough to be worth loading.

`superseded_by` is a self-reference: a replaced memory is retained and marked,
never deleted, so the history stays auditable.

`conversation_summaries.covered_message_count` is a **count**, not a message id.
uuid4 has no ordering, so an id-based watermark could not tell which of two
concurrent updates covered more messages.

**Observed and inferred are separate tables.** `topic_stats` holds
measurements; inferred conclusions live in `student_memories` with a confidence
band. Conflating them is how a guess gets presented as a measurement.

---

# Phase 05: learning engine tables

Ten tables in `db/learning_models.py`, one migration (`de2503b61a7d`).
**36 tables total.**

## Artifacts and notes

| Table | Purpose | Key constraints |
|---|---|---|
| `learning_artifacts` | Generated diagram metadata | `uq_artifact_subject_sha`, `ck_artifact_generated_by` |
| `study_notes` | Structured notes with their sources | `ix_notes_subject_topic` |
| `homework_tasks` | Live task state per conversation | `ix_homework_conversation` |

`learning_artifacts` is content-addressed on `(subject_id, sha256)`, so asking
for the same graph twice stores one blob and one row — which also makes a retry
after a dropped connection free. `generated_by` is constrained to
`'deterministic' | 'model_spec'`: it records whether a model produced the
*specification*, never the pixels, because that path does not exist.

`spec` stores the validated specification the artifact was rendered from, so the
same picture is reproducible rather than approximately regenerated.

`study_notes.dropped_citations` keeps the citations a model produced that matched
no supplied source. Storing them makes a rise in hallucinated references visible
instead of merely discarded.

## Flashcards

| Table | Purpose | Key constraints |
|---|---|---|
| `flashcard_decks` | A named deck | `uq_deck_subject_name` |
| `flashcards` | Card + current SM-2 state | `uq_card_deck_content`, `ck_card_ease_floor`, `ix_card_due` |
| `flashcard_reviews` | Append-only review history | `ix_review_card_time` |

`uq_card_deck_content (deck_id, content_sha256)` means regenerating a deck from
the same document creates zero duplicates.

**The schedule is a column and the log is the evidence.** `repetitions`,
`interval_days`, `ease_factor`, `lapses` and `due_on` on `flashcards` are derived
state, cached so the due-card query is one indexed read rather than a replay of
`flashcard_reviews`. The log is never updated or deleted, so a change to the
scheduling algorithm can be replayed against real review history instead of being
trusted.

`due_on IS NULL` means never reviewed, which the due query treats as due now —
expressed in SQL, so a new card surfaces without a second pass in Python.

`ck_card_ease_floor (ease_factor >= 1.3)` puts SM-2's floor in the database.
Below it, intervals collapse and a card is shown forever.

## Assessments

| Table | Purpose | Key constraints |
|---|---|---|
| `assessments` | A quiz or mock test | `ck_assessment_kind`, `ix_assessment_subject_created` |
| `assessment_questions` | One question + its key | `uq_question_number`, `ck_question_marks` |
| `assessment_attempts` | One student sitting | `ck_attempt_state`, `ix_attempt_subject_state` |
| `attempt_responses` | One answer + its grade | `uq_response_attempt_question`, `ck_response_graded_by` |

Quizzes and mock tests share one table because they differ by `kind`, not by
shape.

**`assessment_questions.answer_key` is a column the delivery query never
selects.** The key, the worked solution and the rubric live in one JSONB column;
`load_student_paper()` names only number, type, prompt, marks and options, and
projects them into `StudentQuestion`, a type with no field able to hold any of
the rest. Withholding is structural rather than a filter someone must remember to
apply. `load_answer_key()` is a separate function with its own call site.

`assessments.truncated_reason` records when a plan ceiling produced a smaller
paper than requested, so the student is told plainly. A silently shortened paper
looks like a defect.

`assessment_attempts.model_calls` stores what the attempt actually cost. It is
asserted in tests, so a regression that graded ten MCQs with ten calls fails
rather than merely costing money.

`attempt_responses.correct` is nullable: partial credit on a subjective question
is not a boolean, and forcing one would misreport it. `graded_by` is constrained
to `'deterministic' | 'model'`, which keeps the model-call count auditable
against the rows themselves.

## Migration drift

`ix_chunk_fulltext` is a functional GIN index over
`to_tsvector('english', text)`, created by raw SQL in Phase 04 because the
declarative layer cannot express it. Autogenerate cannot see it in
`target_metadata` and proposed dropping it on every subsequent revision.
`migrations/env.py` now excludes it by name through `include_object`, so
`alembic check` reports real drift instead of being permanently red.
