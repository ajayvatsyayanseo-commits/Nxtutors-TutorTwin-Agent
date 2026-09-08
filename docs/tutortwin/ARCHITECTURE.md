# TutorTwin Architecture

Status: Phase 01 (foundation). This document describes running code.

## What exists today

A FastAPI service that accepts a normalized event, resolves identity and
entitlement through ports, persists a conversation turn, and returns a
deterministic reply. **It makes zero paid AI provider calls.**

## Shape

```
HTTP POST /v1/events
  -> BodySizeLimitMiddleware      reject oversized bodies before parsing
  -> RequestContextMiddleware     bind request_id / correlation_id
  -> Pydantic NormalizedEvent     strict validation, extra="forbid"
  -> SharedSecretAuthenticator    internal caller check
  -> TutorTwinEntryService
       1. idempotency claim       unique constraint decides
       2. IdentityGateway         resolve subject
       3. EntitlementGateway      COST GATE - stop here if ineligible
       4. TutorGateway            assigned tutor + persona
       5. conversation            get-or-create open conversation
       6. persist inbound message
       7. capability              deterministic placeholder (no provider call)
       8. persist outbound + deliver via OutboundGateway
  -> EventResponse
```

The order is the design. Identity and entitlement run before anything
expensive, which is what makes "ineligible student costs nothing" a structural
property rather than a policy someone must remember.

## Ports and adapters

Business code depends only on `tutortwin.domain.ports` Protocols:

| Port | Phase 01 adapter | Replaced by |
|---|---|---|
| `IdentityGateway` | `FakeIdentityGateway` | Phase 09 (website) |
| `EntitlementGateway` | `FakeEntitlementGateway` | Phase 09 (website) |
| `TutorGateway` | `FakeTutorGateway` | Phase 09 (website) |
| `OutboundGateway` | `FakeOutboundGateway` | Phase 08 (Lead Intake) |
| `BlobStore` | `InMemoryBlobStore` | Phase 03 (Cloudflare R2) |
| `TaskQueue` | `RecordingTaskQueue` | Phase 03 (Cloud Tasks) |
| `LLMProvider` | `ForbiddenLLMProvider` | Phase 02 (OpenAI/Anthropic) |
| `EmbeddingProvider` | `ForbiddenEmbeddingProvider` | Phase 04 |
| `Clock` | `SystemClock` / `FixedClock` | - |
| `IdGenerator` | `UuidIdGenerator` | - |

`ForbiddenLLMProvider` is a tripwire, not a stub: calling it raises. That turns
"no paid calls happen in Phase 01" into something a test can prove rather than
something we assert in prose.

Every adapter is selected in one place, `api/dependencies.py`. Phases 08 and 09
change that file, not the orchestration.

## Isolation

Phase 01 has no dependency on the Lead Intake repository, the NX Tutors
website, or production MySQL. Nothing from those systems is imported, read, or
assumed. The gateways above are the seams where they will connect.

## Serverless posture

Designed for Cloud Run with min instances 0:

- Small connection pool (2 + 2 overflow) - many short-lived containers.
- `pool_pre_ping` - a frozen container can wake with a dead socket.
- `pool_recycle` 280s - below typical idle timeouts.
- Server-side `statement_timeout` per connection.
- No transaction is held open across a network/provider call.
- No Redis, no S3, no always-on worker, no background thread required for
  correctness.

## Event loop note

Async psycopg cannot run on Windows' ProactorEventLoop. `tutortwin/runtime.py`
installs a selector policy and `tutortwin/__main__.py` drives uvicorn inside a
loop we create (`loop="none"`), because uvicorn otherwise replaces the policy
at startup. On Linux this is a no-op.

## Deliberately not built yet

Model routing, media processing, RAG, memory, learning engine, admin control
plane, and the async job pipeline. The tables and ports they need exist; the
logic arrives in Phases 02-07.
