# TutorTwin Runbook

What to do when something is wrong, and what to check first. Written for
somebody paged at 3am who did not build this.

The single most useful fact: **every cost control fails closed.** If you are
unsure whether to pull a switch, pull it. The service degrades to deterministic
answers and refuses to spend; it does not lose data.

---

## Orientation

| Thing | Where |
|---|---|
| API | Cloud Run `tutortwin-api-<env>` |
| Control plane | Cloud Run `tutortwin-admin-<env>` |
| Async work | Cloud Tasks `tutortwin-media-<env>` |
| Database | Neon (pooled DSN for the app, direct DSN for migrations) |
| Objects | Cloudflare R2, private bucket, 7-day lifecycle |
| Secrets | Google Secret Manager, `*-<env>` |
| Retention sweep | Cloud Scheduler `tutortwin-retention-<env>`, 03:17 UTC |

```bash
API=$(gcloud run services describe tutortwin-api-$ENV --region=$REGION --format='value(status.url)')
curl -s $API/healthz     # is the process alive
curl -s $API/readyz      # can it serve traffic (checks the database)
```

`/healthz` never touches a dependency. If it answers and `/readyz` does not, the
container is fine and something it depends on is not.

---

## First five minutes

```bash
# 1. What is the service saying about itself?
gcloud run services logs read tutortwin-api-$ENV --region=$REGION --limit=100

# 2. Is it spending?  (control plane -> Dashboard, or:)
#    Any of these being non-zero and rising is the thing to look at.
#      requests_failed, quota_blocked, jobs_failed, latency_p95_ms

# 3. Is the queue backed up?
gcloud tasks queues describe tutortwin-media-$ENV --location=$REGION
```

Log events worth knowing by name:

| Event | Means |
|---|---|
| `service_started` | A cold start. `internal_auth=oidc` confirms OIDC is on. |
| `request_rejected` | A gate refused. `error_code` says which. |
| `provider_blocked` | A vendor is over budget or failing; the fallback is being used. |
| `job_deferred_concurrency` | The heavy-job ceiling is shedding load. Normal under burst. |
| `media_job_failed` | An extraction attempt failed. Retryable unless `FAILED_PERMANENT`. |
| `retention_swept` | The nightly sweep ran. `errors` > 0 means R2 refused a delete. |
| `oidc_verification_failed` | Someone called `/internal/*` with a bad token. |

**Never in the logs:** student message text, any secret, any DSN. If you see one,
that is an incident in itself — see *Secret exposure* below.

---

## The kill switches

Ten of them, in the control plane under **Feature flags**. Each takes effect on
the next request; there is nothing to restart and no deploy.

```
pdf_processing     image_processing   voice_processing   mock_tests
verifier           provider_openai    provider_anthropic advanced_model
rag_retrieval      code_sandbox
```

Flipping one is a high-risk action: it needs a typed reason and writes an audit
event. That is deliberate — three weeks later somebody will ask why PDFs were off
for two days.

**The global one** is the `ai_enabled` flag. With it off, every request is
answered deterministically and nothing reaches a paid provider.

---

## Symptom → action

### The bill is climbing

```bash
# Control plane -> Costs, grouped by provider, then by capability, then by student.
```

The ceilings should already have stopped it. In order of what fires first:

1. `TUTORTWIN_SYSTEM_HOURLY_BUDGET_MICROS` — spend velocity. This is the one that
   catches a runaway *while it is running*.
2. `TUTORTWIN_SYSTEM_DAILY_BUDGET_MICROS` — the day's ceiling.
3. `TUTORTWIN_PROVIDER_DAILY_BUDGET_MICROS` — per vendor, so the fallback stays
   available.

If spend is climbing and none has fired, they are set too high for the traffic.
Lower them on the service and redeploy — they are plain environment variables, not
secrets:

```bash
gcloud run services update tutortwin-api-$ENV --region=$REGION \
  --update-env-vars=TUTORTWIN_SYSTEM_HOURLY_BUDGET_MICROS=2000000
```

If it is one student, the control plane can reset or override their entitlement.
If it is one capability, the matching kill switch is faster than a config change.

### A vendor is down or rate-limiting

Usually nothing to do. The gateway retries with exponential backoff and full
jitter, falls back one tier *down* (never up), and after
`TUTORTWIN_PROVIDER_FAILURE_CIRCUIT` consecutive failed calls the circuit opens
and that vendor is skipped entirely.

