# Phase 02 Acceptance Report

TutorTwin Meta-Agent: deterministic orchestration, cost-aware model routing,
text tutoring capabilities. All commands below were executed; output is verbatim.

## Environment

| Component | Version |
|---|---|
| OS | Windows 11 (win32) |
| Python | 3.12.10 |
| PostgreSQL | 18.1 (local) |
| anthropic SDK | 1.2.0 |
| openai SDK | 3.6.0 |

Live vendor credentials were **not** available. Production adapters are
implemented and unit-tested against stubbed SDK clients; live smoke commands are
in Limitations.

## 1. Static checks

```bash
$ ruff check src tests migrations scripts
All checks passed!

$ ruff format --check src tests migrations scripts
68 files already formatted

$ mypy
Success: no issues found in 48 source files
```

mypy runs in `strict` mode.

## 2. Tests

```bash
$ pytest
206 passed in 39.16s

$ pytest -m "not integration"
160 passed, 46 deselected in 4.21s

$ pytest --cov --cov-report=term
TOTAL    1904    122    266    40    92%
206 passed
```

Coverage of the new subsystems:

| Module | Coverage |
|---|---|
| `policies/budget_policy.py` | **100%** |
| `orchestration/router.py` | 96% |
| `capabilities/executor.py` | 88% |
| `providers/openai_adapter.py` | 85% |
| `providers/anthropic_adapter.py` | 84% |
| `providers/gateway.py` | 84% |

## 3. Mandatory scenarios

All fourteen, in `tests/integration/test_meta_agent.py`, asserting the fake
provider's own call counter:

| # | Requirement | Test | Calls | Result |
|---|---|---|---|---|
| 1 | Simple biology → cheap tier | `test_simple_biology_uses_cheap_tier_once` | 1 (`CHEAP_TEXT`) | PASS |
| 2 | Difficult calculus → advanced | `test_advanced_calculus_uses_reasoning_tier` | 1 (`ADVANCED_REASONING`) | PASS |
| 3 | Coding help, no execution | `test_coding_help_never_executes_code` | 1 | PASS |
| 4 | Follow-up gets context | `test_follow_up_receives_prior_turns` | 2 | PASS |
| 5 | Simpler explanation changes pedagogy | `test_student_can_change_explanation_depth` | 2 | PASS |
| 6 | Non-Pro → zero calls | `test_non_pro_student_makes_zero_model_calls` | **0** | PASS |
| 7 | Quota exhausted → zero calls | `test_quota_exhausted_makes_zero_model_calls` | **0** | PASS |
| 8 | Timeout → controlled fallback | `test_provider_timeout_falls_back_within_the_attempt_cap` | 2 (capped) | PASS |
| 9 | Simple question → no verifier | `test_simple_question_never_triggers_a_verifier` | 1 | PASS |
| 10 | Low-confidence STEM → verifier | `test_low_confidence_advanced_stem_triggers_verifier` | 2 | PASS |
| 11 | Duplicate → no duplicate call | `test_duplicate_event_makes_no_second_provider_call` | 1 | PASS |
| 12 | Persona version reflected | `test_persona_and_identity_reach_the_prompt` | 1 | PASS |
| 13 | Injection cannot escalate | `test_prompt_injection_cannot_change_entitlement` | **0** | PASS |
| 14 | Cost recorded exactly once | `test_usage_ledger_records_each_call_exactly_once` | 2 = 2 rows | PASS |

Plus: `test_repeated_failures_stop_at_the_cap`,
`test_injection_stays_in_the_untrusted_half_of_the_prompt`,
`test_media_without_brief_makes_zero_model_calls`,
`test_answer_is_persisted_to_the_conversation`.

## 4. Migrations

```bash
$ alembic upgrade head
INFO  Running upgrade f80deb6c45fc -> 71e302352594, phase 02 idempotency claim tracking + model catalog seed

$ alembic downgrade -1
INFO  Running downgrade 71e302352594 -> f80deb6c45fc, ...

$ alembic upgrade head
INFO  Running upgrade f80deb6c45fc -> 71e302352594, ...

$ alembic current
71e302352594 (head)
```

Seeded catalog (10 rows, both vendors):

```
ADVANCED_REASONING anthropic claude-opus-5      ADVANCED_REASONING openai o4-mini
CHEAP_TEXT         anthropic claude-haiku-4-5   CHEAP_TEXT         openai gpt-4.1-mini
STANDARD_TUTOR     anthropic claude-sonnet-5    STANDARD_TUTOR     openai gpt-4.1
VERIFIER_PRIMARY   anthropic claude-sonnet-5    VERIFIER_PRIMARY   openai gpt-4.1
VERIFIER_SECONDARY anthropic claude-haiku-4-5   VERIFIER_SECONDARY openai gpt-4.1-mini

feature_flags: ai_enabled=true
```

## 5. Live scenario traces

### CLI harness (`scripts/chat.py`)

