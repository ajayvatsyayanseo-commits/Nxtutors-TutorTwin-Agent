# TutorTwin Event Model

Contract version **1.0** (`tutortwin.domain.events`).

Every channel normalizes into this shape before reaching business code. No
channel-native payload (WhatsApp, web, test harness) is ever seen by
orchestration.

## NormalizedEvent

| Field | Type | Notes |
|---|---|---|
| `contract_version` | `"1.0"` | Literal; a future version is an explicit change |
| `event_id` | str | Unique per event from the source |
| `request_id` | str | This processing attempt |
| `correlation_id` | str | Spans a whole interaction, across services |
| `source` | str | Channel, e.g. `test_harness`, `lead_intake` |
| `source_agent` | str | Default `standalone` |
| `subject` | SubjectRef | `external_type` + `external_id` |
| `message` | InboundMessage | See below |
| `context` | EventContext | `locale`, default `en-IN` |
| `occurred_at` | datetime | Source timestamp |

Models are `extra="forbid"` and frozen: an unknown field is a 422, not a silent
ignore, and an event cannot be mutated after validation.

## Message types

`TEXT` · `AUDIO` · `IMAGE` · `PDF` · `DOCUMENT` · `ACTION` · `SYSTEM`

```
InboundMessage:
  message_id  str
  type        MessageType
  text        str | None      (max 16,000 chars)
  media       MediaRef | None
```

## MediaRef - a pointer, never bytes

```
provider        str          e.g. "test", "whatsapp"
media_id        str
mime_type_hint  str | None
size_hint       int | None
filename        str | None
```

The reference is deliberately payload-free so the brief gate can hold it with
zero download, zero OCR, zero vision and zero embedding cost.

## Idempotency

```python
idempotency_key = f"{source}:{message_id or event_id}"
```

Scoped by `source` so the same message id from two channels cannot collide.
Enforced by the `uq_idempotency_key` unique constraint, not by application
logic - see [DATA_MODEL.md](DATA_MODEL.md).

A duplicate returns the stored response with `idempotent_replay: true` and
performs no work: no new conversation, no new messages, no new delivery.

## Media brief gate

Implemented in `capabilities/placeholder.py` as deterministic code, not a
system prompt. Media arriving with no meaningful caption (< 3 chars) yields a
single `ASK_FILE_BRIEF` action. Nothing is downloaded, extracted or embedded.

Valid briefs look like "solve question 4", "summarize pages 12-15",
"check my answers".

Note the ordering: entitlement is evaluated *before* the brief gate, so an
ineligible student never reaches media handling at all.

## Outbound actions

`SEND_TEXT` · `SEND_DOCUMENT` · `SEND_IMAGE` · `SEND_AUDIO` · `SHOW_UPGRADE` ·
`ASK_FILE_BRIEF` · `SHOW_MENU` · `TUTOR_NOTIFICATION`

## EventResponse

```
conversation_id    str | None      null when rejected before conversation creation
status             COMPLETED | REJECTED | FAILED
outbound_actions   tuple[OutboundAction, ...]
handoff            null            (Phase 08)
usage              {paid_model_calls: int}
idempotent_replay  bool
```

`usage.paid_model_calls` is always `0` in Phase 01 and is the machine-readable
cost evidence carried by every response.
