# TutorTwin Deployment

Status: **Phase 07.** Two Cloud Run services, a Cloud Tasks queue, Neon and R2,
all described by Terraform in [`infra/terraform/`](../../infra/terraform).

Nothing here has been applied against a live Google Cloud account in this
environment — see [Limitations](#limitations). The configuration is validated
(`terraform validate` passes) and every command below is written to be run, not
to be read.

## Topology

| Component | Service | Scale | Billing |
|---|---|---|---|
| `tutortwin-api` | Cloud Run | min 0, max 10 | request-based (`cpu_idle`) |
| `tutortwin-admin` | Cloud Run, **separate service** | min 0, max 3 | request-based |
| Async work | Cloud Tasks → OIDC push to `/internal/jobs/run` | — | per task |
| Retention sweep | Cloud Scheduler → OIDC push to `/internal/retention/sweep` | nightly | per job |
| Database | Neon PostgreSQL + pgvector | serverless | — |
| Objects | Cloudflare R2 Standard, private | — | no egress fee |
| Secrets | Google Secret Manager | — | per version |

No Redis. No AWS S3. No Fargate/ECS/Kubernetes. No NAT Gateway. No EC2. No
permanently running worker. No RabbitMQ, Kafka or Celery.

**Why no VPC.** Every dependency — Neon, R2, OpenAI, Anthropic — is a public TLS
endpoint. A VPC connector would add a fixed hourly charge and a NAT gateway to a
design whose entire point is costing nothing while idle, and would buy no
isolation those four already provide through authentication.

**Why two services.** The control plane is Next.js and the API is Python. One
container would mean one deploy for two things that change at different rates, a
Node toolchain inside the API's attack surface, and a cold start that pays for
both runtimes. They also scale differently: the API is called by a queue and a
message bridge, the control plane by a handful of humans.

---

## First deploy

### 0. Outside Google

Two managed services are created by hand. They are not in Terraform because
their providers would add two credentials and two more `apply` failure modes in
exchange for creating one database and one bucket.

**Neon** — create the project, enable `pgvector`, and take both connection
strings:

- **pooled** → `TUTORTWIN_DATABASE_URL` (the app; many short-lived containers)
- **direct** → `TUTORTWIN_DATABASE_MIGRATION_URL` (Alembic; a transaction-pooling
  endpoint breaks DDL and advisory locks)

**Cloudflare R2** — create the bucket and an API token scoped to it:

- **Private.** No public-read policy, ever. Access is by signed short-lived URL
  or backend streaming.
- Lifecycle rule: expire objects under `media/` after 7 days. The adapter also
  writes an `expires-at` metadata field so an object is self-describing, but the
  bucket rule is what actually reclaims storage.
- Key layout is `media/<subject>/<aa>/<sha256>.<ext>` — content-addressed, with
  the owner in the path so ownership is checkable without a lookup.

### 1. Infrastructure

```bash
cd infra/terraform
cp staging.tfvars.example staging.tfvars   # fill in project, region, images
terraform init
terraform apply -var-file=staging.tfvars
```

The first apply cannot know the service's own URL, which two settings need. Read
the outputs and apply again:

```bash
terraform output          # tasks_target_url, oidc_audience, tasks_service_account
# add those to api_env in staging.tfvars, then:
terraform apply -var-file=staging.tfvars
```

### 2. Secrets

Terraform creates the secret *containers*; it never holds a value. State is
stored, shared and diffed, and a value in state is a value in a bucket somebody
can read.

```bash
for s in database-url database-migration-url internal-api-key \
         anthropic-api-key openai-api-key r2-access-key-id r2-secret-access-key; do
  read -rs -p "$s: " v && echo
  echo -n "$v" | gcloud secrets versions add "$s-staging" --data-file=-
done
```

### 3. Images

```bash
REPO=$(terraform output -raw artifact_repository)
gcloud auth configure-docker "${REPO%%/*}"

docker build -t "$REPO/api:$(git rev-parse --short HEAD)" .
docker build -t "$REPO/admin:$(git rev-parse --short HEAD)" apps/admin
docker push "$REPO/api:$(git rev-parse --short HEAD)"
docker push "$REPO/admin:$(git rev-parse --short HEAD)"
```

Pin by digest in production. A tag can be moved; a digest cannot, and "which
build is actually running" should not be a question with two answers.

### 4. Migrate, then deploy

```bash
TUTORTWIN_DATABASE_MIGRATION_URL="$DIRECT_DSN" alembic upgrade head
terraform apply -var-file=staging.tfvars   # with the new image tags
```

Migrations run before the new code, so each must be backwards compatible with
the code currently serving.

### 5. First administrator

```bash
python -m tutortwin.cli.admin hash-password        # prompts; nothing hits shell history
TUTORTWIN_ADMIN_BOOTSTRAP_EMAIL=ops@nxtutors.in \
TUTORTWIN_ADMIN_BOOTSTRAP_PASSWORD_HASH='<the hash>' \
  python -m tutortwin.cli.admin bootstrap
```

It refuses once any administrator exists. A bootstrap that also works on a live
system is a back door with a friendly name.

---

## Configuration

The full list with comments is [`.env.example`](../../.env.example). What matters
for a deployment:

### Refuses to start without these

`Settings.require_deployable()` runs before the port opens, in `staging` and
`production`:

| Missing | Would otherwise |
|---|---|
| `TUTORTWIN_INTERNAL_API_KEY` | leave `/v1/events` accepting anonymous requests |
| `TUTORTWIN_R2_*` | write media to a container filesystem that disappears |
| `TUTORTWIN_TASKS_*` | record jobs and never dispatch them |
| `TUTORTWIN_DATABASE_MIGRATION_URL` | run migrations through a pooled endpoint |

Each of those fails **silently** otherwise. A service that boots and loses data
is worse than one that will not boot.

### Which adapter is chosen

Decided once, in `api/dependencies.py`, from whether the credentials exist —
never by an `environment == "production"` check at a use site:

| Setting present | Storage | Queue |
|---|---|---|
| no | filesystem under `media_root` | recorder (enqueues nothing) |
| yes | Cloudflare R2 | Cloud Tasks with OIDC |

That is the whole of the environment-profile mechanism. `local`, `test`,
`staging` and `production` differ in configuration values and in
`require_deployable()`; no business logic branches on the environment name.

### Sizing

`1 vCPU / 1Gi`, `concurrency 80`, `timeout 600s`.

- **Memory** is set by PDF rasterisation for OCR — the peak in the pipeline. The
  40-page ceiling the media limits allow fits in roughly 700MB; below 1Gi that
  page count OOMs mid-extraction, above it the difference is billed idle.
- **Concurrency 80** because the work is IO-bound — waiting on a provider or on
  Neon. A low concurrency would start instances to sit idle. The Postgres pool
  (2 + 2 per container) is what actually bounds database load, not this number.
- **`max_instances` is a budget control**, not only a scaling knob: it bounds
  concurrent Postgres connections (`4 × max_instances`) and concurrent provider
  spend at the same time.
- **`timeout 600s`** must exceed the queue's 540s dispatch deadline, or Cloud Run
  cancels a job push that Cloud Tasks still believes is running.

### Probes and shutdown

- **Startup probe** on `/readyz` — gates traffic on the database being reachable,
  so an instance that cannot serve is never sent a request.
- **Liveness probe** on `/healthz` — touches nothing. A database blip must not
  get healthy containers killed and restarted into the same blip.
- **SIGTERM** stops new connections and drains in-flight requests, bounded by
  `TUTORTWIN_SHUTDOWN_GRACE_SECONDS` (20s). It must be no larger than Cloud Run's
  own termination grace period, or the drain is cut off mid-request anyway.

---

## Queue and internal authentication

```
Cloud Tasks --(OIDC, audience = service URL)--> POST /internal/jobs/run
Cloud Scheduler --(same identity)------------> POST /internal/retention/sweep
```

The task body carries **only** `{"job_id": "..."}`. The worker reads all durable
state from Postgres, so a retry cannot act on a stale snapshot and the payload
cannot bloat.

Authentication is layered, and both layers are real:

1. **IAM** — only the `tutortwin-invoker` service account has `run.invoker`.
2. **Application** — `OidcVerifier` checks the token's audience *and* its service
   account. The audience alone would accept any Google-signed token minted for
   this URL, which is a much larger set of callers than intended.

`/v1/events` keeps the shared secret, because its caller (Lead Intake, Phase 08)
is not on Google's identity plane. The two paths are separate methods so that
turning OIDC on for the queue can never silently stop authenticating the event
ingress.

### Retries, twice over

Bounded by the queue **and** by the `jobs.max_attempts` column. The duplication
is deliberate: a queue recreated with defaults cannot produce an unbounded retry
storm against a paid provider, because the row refuses first.

| Job type | Attempts | Backoff |
|---|---|---|
| `MEDIA_EXTRACT` | 3 | 30s → 600s, exponential, full jitter |
| `RETENTION_SWEEP` | 5 | 60s → 3600s |

The handler's HTTP status tells Cloud Tasks what to do: **200** for finished or
permanently failed, **503** for "try again", **429** for "the fleet is saturated,
come back later". Returning 200 on a transient failure silently drops the
student's document.

---

## OCR

Tesseract is installed in the image. Every page it reads locally is a vision call
not paid for, and the planner routes to vision only when it reports itself
unavailable or returns low confidence. `eng` only — each language pack is ~15MB
of image for a language this deployment does not serve.

---

## Verifying a deploy

```bash
API=$(terraform output -raw api_url)

curl -sf $API/healthz                                   # {"status":"ok"}
curl -sf $API/readyz                                    # {"status":"ready",...}
curl -s $API/readyz | grep -qi postgres && echo "LEAK"  # must print nothing

# The internal endpoint must refuse an anonymous caller.
test "$(curl -s -o /dev/null -w '%{http_code}' -X POST $API/internal/jobs/run \
  -H 'content-type: application/json' -d '{"job_id":"00000000-0000-0000-0000-000000000000"}')" = 401 \
  && echo "internal endpoint is authenticated"

# And accept the queue's identity.
curl -s -X POST $API/internal/jobs/run \
  -H "Authorization: Bearer $(gcloud auth print-identity-token --audiences=$API)" \
  -H 'content-type: application/json' \
  -d '{"job_id":"00000000-0000-0000-0000-000000000000"}'   # {"state":"NOT_FOUND"}

# One real turn, end to end.
curl -s -X POST $API/v1/events -H "x-internal-key: $INTERNAL_KEY" \
  -H 'content-type: application/json' -d '{
    "event_id":"smoke-1","request_id":"smoke-1","correlation_id":"smoke-1",
    "source":"smoke","subject":{"external_type":"phone","external_id":"+919999000001"},
    "message":{"message_id":"smoke-1","type":"TEXT","text":"what is osmosis"},
    "occurred_at":"2026-01-01T00:00:00Z"}'

# Idempotency: the same call again must replay, not re-spend.
#   -> "idempotent_replay": true

ADMIN=$(terraform output -raw admin_url)
curl -sf -o /dev/null -w '%{http_code}\n' $ADMIN/login      # 200
```

---

## Limitations

- **Not applied to a live account.** No Google Cloud, Neon or Cloudflare
  credentials exist in this environment. `terraform validate` passes and
  `terraform init -backend=false` resolves the provider; `terraform plan` against
  a real project has not been run, so provider-side rejections (quota, API
  enablement, org policy) are unproven.
- **R2 and Cloud Tasks adapters have never run against the live services.** They
  are wired, selected by configuration and covered by tests against fakes. The
  first real `put` and the first real task push will be in staging.
- **`terraform apply` is a two-pass operation** the first time, because two
  settings need the service's own URL. This is inherent to self-referential Cloud
  Run configuration, not a defect to fix later.
- **No Terraform remote backend is configured.** Add a GCS backend before more
  than one person applies.
