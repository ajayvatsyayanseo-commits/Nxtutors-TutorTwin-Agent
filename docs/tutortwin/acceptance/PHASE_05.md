# Phase 05 Acceptance — Complete StudySnap + GPAI Inspired Learning Engine

Standalone. No Lead Intake, no NX website, no production MySQL. All model calls
in tests go through the fake provider; all storage through PostgreSQL 18.1 and
the filesystem BlobStore.

## Environment

| Component | Version |
|---|---|
| OS | Windows 11 (win32) |
| Python | 3.12.10 |
| PostgreSQL | 18.1 (local) |
| sympy | 1.14.0 |
| pint | 0.25.3 |
| matplotlib | 3.11.1 (Agg backend) |

## Verification commands and results

All commands were executed; output is verbatim.

```bash
$ ruff check src tests
All checks passed!

$ ruff format --check src tests
109 files already formatted

$ mypy src
Success: no issues found in 85 source files

$ alembic check
No new upgrade operations detected.

$ pytest
539 passed in 74.99s (0:01:14)

$ pytest -m "not integration"
424 passed, 115 deselected in 7.94s

$ pytest --cov=tutortwin --cov-report=term
TOTAL    5842    560    1118    164    88%
539 passed
```

Phase 04 ended at 370 tests and 86%. The suite is now **539 tests at 88%**.

The learning engine's own tests are **124 test functions, 169 collected cases**
(the difference is parametrisation):

| File | Functions | Collected |
|---|---|---|
| `tests/unit/test_learning_engine.py` | 70 | 105 |
| `tests/unit/test_learning_pipeline.py` | 36 | 46 |
| `tests/integration/test_learning_engine_e2e.py` | 18 | 18 |

Learning-engine module coverage:

| Module | Coverage |
|---|---|
| `db/learning_models.py` | 100% |
| `domain/learning.py` | 99% |
| `learning/notes.py` | 99% |
| `learning/solver.py` | 97% |
| `learning/visuals.py` | 96% |
| `learning/homework.py` | 96% |
| `learning/essay.py` | 95% |
| `learning/assessment.py` | 93% |
| `learning/verification.py` | 83% |
| `repositories/learning.py` | 82% |
| `learning/twin.py` | 81% |
| `learning/practice.py` | 80% |
| `services/artifacts.py` | 74% |

The two lowest are honest gaps rather than untested behaviour: `practice.py`'s
uncovered lines are the prose branches of `describe_progress()`, and
`artifacts.py`'s are the renderer-dispatch arms reached only through spec types
that their own renderer tests already cover directly.

## Schema

Ten new tables in one migration, `de2503b61a7d`. **36 tables total.**

`learning_artifacts`, `study_notes`, `homework_tasks`, `flashcard_decks`,
`flashcards`, `flashcard_reviews`, `assessments`, `assessment_questions`,
`assessment_attempts`, `attempt_responses`.

Round-trip proved on a throwaway database: `upgrade head` → 36 tables →
`downgrade base` → 0 tables → `upgrade head` → 36 tables
(`test_migrations_downgrade_and_reapply`).

`alembic check` was permanently red before this phase because `ix_chunk_fulltext`
is a functional GIN index created by raw SQL that autogenerate cannot see in the
model metadata, so every revision proposed dropping it. `migrations/env.py` now
excludes it by name, and the drift check is meaningful again.

## Feature matrix

