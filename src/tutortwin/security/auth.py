"""Internal service auth and the admin auth seam.

Two callers are internal, and they authenticate differently because they *are*
different:

- **Cloud Tasks** presents a Google-signed OIDC token, minted for one audience
  and one service account. Nothing has to be shared, stored or rotated, and a
  leaked token expires on its own.
- **Lead Intake** (Phase 08) presents the shared secret, because it is not on
  Google's identity plane.

`InternalAuth` accepts whichever is configured, and prefers OIDC when it is:
downgrading to a static string when a signed token was available would make the
strongest credential optional.

Admin auth is implemented in Phase 06; the Protocol below is the seam the rest
of the codebase talks to.
"""

from __future__ import annotations

import secrets
from typing import Any, Protocol

from tutortwin.config import Settings
from tutortwin.domain.errors import ErrorCode, TutorTwinError
from tutortwin.observability.logging import get_logger

logger = get_logger(__name__)


class InternalAuthenticator(Protocol):
    def verify(self, presented_key: str | None) -> None:
        """Raise TutorTwinError(UNAUTHENTICATED) when the caller is not trusted."""
        ...


class SharedSecretAuthenticator:
    """Constant-time shared-secret check.

    When no key is configured the service is open, which is only tolerable
    outside production - `require_configured_in_production` enforces that.
    """

    def __init__(self, settings: Settings) -> None:
        key = settings.internal_api_key
        self._expected = key.get_secret_value() if key else None
        self._enforce = settings.is_production

    def require_configured_in_production(self) -> None:
        if self._enforce and not self._expected:
            raise RuntimeError(
                "TUTORTWIN_INTERNAL_API_KEY must be set when environment=production."
            )

    @property
    def configured(self) -> bool:
        return self._expected is not None

    def verify(self, presented_key: str | None) -> None:
        if self._expected is None:
            return
        if presented_key is None or not secrets.compare_digest(presented_key, self._expected):
            raise TutorTwinError(ErrorCode.UNAUTHENTICATED, "Invalid or missing credentials.")


class OidcVerifier:
    """Verifies a Google-signed OIDC token from Cloud Tasks.

    `google-auth` is imported lazily: a deployment outside GCP never installs it,
    and the test suite does not depend on it.

    Both checks matter. The **audience** stops a token minted for some other
    service being replayed here; the **service account** stops any Google-signed
    token for this URL - which is a far larger set of issuers than intended -
    from counting as our own queue.
    """

    def __init__(self, audience: str, service_account: str | None) -> None:
        self._audience = audience
        self._service_account = service_account

    def verify(self, bearer: str | None) -> None:
        if not bearer:
            raise TutorTwinError(ErrorCode.UNAUTHENTICATED, "Invalid or missing credentials.")
        token = bearer.removeprefix("Bearer ").removeprefix("bearer ").strip()

        try:
            from google.auth.transport import requests as google_requests
            from google.oauth2 import id_token

            claims: dict[str, Any] = id_token.verify_oauth2_token(
                token, google_requests.Request(), self._audience
            )
        except ImportError as exc:  # pragma: no cover - deployment-only path
            raise RuntimeError(
                "TUTORTWIN_OIDC_AUDIENCE is set but google-auth is not installed. "
                "Install the 'gcp' extra."
            ) from exc
        except Exception as exc:
            # The reason is logged, never returned: telling a caller *why* their
            # token was rejected is how a token gets tuned until it is accepted.
            logger.warning("oidc_verification_failed", error_type=type(exc).__name__)
            raise TutorTwinError(
                ErrorCode.UNAUTHENTICATED, "Invalid or missing credentials."
            ) from exc

        email = claims.get("email")
        if self._service_account and email != self._service_account:
            logger.warning("oidc_wrong_service_account")
            raise TutorTwinError(ErrorCode.UNAUTHENTICATED, "Invalid or missing credentials.")


class InternalAuth:
    """The check every internal endpoint runs.

    OIDC when configured, the shared secret otherwise. `require_configured` is
    what stops a deployed environment coming up with neither, which would leave
    an unauthenticated way to spend money on the public internet.
    """

    def __init__(self, settings: Settings) -> None:
        self._shared = SharedSecretAuthenticator(settings)
        self._oidc = (
            OidcVerifier(settings.oidc_audience, settings.oidc_service_account)
            if settings.oidc_audience
            else None
        )
        self._enforce = settings.is_deployed

    @property
    def mode(self) -> str:
        return "oidc" if self._oidc else "shared_secret"

    def require_configured(self) -> None:
        """Staging counts. A staging deployment reachable without a credential
        is a public endpoint that spends real provider money, whatever it is
        called."""
        if not self._enforce:
            return
        # The shared secret is required regardless: it is what authenticates
        # `/v1/events`, and OIDC does not cover that caller.
        if not self._shared.configured:
            raise RuntimeError(
                "TUTORTWIN_INTERNAL_API_KEY must be set in a deployed environment. "
                "Without it /v1/events accepts anonymous requests."
            )

    def verify(self, *, authorization: str | None, internal_key: str | None) -> None:
        """For the job endpoint: OIDC when configured, shared secret otherwise."""
        if self._oidc is not None:
            self._oidc.verify(authorization)
            return
        self._shared.verify(internal_key)

    def verify_shared_secret(self, internal_key: str | None) -> None:
        """For `/v1/events`, whose caller is Lead Intake.

        Lead Intake is not a Google service account, so OIDC is not available to
        it. Keeping this a separate method means enabling OIDC for the queue can
        never silently stop authenticating the event ingress.
        """
        self._shared.verify(internal_key)


class AdminAuthenticator(Protocol):
    """Implemented in Phase 06 (admin control plane)."""

    async def authenticate(self, token: str) -> str:
        """Return an admin actor id, or raise UNAUTHENTICATED."""
        ...
