"""Admin roles, permissions and the high-risk action register.

**Permissions are data, not code.** The role matrix below is the single place a
role's authority is defined, so "what can SUPPORT do?" is answerable by reading
one table rather than grepping for `if role ==` across the API. A route declares
the permission it needs; the dependency compares against this matrix. Hiding a
button is a courtesy to the user, never a control.

**High-risk actions are enumerated, not judged case by case.** Each one costs
money, changes what students can do, or destroys data, so each requires a stated
reason and each writes an audit event. Keeping the list here means adding a
dangerous endpoint without registering it is a visible omission rather than an
invisible one.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class AdminRole(StrEnum):
    SUPER_ADMIN = "SUPER_ADMIN"
    ADMIN = "ADMIN"
    ACADEMIC_ADMIN = "ACADEMIC_ADMIN"
    SUPPORT = "SUPPORT"
    TUTOR_VIEWER = "TUTOR_VIEWER"
    USAGE_VIEWER = "USAGE_VIEWER"


class Permission(StrEnum):
    """One verb on one resource. Read and write are always separate."""

    DASHBOARD_READ = "dashboard:read"

    STUDENT_READ = "student:read"
    STUDENT_WRITE = "student:write"

    TUTOR_READ = "tutor:read"
    TUTOR_WRITE = "tutor:write"
    PERSONA_ACTIVATE = "persona:activate"

    PLAN_READ = "plan:read"
    PLAN_WRITE = "plan:write"

    MODEL_READ = "model:read"
    MODEL_WRITE = "model:write"

    PROMPT_READ = "prompt:read"
    PROMPT_WRITE = "prompt:write"

    CONVERSATION_READ = "conversation:read"

    DOCUMENT_READ = "document:read"
    DOCUMENT_WRITE = "document:write"

    LEARNING_READ = "learning:read"

    JOB_READ = "job:read"
    JOB_WRITE = "job:write"

    COST_READ = "cost:read"

    FLAG_READ = "flag:read"
    FLAG_WRITE = "flag:write"

    AUDIT_READ = "audit:read"

    ADMIN_USER_READ = "admin_user:read"
    ADMIN_USER_WRITE = "admin_user:write"


_ALL: frozenset[Permission] = frozenset(Permission)

# Everything an operator needs, minus the ability to create administrators or
# change their roles. That separation is the whole point of SUPER_ADMIN: an
# account that can grant itself more authority is not a lesser role.
_ADMIN: frozenset[Permission] = _ALL - {
    Permission.ADMIN_USER_WRITE,
}

_ACADEMIC: frozenset[Permission] = frozenset(
    {
        Permission.DASHBOARD_READ,
        Permission.STUDENT_READ,
        Permission.TUTOR_READ,
        Permission.TUTOR_WRITE,
        Permission.PERSONA_ACTIVATE,
        Permission.PROMPT_READ,
        Permission.PROMPT_WRITE,
        Permission.CONVERSATION_READ,
        Permission.DOCUMENT_READ,
        Permission.DOCUMENT_WRITE,
        Permission.LEARNING_READ,
        Permission.PLAN_READ,
        Permission.MODEL_READ,
        Permission.FLAG_READ,
        Permission.JOB_READ,
        Permission.AUDIT_READ,
    }
)

# Support answers "why did this student not get an answer?". That needs reads
# across the request path and the ability to retry a failed job - and nothing
# that changes cost, entitlement or routing.
_SUPPORT: frozenset[Permission] = frozenset(
    {
        Permission.DASHBOARD_READ,
        Permission.STUDENT_READ,
        Permission.CONVERSATION_READ,
        Permission.DOCUMENT_READ,
        Permission.LEARNING_READ,
        Permission.TUTOR_READ,
        Permission.JOB_READ,
        Permission.JOB_WRITE,
        Permission.PLAN_READ,
        Permission.FLAG_READ,
    }
)

_TUTOR_VIEWER: frozenset[Permission] = frozenset(
    {
        Permission.TUTOR_READ,
        Permission.STUDENT_READ,
        Permission.LEARNING_READ,
        Permission.CONVERSATION_READ,
    }
)

_USAGE_VIEWER: frozenset[Permission] = frozenset(
    {
        Permission.DASHBOARD_READ,
        Permission.COST_READ,
        Permission.MODEL_READ,
        Permission.PLAN_READ,
    }
)

ROLE_PERMISSIONS: dict[AdminRole, frozenset[Permission]] = {
    AdminRole.SUPER_ADMIN: _ALL,
    AdminRole.ADMIN: _ADMIN,
    AdminRole.ACADEMIC_ADMIN: _ACADEMIC,
    AdminRole.SUPPORT: _SUPPORT,
    AdminRole.TUTOR_VIEWER: _TUTOR_VIEWER,
    AdminRole.USAGE_VIEWER: _USAGE_VIEWER,
}


def permissions_for(role: AdminRole) -> frozenset[Permission]:
    """Unknown roles get nothing, rather than defaulting to something."""
    return ROLE_PERMISSIONS.get(role, frozenset())


def role_allows(role: AdminRole, permission: Permission) -> bool:
    return permission in permissions_for(role)


class AdminActor(BaseModel):
    """Who is making the request. Built from the session, never from the body."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    admin_id: str
    email: str
    role: AdminRole
    session_id: str

    @property
    def permissions(self) -> frozenset[Permission]:
        return permissions_for(self.role)

    def can(self, permission: Permission) -> bool:
        return permission in self.permissions