| Section | Capability | Implementation | Free of model calls? |
|---|---|---|---|
| A | Hint / solution / explain step / check answer / mistake analysis / simpler / diagram / similar / harder / easier | `learning/homework.py` | action detection yes |
| A | Task state survives the turn; "step 3" resolves | `homework_tasks`, `repositories/learning.py` | yes |
| B | Normalize → subject → assumptions → solve → verify → confidence → selective verify | `learning/solver.py` | all but the solve |
| B | Local verifiers: SymPy, Pint, numeric tolerance, substitution, dimensional | `learning/verification.py` | yes |
| B | `VerificationPolicy`, cross-model comparison, qualified answer | `learning/solver.py` | yes |
| C | Plots, geometry, free-body, block/flow, series circuit, structured SVG, TikZ | `learning/visuals.py` | yes |
| C | `VisualArtifactService`, stored metadata, content-addressed | `services/artifacts.py`, `learning_artifacts` | yes |
| D | Twin generation, 4 modes, batch within plan limits | `learning/twin.py` | yes |
| E | Notes from topic / conversation / document, with sources | `learning/notes.py`, `study_notes` | 1 call; parsing free |
| F | Persistent decks, SM-2 scheduling, review history | `learning/practice.py`, 3 tables | yes |
| G | Quizzes: 5 question types, hidden answers, persisted attempts | `learning/assessment.py`, 4 tables | objective grading yes |
| H | Mock blueprint, mark allocation, printable artifact, plan ceilings | `learning/assessment.py` | yes |
| I | Objective + rubric grading, evidence, manual-review flag | `learning/assessment.py` | objective yes; rubric batched |
| J | Thesis / structure / clarity / grammar / evidence / paragraph feedback, rubric grade | `learning/essay.py` | 1 call; bounds enforced free |
| K | `SandboxGateway`, `DisabledSandbox`, `ArithmeticCalculator` | `learning/homework.py` | yes |
| L | Topic attempts, correctness, hints, mastery bands, weak topics | `learning/practice.py`, `topic_stats` | yes |
| M | Persona + pedagogy formatting | Phase 02 `services/prompts.py` | yes |

Not implemented, deliberately: RDKit chemistry structures (optional, extension
not enabled — see Known limitations).

## The 15 required end-to-end scenarios

All in `tests/integration/test_learning_engine_e2e.py`, against real PostgreSQL.

| # | Scenario | Test | Model calls | Asserted |
|---|---|---|---|---|
| 1 | image homework → brief → solve → verify | `test_image_homework_with_brief_is_solved_and_verified` | 0 (verification) | brief present, Unicode minus normalised, VERIFIED, task persisted with media id |
| 2 | calculus text → verify | `test_calculus_answer_is_verified_symbolically_without_a_model` | **0** | VERIFIED for equivalent, REFUTED for wrong |
| 3 | physics with units → unit check | `test_physics_answer_is_checked_across_units` | **0** | `200 cm` = `2.0 m` VERIFIED; `5 kg` vs `5 m` REFUTED; assumptions stated |
| 4 | generate graph → local artifact | `test_graph_is_rendered_locally_and_stored` | **0** | PNG magic bytes, `generated_by=deterministic`, second request reuses one row |
| 5 | twin problem → verified different answer | `test_twin_problem_has_a_verified_different_answer` | **0** | `verified=True`, answers differ numerically |
| 6 | flashcards from document → review schedule | `test_flashcards_from_a_document_get_a_deterministic_schedule` | 0 (ingest embeds locally) | 2 cards added, re-add adds 0, first interval exactly 1 day, card leaves the queue |
| 7 | quiz → submit → grade | `test_quiz_is_delivered_without_answers_and_graded_without_a_model` | **0** | no key in the delivered payload, 4/4 marks, all rows `graded_by=deterministic` |
| 8 | mock → artifact → hidden key | `test_mock_paper_artifact_contains_no_answers` | **0** | stored bytes contain the prompt, not the worked solution; key still in the DB |
| 9 | submit mock → grade → progress | `test_submitted_mock_updates_progress_and_weak_topics` | **0** | weak topics carry evidence; a 3-attempt topic is excluded; `accuracy is None` |
| 10 | essay feedback | `test_essay_feedback_is_one_call_and_will_not_ghost_write` | **1** | 5 dimensions, phantom paragraph dropped, grade clamped, rewrite bounded |
| 11 | coding help with no unsafe execution | `test_coding_help_never_executes_and_says_so` | 0 | sandbox DISABLED, empty stdout, calculator rejects `__import__` and `9**9**9` |
| 12 | RAG-grounded answer includes source | `test_rag_grounded_notes_cite_a_real_source_and_drop_invented_ones` | 0 (deterministic embeddings) | real citation kept, invented journal dropped and recorded |
| 13 | cheap simple question does not use verifier | `test_simple_question_never_pays_for_a_second_model` | **0 extra** | reason `simple_question_local_check_only` / `not_a_stem_capability` |
| 14 | hard STEM uses verifier per policy | `test_hard_stem_uses_the_verifier_unless_a_local_check_settles_it` | 1 extra, or **0** | LOW + advanced → verifier; local REFUTED → `local_check_refuted`, 0 extra |
| 15 | budget-limited plan cannot generate giant mock | `test_free_plan_cannot_generate_a_giant_mock` | **0** | FREE 180 min → 30 min / 10 q with `truncated_reason`; PRO → 180 / 50 |

