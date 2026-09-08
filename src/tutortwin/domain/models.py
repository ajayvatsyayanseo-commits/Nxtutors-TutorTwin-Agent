"""Domain value objects. Persistence-free, vendor-free."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SubjectStatus(StrEnum):
    ACTIVE = "ACTIVE"
    BLOCKED = "BLOCKED"


class EntitlementStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    EXPIRED = "EXPIRED"


class ResolvedSubject(Frozen):
    id: UUID
    external_type: str
    external_id: str
    display_name: str | None = None
    status: SubjectStatus = SubjectStatus.ACTIVE


class EntitlementSnapshot(Frozen):
    """Point-in-time entitlement. `fetched_at` matters: this may be cached."""

    subject_id: UUID
    plan_code: str
    status: EntitlementStatus
    fetched_at: datetime
    source: str = "local_fake"
    ends_at: datetime | None = None

    @property
    def allows_paid_ai(self) -> bool:
        """The gate that keeps ineligible students at zero paid provider calls."""
        return self.status is EntitlementStatus.ACTIVE


class TutorPersona(Frozen):
    """Structured, versioned persona. Never impersonates the human tutor."""

    version: int = Field(ge=1)
    tone: str = "supportive"
    response_length: str = "medium"
    language: str = "en"
    step_by_step: bool = True
    hint_first: bool = True
    socratic: bool = False
    custom_instructions: str | None = None
    forbidden_behaviors: tuple[str, ...] = ()


class TutorProfile(Frozen):
    id: UUID
    display_name: str
    persona: TutorPersona

    @property
    def assistant_identity(self) -> str:
        """The only name TutorTwin may present itself under."""
        return f"TutorTwin - AI Assistant for {self.display_name}"