Do something if the fallback is also that vendor — check `GET /v1/admin/models`
in the control plane. If both aliases resolve to the sick provider, repoint one:
**Models & routing** → change the route. High-risk, audited, takes effect on the
next request.

If both vendors are down, flip `ai_enabled` off. Students get a deterministic
"cannot answer right now" instead of a 30-second wait followed by an error.

### Jobs are piling up

```bash
gcloud tasks queues describe tutortwin-media-$ENV --location=$REGION
```

- **Depth rising, `job_deferred_concurrency` in the logs** — working as intended.
  The ceiling is shedding load and Cloud Tasks is re-delivering on its own
  backoff. Raise `TUTORTWIN_HEAVY_JOB_MAX_CONCURRENCY` only if Neon has the
  connections to spare; it is deliberately below what Cloud Run could produce.
- **Depth rising, no deferrals** — the handler is failing. Check `media_job_failed`
  for the error type, then the control plane's **Jobs** view for `last_error`.
- **Everything `FAILED_PERMANENT`** — attempts are exhausted. Fix the cause, then
  retry from the control plane (Jobs → Retry). Retrying does not erase attempt
  history.

To pause dispatch without losing work:

```bash
gcloud tasks queues pause tutortwin-media-$ENV --location=$REGION
# ... fix ...
gcloud tasks queues resume tutortwin-media-$ENV --location=$REGION
```

### A job is stuck in RUNNING

The handler catches its own crashes and re-arms the row, so this should not
happen. If a container was killed hard enough to skip that, the concurrency
counter treats a `RUNNING` row older than 15 minutes as stale and stops counting
it — so the fleet unblocks itself.

The row stays `RUNNING`. Cancel it from the control plane, or re-drive it:

```bash
curl -X POST $API/internal/jobs/run \
  -H "Authorization: Bearer $(gcloud auth print-identity-token --audiences=$API)" \
  -H 'content-type: application/json' -d '{"job_id":"<uuid>"}'
```

### The database is unreachable

`/readyz` returns 503 and Cloud Run stops sending traffic to that instance;
`/healthz` keeps answering so healthy containers are not killed and restarted
into the same outage.

1. Check Neon's status and the connection count. The per-container pool is 2 + 2,
   so the fleet ceiling is roughly `4 × max_instances` — with
   `api_max_instances = 10` that is 40.
2. If Neon is up and connections are exhausted, lower `max-instances`. It is a
   connection ceiling as much as a scaling one:
   ```bash
   gcloud run services update tutortwin-api-$ENV --region=$REGION --max-instances=4
   ```
3. Nothing is lost meanwhile. Inbound events fail with 503; Cloud Tasks retries,
   and the message bridge retries.

### R2 is unreachable

Extraction fails and retries. The retention sweep leaves rows whose blob it could
not delete — deliberately, because a deleted row is the only handle on an
orphaned object. `retention_swept` with `errors > 0` is the signal; the next
night's sweep picks them up.

Media *intake* still works: the reference is recorded and the job queued. Only
the fetch fails.

### Someone reports a wrong or harmful answer

1. Control plane → **Conversations** → find the turn. It shows the normalized
   text, the capability, every model call, the RAG chunks used, latency and cost.
2. `prompt_version` on the request identifies exactly which prompt blocks
   produced it — old answers stay explainable because prompt versions are
   immutable.
3. If a persona is at fault, draft a new version and activate it. Activation is
   high-risk and audited; the previous version is deactivated, never deleted.

### Secret exposure

If a credential reaches a log, a ticket, or a screen share:

1. **Rotate first, investigate second.**
   ```bash
   echo -n "$NEW" | gcloud secrets versions add <name>-$ENV --data-file=-
   gcloud run services update tutortwin-api-$ENV --region=$REGION  # picks up :latest
   ```
2. Disable the old version once traffic has moved:
   ```bash
   gcloud secrets versions disable <old-version> --secret=<name>-$ENV
   ```
3. For a **provider key**, revoke it at the vendor as well — the value in Secret
   Manager is a copy, not the authority.
4. For the **internal API key**, rotate it and Lead Intake's copy together; there
   is a window where one is ahead of the other, so do it during low traffic.
5. Check the audit log for what the exposure window could have reached.

---

## Rotation schedule