```bash
$ python scripts/chat.py --say "What is photosynthesis?" --say "why does that matter?"
You: What is photosynthesis?
TutorTwin [SEND_TEXT]: [fake model] ...
  status=COMPLETED  paid_model_calls=1
You: why does that matter?
TutorTwin [SEND_TEXT]: [fake model] ...
  status=COMPLETED  paid_model_calls=1

Cost trace for this database: 2 provider call(s), estimated $0.001260
```

```bash
$ python scripts/chat.py --plan FREE --phone "+919999000077" --say "Explain quadratic equations"
TutorTwin [SHOW_UPGRADE]: AI Tutor is available on the Pro plan.
  status=REJECTED  paid_model_calls=0

Cost trace for this database: 2 provider call(s), estimated $0.001260
```

The total did not move: the FREE request cost nothing.

### Persisted ledger

```
CHEAP_TEXT     | claude-haiku-4-5 | in=120 out=60 | cost_micros=420 | rate=2026-08
STANDARD_TUTOR | claude-sonnet-5  | in=120 out=60 | cost_micros=840 | rate=2026-08
```

```
messages:        STUDENT | -    ASSISTANT | BIOLOGY
                 STUDENT | -    ASSISTANT | GENERAL_TUTORING
request_states:  COMPLETED -    COMPLETED -    REJECTED ENTITLEMENT_INACTIVE
```

### HTTP API

```bash
$ curl -s http://127.0.0.1:8111/readyz
{"status":"ready","checks":{"database":"ok"}}

$ curl -X POST /v1/events  (Pro student, no vendor key configured)
{"conversation_id":"0d3376c6-...","status":"COMPLETED",
 "outbound_actions":[{"type":"SEND_TEXT","text":"TutorTwin - AI Assistant for Anita Sharma
  is being set up and cannot answer questions yet. Please try again shortly."}],
 "usage":{"paid_model_calls":0},"idempotent_replay":false}

$ curl -X POST /v1/events  (same event again)
{... "idempotent_replay":true}
```

### Routing trace (structured log)

```json
{"capability": "MATH", "difficulty": "ADVANCED", "route_reason": "rule_score_8",
 "budget_outcome": "ALLOW_WITH_VERIFICATION", "budget_reason": "ADVANCED_STEM",
 "model_alias": "ADVANCED_REASONING", "estimated_tokens": 557,
 "prompt_version": "2.0:safety.v2:identity.v2:persona.v2:capability.v2:format.v2:MATH:HINT_FIRST",
 "event": "request_routed", "request_id": "req_72939...", "correlation_id": "corr_fa9ec...",
 "event_id": "e9", "level": "info", "timestamp": "2026-08-31T05:00:27.873965Z"}
```

Every routing decision is auditable: capability, why, tier, budget reason, and
the exact prompt version used.

### Log safety

```bash
$ grep -c "photosynthesis" server.log   → 0   # student content NOT logged
$ grep -c "local-dev-key" server.log    → 0   # secret NOT logged
```

## 6. Static cleanup scan

```bash
$ grep -rnE "TODO|FIXME|NotImplementedError|console\.log|shell=True|eval\(|verify=False|pickle\.loads|allow_origins" src scripts migrations
# no matches

$ grep -rnE "claude-|gpt-4|o4-mini" src --include="*.py" | grep -v providers/registry.py | grep -v adapter.py
# no matches - vendor model IDs appear only in the registry, the adapters' docs, and the seed migration
```

## 7. Cost accounting

| Scenario | OpenAI | Anthropic | Total calls | Ledger rows |
|---|---|---|---|---|
| Simple biology | 0 | 0 (fake) | 1 | 1 |
| Advanced calculus | 0 | 0 (fake) | 1 | 1 |
| Low-confidence STEM + verifier | 0 | 0 (fake) | 2 | 2 |
| Timeout → fallback | 0 | 0 (fake) | 2 | 2 |
| FREE student | 0 | 0 | **0** | **0** |
| Quota exhausted | 0 | 0 | **0** | **0** |
| Duplicate event | 0 | 0 | **0** | **0** |
| Media without brief | 0 | 0 | **0** | **0** |
| Prompt injection (FREE) | 0 | 0 | **0** | **0** |

**Real money spent during Phase 02: $0.00.** No live vendor call was made.

## 8. Defects found and fixed during this phase

Three came from an adversarial design review of my own plan; all three were
verified against real Phase 01 code before being fixed.

1. **Transaction held across the provider network call.**
   `api/routes/events.py` wrapped all of `handle_event` in `session_scope()`.
   Adding a model call there would pin one of 2-4 pooled Postgres connections
   for the full model latency — the exact thing `db/engine.py` forbids.
   *Fixed:* the route opens no session; the entry service splits into TX1 →
   (no transaction) model call → TX2.

2. **Quota-exhausted student could still spend.** Phase 01 gated only on
   `entitlement.allows_paid_ai`. A Pro student past their daily cap would have
   reached a provider.
   *Fixed:* the budget policy is wired into the pipeline; proven by
   `test_quota_exhausted_makes_zero_model_calls`.