Three supporting integration tests round out the suite: batched rubric grading
(ten questions, one call), a homework step follow-up across a database
round-trip, and the delivery projection's field set.

## Exact model call counts

Measured, not estimated.

| Path | Calls |
|---|---|
| Problem normalisation + subject detection + assumptions | 0 |
| Any local verification (substitution, symbolic, numeric, unit, dimensional) | 0 |
| Verification policy decision | 0 |
| Cross-model comparison of two answers | 0 |
| Twin problem, N = 1 | 0 |
| Twin problem, N = 5 (batch) | 0 |
| Function plot (PNG), geometry (SVG), free-body (PNG), block (SVG), circuit (SVG), TikZ | 0 each |
| Same artifact requested a second time | 0, and no second row |
| Printable mock paper | 0 |
| SM-2 next due date | 0 |
| Grading 3 objective questions | 0 |
| Grading 10 objective questions | 0 |
| Exact-match short answer | 0 |
| Blueprint for any duration | 0 |
| Weak-topic analysis | 0 |
| Study notes over 6 sources | 1 |
| Essay feedback with rubric grade | 1 |
| Grading 10 subjective questions | 1 |
| Simple STEM question, HIGH confidence | 1 |
| Advanced STEM, LOW confidence, local check inconclusive | 2 |
| Advanced STEM, LOW confidence, local check REFUTED | 1 |

## Representative artifact samples

### Series circuit (SVG, 1268 bytes, sha `a629c77baa204626…`)

```svg
<svg xmlns="http://www.w3.org/2000/svg" width="312" height="244" viewBox="0 0 312 244">
<rect width="100%" height="100%" fill="white"/>
<text x="156.0" y="20" text-anchor="middle" font-size="13" font-weight="bold">Series circuit</text>
<rect x="40.0" y="64.0" width="232.0" height="140.0" fill="none" stroke="black" stroke-width="1.6"/>
... battery, resistor and lamp symbols drawn on the loop ...
</svg>
```

### Function plot

`y = x^2 - 4` as PNG: 23 611 bytes, 704 × 440, sha `ed3f4ec26fc2e19e…`. The same
spec always produces the same bytes, which is what makes the store
content-addressed.

### TikZ source for the same plot

```tex
\begin{tikzpicture}
\begin{axis}[xlabel={x}, ylabel={y}, title={y = x\textsuperscript{2} - 4}, grid=both, domain=-10.0:10.0]
\addplot[thick] {x**2 - 4};
\end{axis}
\end{tikzpicture}
```

A hostile title survives as inert text: `pwn \input{/etc/passwd} $x$` becomes
`title={pwn input/etc/passwd x}`.

### Printable mock paper (stored artifact, `PRINTABLE_PAPER` / `TEXT`)

```
Algebra mock
============
Time: 30 minutes    Maximum marks: 4

Q1. [2 mark(s)] Solve x + 2 = 5
    (a) x = 1
    (b) x = 3
    (c) x = 7

Q2. [2 mark(s)] A trolley travels 2.0 m. Give the distance in cm.

--- end of paper ---
```

The worked solution `Subtract 2 from both sides.` is in
`assessment_questions.answer_key` and appears nowhere in these bytes.

### Twin problems

| Original | Twin | Answer | Verified |
|---|---|---|---|
| `x^2 - 5x + 6 = 0` | `x^2 - 7x + 12 = 0` | x = 3 or x = 4 | yes |
| `3x + 4 = 19` | `3x + 5 = 23` | x = 6 | yes |

### SM-2 ladder (all GOOD, from a new card)

`1, 6, 15, 38, 95, 238` days.

### Blueprints for a 180-minute request

| Plan | Duration | Questions | Marks | Mix | `truncated_reason` |
|---|---|---|---|---|---|
| FREE | 30 | 10 | 30 | 6 MCQ, 2 T/F, 2 numeric | duration reduced from 180 to 30 minutes; questions reduced from 15 to 10 |
| PRO | 180 | 50 | 180 | 20 MCQ, 12 numeric, 10 short, 8 structured | — |

### Qualified answer on cross-model disagreement

```
I checked this answer a second way and the two checks did not agree, so treat
the result below as provisional and work through the steps yourself: (the final
results differ)

The speed is 12 m/s.
```