class HighRiskAction(StrEnum):
    """Actions that require a typed confirmation and a stated reason."""

    ENTITLEMENT_OVERRIDE = "ENTITLEMENT_OVERRIDE"
    QUOTA_RESET = "QUOTA_RESET"
    MODEL_ROUTE_CHANGE = "MODEL_ROUTE_CHANGE"
    FEATURE_KILL_SWITCH = "FEATURE_KILL_SWITCH"
    PERSONA_ACTIVATION = "PERSONA_ACTIVATION"
    STUDENT_DATA_DELETE = "STUDENT_DATA_DELETE"
    DOCUMENT_REPROCESS = "DOCUMENT_REPROCESS"
    ADMIN_ROLE_CHANGE = "ADMIN_ROLE_CHANGE"
    PLAN_POLICY_CHANGE = "PLAN_POLICY_CHANGE"
    PROMPT_ACTIVATION = "PROMPT_ACTIVATION"


MIN_REASON_CHARS = 8
"""Long enough to exclude "ok" and "asdf". A reason nobody can read later is the
same as no reason, and the audit trail is the only record of intent."""

MAX_REASON_CHARS = 500


class HighRiskRequest(BaseModel):
    """Body mixin for every dangerous mutation.

    `confirm` must be sent explicitly. A destructive endpoint that acts on a bare
    POST is one mis-click - or one replayed request - away from data loss.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str = Field(min_length=MIN_REASON_CHARS, max_length=MAX_REASON_CHARS)
    confirm: bool = Field(description="Must be true. The UI types the target name to set it.")


# Feature flags the control plane owns. Enumerated so an operator cannot invent a
# key that nothing reads, which would look like a kill switch and do nothing.
KILL_SWITCHES: tuple[tuple[str, str], ...] = (
    ("pdf_processing", "PDF extraction and OCR"),
    ("image_processing", "Image OCR and vision escalation"),
    ("voice_processing", "Voice note transcription"),
    ("mock_tests", "Mock test generation"),
    ("verifier", "Second-model verification"),
    ("provider_openai", "OpenAI provider"),
    ("provider_anthropic", "Anthropic provider"),
    ("advanced_model", "Advanced reasoning tier"),
    ("rag_retrieval", "Retrieval-augmented answers"),
    ("code_sandbox", "Student code execution (Phase 07)"),
)

KILL_SWITCH_KEYS: frozenset[str] = frozenset(key for key, _ in KILL_SWITCHES)


__all__ = [
    "KILL_SWITCHES",
    "KILL_SWITCH_KEYS",
    "MAX_REASON_CHARS",
    "MIN_REASON_CHARS",
    "ROLE_PERMISSIONS",
    "AdminActor",
    "AdminRole",
    "HighRiskAction",
    "HighRiskRequest",
    "Permission",
    "permissions_for",
    "role_allows",
]
