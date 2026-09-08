"""Structured error model. Every failure leaving the API is one of these."""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    VALIDATION_FAILED = "VALIDATION_FAILED"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    IDENTITY_UNRESOLVED = "IDENTITY_UNRESOLVED"
    ENTITLEMENT_INACTIVE = "ENTITLEMENT_INACTIVE"
    RATE_LIMITED = "RATE_LIMITED"
    CONFLICT = "CONFLICT"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


_STATUS: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_FAILED: 422,
    ErrorCode.PAYLOAD_TOO_LARGE: 413,
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.IDENTITY_UNRESOLVED: 404,
    ErrorCode.ENTITLEMENT_INACTIVE: 403,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.CONFLICT: 409,
    ErrorCode.DEPENDENCY_UNAVAILABLE: 503,
    ErrorCode.INTERNAL_ERROR: 500,
}


class TutorTwinError(Exception):
    """Base for controlled, client-safe failures.

    `message` is safe to return to a caller: it must never embed student content,
    secrets or driver internals.
    """

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    @property
    def http_status(self) -> int:
        return _STATUS[self.code]


class EntitlementError(TutorTwinError):
    def __init__(self, message: str = "Active entitlement required.") -> None:
        super().__init__(ErrorCode.ENTITLEMENT_INACTIVE, message)


class IdentityError(TutorTwinError):
    def __init__(self, message: str = "Subject could not be resolved.") -> None:
        super().__init__(ErrorCode.IDENTITY_UNRESOLVED, message)


class OwnershipError(TutorTwinError):
    """Raised when a caller requests an object owned by another subject."""

    def __init__(self, message: str = "Not found.") -> None:
        # Deliberately 404-shaped: existence of another subject's object is not leaked.
        super().__init__(ErrorCode.NOT_FOUND, message)


class DependencyError(TutorTwinError):
    def __init__(self, message: str = "A required dependency is unavailable.") -> None:
        super().__init__(ErrorCode.DEPENDENCY_UNAVAILABLE, message)