| Credential | Every | Notes |
|---|---|---|
| `internal-api-key` | 90 days | Coordinate with Lead Intake (Phase 08) |
| Provider keys | 90 days | Rotate at the vendor, then Secret Manager |
| R2 access key | 180 days | Create the new token before deleting the old |
| Neon password | 180 days | Both DSNs, pooled and direct |
| Admin passwords | On demand | `tutortwin-admin reset-password --email ...` |

OIDC needs no rotation. That is the point of it: tokens are minted per request
and expire on their own, so there is nothing to leak that outlives the minute it
was leaked in.

---

## Deploys

```bash
# 1. Migrate first, on the DIRECT DSN. A pooled endpoint breaks DDL and
#    advisory locks.
TUTORTWIN_DATABASE_MIGRATION_URL=$DIRECT_DSN alembic upgrade head

# 2. Then shift traffic.
gcloud run deploy tutortwin-api-$ENV --region=$REGION --image=$IMAGE
```

Migrations run **before** the new code, so they must be backwards compatible with
the code currently serving: add columns nullable, backfill later, drop in a
subsequent release. Every Phase 07 migration follows this.

Rollback is a traffic shift, not a database change:

```bash
gcloud run services update-traffic tutortwin-api-$ENV --region=$REGION \
  --to-revisions=<previous>=100
```

Do **not** roll a migration back to recover from a bad deploy. Roll the code back
first; decide about the schema afterwards, when nobody is waiting.

---

## Backup and recovery

### Postgres

Neon's own point-in-time restore is the mechanism; there is no separate dump.
Retention depends on the Neon plan — confirm it is at least 7 days.

```bash
# Restore to a branch and inspect BEFORE repointing anything.
neonctl branches create --name recovery-$(date +%s) --parent-timestamp <iso8601>
```

Recovery order after a restore:

1. Point `TUTORTWIN_DATABASE_MIGRATION_URL` at the branch and run
   `alembic upgrade head` — the branch may be behind.
2. Verify: `SELECT count(*) FROM tutortwin_subjects;` and the newest
   `request_events.created_at`.
3. Repoint `TUTORTWIN_DATABASE_URL` and redeploy.

**Jobs in flight are lost by a restore.** They are re-driven by Cloud Tasks only
if the task still exists; a task whose row vanished settles as `NOT_FOUND`, which
is a no-op. Re-uploading the file is the recovery path, and it is cheap because
extraction is content-addressed.

### R2

The bucket lifecycle rule expires objects under `media/` after 7 days, and that
rule — not the application — is what reclaims storage. Objects are **not** backed
up: they are derived from what a student sent and are re-fetchable from the
source for as long as the source keeps them.

Losing the bucket loses cached extractions, not conversations, progress or
learning artifacts. All of those are in Postgres.

---

## Retention and deletion

| Data | Kept | Removed by |
|---|---|---|
| Media objects (blobs) | 7 days | R2 lifecycle rule |
| Media rows + extracted text | 7 days | Nightly `/internal/retention/sweep` |
| Conversations and messages | Indefinite | Student data deletion |
| Learning artifacts, progress, memories | Indefinite | Student data deletion |
| `usage_ledger` | Indefinite | Never — it is cost evidence |
| `audit_events` | Indefinite | Never — it is the record of who did what |

The sweep is idempotent and bounded (200 objects per run by default). To run it
by hand:

```bash
curl -X POST $API/internal/retention/sweep \
  -H "Authorization: Bearer $(gcloud auth print-identity-token --audiences=$API)" \
  -H 'content-type: application/json' -d '{"batch_size":500}'
```

**A deletion request** is served from the control plane: Students → the student →
*Delete learning data*. It requires typing the student's identity, which the API
checks against the row it is about to erase, and it removes memories, progress,
documents, assessments, decks, artifacts and media.

Two things survive deliberately: the identity row, and the audit record of the
deletion. A record that someone deleted a student's data is not itself student
data, and removing it would make the deletion unprovable.

---

## Locked out

```bash
python -m tutortwin.cli.admin list                     # who exists
python -m tutortwin.cli.admin unlock --email ops@...   # clear a lockout
python -m tutortwin.cli.admin reset-password --email ops@...   # prints once
```

Five failed logins locks an account for 15 minutes. That is the account being
attacked, not the operator being punished — `unlock` is safe to run.

`bootstrap` refuses once any administrator exists. If every super-admin is locked
out, `reset-password` on one of them is the way back in; there is deliberately no
command that creates a second first-administrator.
