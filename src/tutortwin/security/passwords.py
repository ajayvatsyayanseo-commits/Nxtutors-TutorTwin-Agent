"""Argon2id password hashing and the password policy.

**Argon2id, at the library's own defaults.** Measured here: m=64 MiB, t=3, p=4,
about 76 ms per hash on this machine — which is the RFC 9106 low-memory profile
and OWASP's current recommendation. The parameters travel inside the PHC string,
so raising them later re-hashes each user on their next login instead of locking
everyone out.

**Verification is constant-work even for an account that does not exist.** A
login that skips hashing when the email is unknown answers in 1 ms instead of
76 ms, and that difference enumerates the administrator list. `dummy_verify()`
exists so the unknown-user path costs the same as the known-user path.
"""

from __future__ import annotations

import re
import secrets
import string

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from tutortwin.observability.logging import get_logger

logger = get_logger(__name__)

_hasher = PasswordHasher()

# A fixed hash of a value nobody knows, used only to burn the same CPU time on
# the unknown-user path. It is a constant, never a credential.
_DUMMY_HASH = _hasher.hash("tutortwin-timing-equaliser")

MIN_PASSWORD_CHARS = 12
"""Long enough that the 76 ms hash cost makes offline guessing impractical.
Length beats composition rules, so there is no "must contain a symbol"."""

MAX_PASSWORD_CHARS = 256
"""Bounded because Argon2 hashes whatever it is given, and a 10 MB "password"
would be a free denial of service."""

_WHITESPACE_ONLY = re.compile(r"^\s*$")


class WeakPassword(ValueError):
    """Rejected before hashing. The message is safe to show an administrator."""


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_CHARS:
        raise WeakPassword(f"Password must be at least {MIN_PASSWORD_CHARS} characters.")
    if len(password) > MAX_PASSWORD_CHARS:
        raise WeakPassword(f"Password must be at most {MAX_PASSWORD_CHARS} characters.")
    if _WHITESPACE_ONLY.match(password):
        raise WeakPassword("Password must not be blank.")


def hash_password(password: str) -> str:
    validate_password(password)
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    """True when the password matches. Never raises for a wrong password."""
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def dummy_verify(password: str) -> None:
    """Burn one hash verification against a constant.

    Called when the email is unknown, disabled or locked, so the response time
    of a failed login carries no information about which of those it was.
    """
    try:
        _hasher.verify(_DUMMY_HASH, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return


def needs_rehash(stored_hash: str) -> bool:
    """True when the stored hash used weaker parameters than we now use."""
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


_ALPHABET = string.ascii_letters + string.digits
GENERATED_PASSWORD_CHARS = 24


def generate_password(length: int = GENERATED_PASSWORD_CHARS) -> str:
    """A CSPRNG password for the bootstrap procedure.

    Alphanumeric only, because this string is copied out of a terminal and typed
    into a browser once; a shell-quoting accident that mangles it would be worse
    than the entropy it saves. 24 alphanumerics is about 143 bits.
    """
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


__all__ = [
    "MAX_PASSWORD_CHARS",
    "MIN_PASSWORD_CHARS",
    "WeakPassword",
    "dummy_verify",
    "generate_password",
    "hash_password",
    "needs_rehash",
    "validate_password",
    "verify_password",
]