## Defects found and fixed during this phase

Each was found by running the code, not by inspection.

1. **`sympify` is remote code execution.** `sympify("__import__('os').getcwd()")`
   returned the working directory. Fixed with a character filter plus a
   whitelisted namespace.
2. **A name whitelist alone is bypassable.** `().__class__.__bases__` reaches
   `object` through attribute access on a literal. The character filter (no
   quotes, no dots, no dunder) is what closes it.
3. **Exponent bomb.** `2**1000` parses in 0.6 ms; `9**9**9` never returned — the
   process was killed at 25 s and again at 30 s. Evaluation happens *during*
   parsing, so the guard had to run on the raw string.
4. **XML entity expansion in the SVG sanitiser.** A billion-laughs payload hung
   the sanitiser itself, and XXE could read local files. `<!ENTITY` is now
   rejected before parsing; both payloads are refused in under 0.02 ms, and the
   tests assert elapsed time because passing quickly is what proves no expansion
   happened.
5. **The sanitiser rejected matplotlib's own SVG.** It emits inert Dublin Core
   RDF and reuses glyph outlines through `<use href="#id">`. Fixed by checking
   only loading attributes.
6. **`x^2` did not parse.** SymPy read `^` as XOR; `convert_xor` added, because
   students write `^` far more often than they mean bitwise xor.
7. **A correct answer was unverifiable.** `compare_quantities("2.0 m", "200 cm")`
   returned NOT_APPLICABLE because the unit pattern rejected the decimal point.
8. **Poles plotted as lines.** `complex(zoo)` yields `nan` rather than raising,
   so an asymptote drew a curve that does not exist.
9. **Twin collapsed the roots.** Offsetting each root of (3, 2) gave (4, 4) — a
   repeated root, which changes the method being practised.
10. **Twin drifted in difficulty.** `3x + 4 = 19` (x = 5) became `3x + 5 = 21`
    (x = 16/3). A student drilling integer answers should not meet thirds.
11. **TikZ titles were safe and wrong.** Stripping `^` turned `y = x^2 - 4` into
    `y = x2 - 4`, a different equation. Superscripts and subscripts are now
    rebuilt from a matched group after filtering.
12. **`alembic check` was permanently red.** The Phase 04 functional GIN index is
    invisible to autogenerate, so every revision proposed dropping it — which
    would have silently disabled lexical retrieval.

## Known limitations

* **No code execution, by design.** `DisabledSandbox` refuses every request. The
  API process holds database credentials and provider keys; running student code
  there would hand them to arbitrary input. An ephemeral isolated runner is
  Phase 07 work.
* **Circuits are series-only.** This covers school physics questions. A general
  netlist renderer needs a placement algorithm whose failure mode is a silently
  wrong diagram, which is worse than not offering it.
* **No RDKit chemistry structures.** The optional extension is not enabled in
  this environment, so the capability is absent rather than stubbed. Nothing
  claims to draw a molecule.
* **Twin generation covers linear and quadratic templates.** Anything else raises
  `NoTemplateMatch`, and the caller may fall back to a model — the only twin path
  that costs money. Widening the template set is additive.
* **Numeric grading needs parseable units.** When Pint cannot parse both sides
  the question is flagged for manual review rather than graded, because falling
  back to a bare numeric comparison would mark `200 cm` wrong against `2.0 m`.
* **Symbolic equivalence can be INCONCLUSIVE.** `simplify()` failing to reach
  zero is not proof of inequality, and reporting REFUTED there would be false
  certainty.
* **Notes and essay parsing depend on the response layout.** A model that ignores
  the fixed format yields fewer sections or dimensions rather than an error; the
  content is never invented to fill the shape.
* **Block diagrams need row and column in the spec.** Auto-layout was rejected:
  a model that must name a position produces a diagram we can draw exactly.
* **Mastery is a band, never a percentage.** Below 5 attempts `accuracy` returns
  `None`, and the topic is excluded from recommendations entirely.
* **Rubric grading is a single batched call.** Ten subjective questions cost one
  call, which is the right cost but means one bad response affects the whole
  batch; over-awards are clamped and missing grades are flagged rather than
  scored zero.

## Isolation confirmed

No Lead Intake code, no NX website code, no MySQL driver, no production
credentials. The only outbound dependency in tests is PostgreSQL. All model
traffic goes through `FakeModelProvider`, and every artifact is rendered locally.
