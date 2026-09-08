"""Domain ports. TutorTwin business code depends only on these.

Phases 08/09 replace the Identity/Entitlement/Tutor/Outbound implementations with
real NX adapters without touching orchestration.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from tutortwin.domain.events import MediaRef, OutboundAction, SubjectRef
from tutortwin.domain.models import (
    EntitlementSnapshot,
    ResolvedSubject,
    TutorProfile,
)


@runtime_checkable
class IdentityGateway(Protocol):
    async def resolve(self, subject: SubjectRef) -> ResolvedSubject | None:
        """Map a channel-native identity to a TutorTwin subject, or None."""
        ...


@runtime_checkable
class EntitlementGateway(Protocol):
    async def snapshot(self, subject: ResolvedSubject) -> EntitlementSnapshot:
        """Current plan/entitlement. Must never call a paid AI provider."""
        ...


@runtime_checkable
class TutorGateway(Protocol):
    async def assigned_tutor(self, subject: ResolvedSubject) -> TutorProfile | None: ...


@runtime_checkable
class OutboundGateway(Protocol):
    async def deliver(
        self, subject: ResolvedSubject, actions: tuple[OutboundAction, ...]
    ) -> None: ...


@runtime_checkable
class BlobStore(Protocol):
    """Object storage port. Cloudflare R2 in production; never Postgres."""

    async def put(self, key: str, data: bytes, content_type: str) -> str: ...

    async def get(self, key: str) -> bytes: ...

    async def signed_url(self, key: str, expires_in_seconds: int) -> str: ...


@runtime_checkable
class TaskQueue(Protocol):
    """Durable async work. Cloud Tasks in production. Payload carries IDs only."""

    async def enqueue(self, job_id: str, *, delay_seconds: int = 0) -> None: ...


@runtime_checkable
class LLMProvider(Protocol):
    """Paid text generation. No implementation exists in Phase 01 by design."""

    async def complete(self, model_alias: str, prompt: str, *, max_output_tokens: int) -> str: ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    async def embed(
        self, model_alias: str, texts: tuple[str, ...]
    ) -> tuple[tuple[float, ...], ...]: ...


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...


@runtime_checkable
class IdGenerator(Protocol):
    def new_id(self) -> str: ...


@runtime_checkable
class MediaInspector(Protocol):
    """Cheap, local-only metadata check. Never OCR, never vision."""

    async def describe(self, media: MediaRef) -> dict[str, str]: ...
