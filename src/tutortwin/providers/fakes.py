"""Deterministic fake adapters.

These are production code, not test helpers: Phases 01-07 run the whole product
on them. Phases 08/09 swap in real NX gateways behind the same ports.

Determinism rule: same input -> same output, no clock/network/random dependence
except through the injected Clock and IdGenerator.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from uuid import UUID

from tutortwin.domain.events import MediaRef, OutboundAction, SubjectRef
from tutortwin.domain.models import (
    EntitlementSnapshot,
    EntitlementStatus,
    ResolvedSubject,
    SubjectStatus,
    TutorPersona,
    TutorProfile,
)

# Stable namespace so a given external id always maps to the same UUID. This is
# what makes the fakes reproducible across runs and processes.
_NAMESPACE = UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


def deterministic_uuid(*parts: str) -> UUID:
    return uuid.uuid5(_NAMESPACE, "|".join(parts))


class FixedClock:
    """Clock that never moves unless told to. Makes timestamps assertable."""

    def __init__(self, at: datetime | None = None) -> None:
        self._now = at or datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = datetime.fromtimestamp(self._now.timestamp() + seconds, tz=UTC)


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class SequentialIdGenerator:
    def __init__(self, prefix: str = "id") -> None:
        self._prefix = prefix
        self._counter = 0

    def new_id(self) -> str:
        self._counter += 1
        return f"{self._prefix}_{self._counter:06d}"


class UuidIdGenerator:
    def new_id(self) -> str:
        return str(uuid.uuid4())


class FakeIdentityGateway:
    """Resolves any known external id; unknown ids resolve to None.

    `known` maps external_id -> display name. An empty mapping means
    "resolve everyone", which is the convenient default for local harness use.
    """

    def __init__(
        self,
        known: dict[str, str] | None = None,
        *,
        blocked: frozenset[str] = frozenset(),
        resolve_unknown: bool = True,
    ) -> None:
        self._known = known or {}
        self._blocked = blocked
        self._resolve_unknown = resolve_unknown
        self.calls = 0

    async def resolve(self, subject: SubjectRef) -> ResolvedSubject | None:
        self.calls += 1
        if subject.external_id not in self._known and not self._resolve_unknown:
            return None
        return ResolvedSubject(
            id=deterministic_uuid("subject", subject.external_type, subject.external_id),
            external_type=subject.external_type,
            external_id=subject.external_id,
            display_name=self._known.get(subject.external_id),
            status=(
                SubjectStatus.BLOCKED
                if subject.external_id in self._blocked
                else SubjectStatus.ACTIVE
            ),
        )


class FakeEntitlementGateway:
    """Plan lookup with an explicit default. Never makes a network call."""

    def __init__(
        self,
        plans: dict[str, str] | None = None,
        *,
        default_plan: str = "FREE",
        active_plans: frozenset[str] = frozenset({"PRO"}),
        clock: FixedClock | SystemClock | None = None,
    ) -> None:
        self._plans = plans or {}
        self._default_plan = default_plan
        self._active_plans = active_plans
        self._clock = clock or FixedClock()
        self.calls = 0

    async def snapshot(self, subject: ResolvedSubject) -> EntitlementSnapshot:
        self.calls += 1
        plan = self._plans.get(subject.external_id, self._default_plan)
        return EntitlementSnapshot(
            subject_id=subject.id,
            plan_code=plan,
            status=(
                EntitlementStatus.ACTIVE
                if plan in self._active_plans
                else EntitlementStatus.INACTIVE
            ),
            fetched_at=self._clock.now(),
            source="local_fake",
        )


class FakeTutorGateway:
    def __init__(self, tutor_name: str = "Anita Sharma") -> None:
        self._tutor_name = tutor_name
        self.calls = 0

    async def assigned_tutor(self, subject: ResolvedSubject) -> TutorProfile | None:
        self.calls += 1
        return TutorProfile(
            id=deterministic_uuid("tutor", self._tutor_name),
            display_name=self._tutor_name,
            persona=TutorPersona(version=1),
        )


class FakeOutboundGateway:
    """Records deliveries instead of sending. Phase 08 replaces with Lead Intake."""

    def __init__(self) -> None:
        self.delivered: list[tuple[UUID, tuple[OutboundAction, ...]]] = []

    async def deliver(self, subject: ResolvedSubject, actions: tuple[OutboundAction, ...]) -> None:
        self.delivered.append((subject.id, actions))


class InMemoryBlobStore:
    def __init__(self) -> None:
        self._objects: dict[str, tuple[bytes, str]] = {}

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        self._objects[key] = (data, content_type)
        return key

    async def get(self, key: str) -> bytes:
        return self._objects[key][0]

    async def signed_url(self, key: str, expires_in_seconds: int) -> str:
        return f"memory://{key}?expires_in={expires_in_seconds}"


class RecordingTaskQueue:
    """Records enqueues; runs nothing. Phase 01 has no async work."""

    def __init__(self) -> None:
        self.enqueued: list[tuple[str, int]] = []

    async def enqueue(self, job_id: str, *, delay_seconds: int = 0) -> None:
        self.enqueued.append((job_id, delay_seconds))


class ForbiddenLLMProvider:
    """A tripwire, not a stub.

    Phase 01 must make zero paid model calls. Any call raises loudly so a test can
    prove the cost gate held rather than silently returning canned text.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, model_alias: str, prompt: str, *, max_output_tokens: int) -> str:
        self.calls += 1
        raise AssertionError(
            f"Paid LLM call attempted in Phase 01 (alias={model_alias}). "
            "No provider call is permitted at this phase."
        )


class ForbiddenEmbeddingProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def embed(
        self, model_alias: str, texts: tuple[str, ...]
    ) -> tuple[tuple[float, ...], ...]:
        self.calls += 1
        raise AssertionError(f"Paid embedding call attempted in Phase 01 (alias={model_alias}).")


class NullMediaInspector:
    """Returns only what the reference already told us. Never downloads bytes."""

    async def describe(self, media: MediaRef) -> dict[str, str]:
        return {
            "provider": media.provider,
            "media_id": media.media_id,
            "mime_type_hint": media.mime_type_hint or "unknown",
        }