3. **Stale idempotency claim answered students with silence, forever.** A
   container dying between claiming the key and storing the response left a
   permanent empty claim; every redelivery returned an empty `COMPLETED`.
   *Fixed:* `claimed_at` / `completed_at` columns make an abandoned claim
   re-claimable after 5 minutes, and `load_idempotent_response` now filters on
   `completed_at` so an in-flight claim is never replayed as an empty success.

Found by my own testing:

4. **Fallback never ran.** With `max_attempts=2`, the primary's retry consumed
   the whole budget, so the fallback alias was never tried — "fall back to
   another model" silently meant "retry the same failing model twice".
   *Fixed:* the gateway reserves one attempt per remaining alias.

5. **Cost metrics were being redacted.** A bare `token` in the secret-key
   pattern matched `input_tokens`, `output_tokens` and `estimated_tokens`,
   destroying the cost trace in every log line.
   *Fixed:* the pattern now matches auth-shaped token names only; regression
   tests assert both directions.

6. **Router misroutes.** "Prove this Taylor series converges" fell through to
   `GENERAL_TUTORING`, and "past tense grammar rule" lost to the generic
   "what is" opener.
   *Fixed:* advanced-math vocabulary scores as a subject signal, and
   high-precision subject markers outrank generic openers.

7. **1,430 lines of dead duplicate code.** A design subagent wrote alternate
   `policies/budget.py` and `policies/intent.py` implementations into the repo.
   Nothing imported them.
   *Deleted.* Coverage rose from 84% to 92% as a result.

## 9. Changed files

Phase 02 added 14 modules and 5 test files; 68 Python files total.

```
src/tutortwin/
  domain/       capabilities.py  provider.py  budget.py          (new)
  orchestration/router.py  entry_service.py                      (new / rewritten)
  policies/     budget_policy.py                                 (new)
  providers/    gateway.py  registry.py  fake_models.py
                anthropic_adapter.py  openai_adapter.py          (new)
  capabilities/ executor.py                                      (new)
  services/     prompts.py  context.py                           (new)
  repositories/ catalog.py                                       (new)
  api/          dependencies.py  routes/events.py                (modified)
  db/models.py  config.py  observability/logging.py              (modified)
migrations/versions/71e302352594_phase_02_...py                  (new)
scripts/chat.py                                                  (new)
tests/unit/     test_router.py  test_budget_policy.py  test_confidence.py
                test_prompts_and_context.py  test_vendor_adapters.py   (new)
tests/integration/test_meta_agent.py                             (new)
docs/tutortwin/ ORCHESTRATION.md  COST_CONTROLS.md  acceptance/PHASE_02.md
```

`git diff --stat` is unavailable: the workspace is not a git repository.

## 10. Isolation compliance

The Lead Intake repository, the NX Tutors website, and production MySQL were not
cloned, opened, inspected, or connected. Identity, entitlement, tutor and
outbound all remain deterministic fakes. The `leadintakedb` database on the same
local PostgreSQL server was left untouched.

## Limitations

1. **No live provider call was made.** No vendor API key was available. Adapters
   are unit-tested against stubbed SDK clients covering success, truncation,
   refusal, timeout, rate-limit, auth and bad-request paths. Live smoke:
   ```bash
   TUTORTWIN_ANTHROPIC_API_KEY=sk-ant-... python scripts/chat.py --live \
     --say "Explain photosynthesis in two sentences"
   ```
   Then confirm one `usage_ledger` row with a real `provider_request_id` and
   non-zero token counts.

2. **Prompt caching is unverified against a live vendor.** The request shape is
   correct and asserted in tests (`cache_control: ephemeral` on the Anthropic
   prefix; prefix-first ordering for OpenAI), but a real cache *hit* can only be
   confirmed by checking `usage.cache_read_input_tokens > 0` on a second live
   call within the TTL.

3. **Quota is read, not reserved.** Two concurrent requests can both observe the
   same count and both proceed, so a limit is soft by at most the number of
   in-flight requests. A hard reservation needs a row lock held across the
   provider call, which the serverless design forbids. Deliberate trade,
   documented in COST_CONTROLS.md.

4. **Ambiguous-intent escalation to a cheap classifier is not implemented.**
   `IntentDecision.used_model` exists and is always `False`. The rule table
   resolves the tested corpus; adding a paid classification call before there is
   evidence it is needed would add cost for no measured benefit.

5. **Conversation summaries are truncation-based, not model-generated.**
   Deliberate: a model summary would add a paid call per turn on long
   conversations. Revisit in Phase 04 alongside memory.

6. **Plan policies are code constants**, not `plan_policies` rows. Phase 06
   (admin control plane) moves them to the database.

## Result

**Phase 02 complete.** 206 tests passing, 92% coverage, ruff and strict mypy
clean, migrations reversible, all 14 mandatory scenarios proven with explicit
provider-call counts, $0.00 spent. Stopping here per the loop protocol.
