"""Typed settings. One source of truth for configuration.

Secrets use pydantic SecretStr so they never render in logs, reprs or tracebacks.
Model identifiers are aliases resolved through `model_aliases`; business code must
never hardcode a vendor model ID.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "test", "staging", "production"]

# Capability -> model alias. Real vendor IDs arrive in Phase 02 via the model
# catalog table; Phase 01 only fixes the alias vocabulary.
MODEL_ALIASES: tuple[str, ...] = (
    "CHEAP_TEXT",
    "STANDARD_TUTOR",
    "ADVANCED_REASONING",
    "VISION",
    "TRANSCRIBE",
    "EMBEDDING",
    "VERIFIER_PRIMARY",
    "VERIFIER_SECONDARY",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TUTORTWIN_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    environment: Environment = "local"
    service_name: str = "tutortwin-api"

    # Two DSNs: pooled for the app (serverless, many short-lived containers),
    # direct for migrations (pgbouncer transaction pooling breaks DDL/advisory locks).
    database_url: SecretStr = SecretStr(
        "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/tutortwin"
    )
    database_migration_url: SecretStr | None = None

    database_postgres_schema: str = "public"
    """The Postgres schema TutorTwin owns.

    Set this when the database is **shared** with another product. Every
    TutorTwin table, and TutorTwin's own `alembic_version`, is created inside it;
    nothing outside it is read, written or migrated.

    It is applied as a per-connection `search_path` of `<schema>, public` rather
    than by stamping a schema onto every model: creation lands in the first entry
    (ours), while extension types installed in `public` - `vector`, above all -
    still resolve. A model-level schema would hard-code the name into the
    migrations and make the same code un-runnable against a database that does
    not use it.
    """

    # Small pool: each serverless container holds few connections.
    db_pool_size: int = Field(default=2, ge=1, le=20)
    db_max_overflow: int = Field(default=2, ge=0, le=20)
    db_pool_recycle_seconds: int = Field(default=280, ge=30)
    db_connect_timeout_seconds: int = Field(default=5, ge=1, le=30)
    db_statement_timeout_ms: int = Field(default=8000, ge=100)
    db_health_timeout_seconds: float = Field(default=2.0, gt=0)

    # Paid AI vendors. OpenAI and Anthropic only - no other paid vendor is
    # permitted. Absent keys mean that vendor is simply not wired.
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None

    # Security
    internal_api_key: SecretStr | None = None
    max_request_bytes: int = Field(default=256 * 1024, ge=1024)

    # First administrator, for the bootstrap command. The password is stored as
    # an Argon2id PHC string, never in the clear: a .env file is read by every
    # process on the box and ends up in shell history, backups and screen shares.
    # The hash is useless to anyone who reads it, which is the entire point of
    # hashing a password in the first place.
    admin_bootstrap_email: str | None = None
    admin_bootstrap_password_hash: SecretStr | None = None

    # Local harness only: external_id -> plan_code for the fake entitlement
    # gateway, so a developer can exercise the Pro path without a real gateway.
    # Phase 09 replaces the fake with the website source of truth.
    fake_pro_subjects: tuple[str, ...] = ()

    # --- media storage -------------------------------------------------------
    #
    # Filesystem locally and in tests, Cloudflare R2 in a deployed environment.
    # Which one is used is decided once, in `dependencies.py`, from whether the
    # R2 fields are set - never by an `if environment == ...` at a use site.
    media_root: str = "./var/media"

    tesseract_cmd: str | None = None
    """Absolute path to the tesseract binary. None means "find it on PATH", which
    is right on Linux and usually wrong on Windows, where the installer does not
    add itself to PATH."""

    r2_account_id: str | None = None
    r2_access_key_id: SecretStr | None = None
    r2_secret_access_key: SecretStr | None = None
    r2_bucket: str | None = None
    media_retention_days: int = Field(default=7, ge=1, le=365)

    # --- async work ----------------------------------------------------------
    #
    # Cloud Tasks pushes to an OIDC-authenticated endpoint on this same service.
    # Absent configuration means jobs are recorded and not dispatched, which is
    # what local development and the test suite want.
    tasks_project: str | None = None
    tasks_location: str | None = None
    tasks_queue: str | None = None
    tasks_target_url: str | None = None
    tasks_service_account: str | None = None

    # The audience an inbound OIDC token must carry. Cloud Tasks signs the token
    # for the target URL, so this is normally that URL. Left unset outside GCP,
    # where the shared secret is the equivalent check.
    oidc_audience: str | None = None
    oidc_service_account: str | None = None
    """When set, an inbound OIDC token must also be issued to this service
    account. Without it any Google-signed token for the audience is accepted,
    which is a much larger set of callers than intended."""

    # --- cost ceilings -------------------------------------------------------
    #
    # These are the platform's own limits, above whatever a plan allows. A plan
    # bounds one student; these bound the bill.
    system_daily_budget_micros: int | None = Field(default=50_000_000, ge=0)
    """$50/day across every student, by default. None disables the ceiling."""

    system_hourly_budget_micros: int | None = Field(default=10_000_000, ge=0)
    """Spend *velocity*. A daily ceiling alone lets a loop burn the whole day's
    budget in four minutes and only notices afterwards."""

    provider_daily_budget_micros: int | None = Field(default=30_000_000, ge=0)
    """Per vendor, so one provider's runaway cannot consume the other's headroom
    and leave the service with no working fallback."""

    provider_failure_circuit: int = Field(default=5, ge=1, le=100)
    """Consecutive failed calls to one vendor before we stop paying to retry it."""

    provider_circuit_window_minutes: int = Field(default=10, ge=1, le=1440)

    heavy_job_max_concurrency: int = Field(default=10, ge=1, le=200)
    """Media jobs allowed to run at once. Bounds simultaneous OCR memory, vision
    spend and Postgres connections at the same time."""

    # --- per-student media ceilings -----------------------------------------
    #
    # Counted from the rows the pipeline already writes, so nothing here is a
    # separate counter that can drift from reality.
    student_daily_pdf_pages: int | None = Field(default=100, ge=0)
    student_daily_ocr_pages: int | None = Field(default=60, ge=0)
    student_daily_voice_seconds: int | None = Field(default=900, ge=0)
    student_daily_mocks: int | None = Field(default=5, ge=0)
    student_monthly_budget_micros: int | None = Field(default=20_000_000, ge=0)

    # --- WhatsApp (Meta Cloud API) -------------------------------------------
    #
    # TutorTwin owns the Meta webhook directly. The keys are read WITHOUT the
    # TUTORTWIN_ prefix as well, because that is how Meta's own docs name them
    # and how they were already configured - `validation_alias` accepts both
    # rather than making somebody rename working credentials.
    whatsapp_access_token: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "TUTORTWIN_WHATSAPP_ACCESS_TOKEN", "WHATSAPP_ACCESS_TOKEN"
        ),
    )
    whatsapp_phone_number_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "TUTORTWIN_WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_PHONE_NUMBER_ID"
        ),
    )
    whatsapp_verify_token: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "TUTORTWIN_WHATSAPP_VERIFY_TOKEN", "WHATSAPP_VERIFY_TOKEN"
        ),
    )
    """Echoed back to Meta during webhook registration. Meta sends it in the
    clear on a GET, so it proves only that whoever registered the webhook knew
    it - the app secret below is what authenticates actual messages."""

    whatsapp_app_secret: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("TUTORTWIN_WHATSAPP_APP_SECRET", "WHATSAPP_APP_SECRET"),
    )
    """Signs every inbound payload. Without it the webhook is a public endpoint
    anyone can post student messages to."""

    whatsapp_graph_api_version: str = Field(
        default="v21.0",
        validation_alias=AliasChoices(
            "TUTORTWIN_WHATSAPP_GRAPH_API_VERSION", "WHATSAPP_GRAPH_API_VERSION"
        ),
    )
    whatsapp_send_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "TUTORTWIN_WHATSAPP_SEND_ENABLED", "WHATSAPP_SEND_ENABLED"
        ),
    )
    """A kill switch for outbound only. False keeps the webhook receiving and
    the agent thinking, and stops anything reaching a real phone - which is what
    you want the first time this points at a production number."""

    whatsapp_http_timeout_seconds: float = Field(
        default=10.0,
        ge=1.0,
        le=120.0,
        validation_alias=AliasChoices(
            "TUTORTWIN_WHATSAPP_HTTP_TIMEOUT_S", "WHATSAPP_HTTP_TIMEOUT_S"
        ),
    )

    # --- payments (Cashfree) & subscription ----------------------------------
    #
    # Unprefixed names accepted, same reasoning as WhatsApp: these are what
    # Cashfree's own dashboard calls them and what was already configured.
    cashfree_env: str = Field(
        default="sandbox",
        validation_alias=AliasChoices("TUTORTWIN_CASHFREE_ENV", "CASHFREE_ENV"),
    )
    """`production` or `sandbox`. It selects the API host, so a wrong value here
    means test cards against real money or real cards against a test ledger."""

    cashfree_app_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("TUTORTWIN_CASHFREE_APP_ID", "CASHFREE_APP_ID"),
    )
    cashfree_secret_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("TUTORTWIN_CASHFREE_SECRET_KEY", "CASHFREE_SECRET_KEY"),
    )
    """Also the webhook signing key. Every payment callback is verified with it,
    so without it the activation endpoint refuses everything - fail closed,
    because a forged callback grants a free subscription."""

    cashfree_api_version: str = Field(
        default="2025-01-01",
        validation_alias=AliasChoices("TUTORTWIN_CASHFREE_API_VERSION", "CASHFREE_API_VERSION"),
    )

    subscription_price_paise: int = Field(default=10_000, ge=100)
    """In paise, as an integer. 10000 = Rs 100. Rupees as a float eventually
    charges somebody 99.99999 and that is not a conversation worth having."""

    subscription_plan_code: str = "PRO"
    subscription_days: int = Field(default=30, ge=1, le=3660)

    public_site_url: str = "https://nxtutortwin.nxtutors.com"
    """Where an unsubscribed student is sent to subscribe, and the base for the
    payment return URL. Must be the address a browser can actually reach."""

    deployment_target: str = "serverless"
    """`serverless` (Cloud Run) or `server` (a VPS, CloudPanel, a plain EC2 box).

    This changes what a production deployment is REQUIRED to have, because the
    two platforms fail in opposite ways:

    On Cloud Run the container filesystem is ephemeral and CPU is throttled
    between requests, so object storage and an external queue are not optional -
    without them media is written to a disk that disappears and background work
    silently never runs.

    On a server neither is true. The disk persists and the process is always
    running, so demanding Cloudflare R2 and Google Cloud Tasks would force two
    dependencies that buy nothing. Requiring them anyway is not caution, it is a
    guard asking the wrong question for the platform it is on.
    """

    @field_validator("deployment_target")
    @classmethod
    def _known_target(cls, v: str) -> str:
        cleaned = v.strip().lower()
        if cleaned not in {"serverless", "server"}:
            raise ValueError("deployment_target must be 'serverless' or 'server'")
        return cleaned

    @property
    def is_serverless(self) -> bool:
        return self.deployment_target == "serverless"

    trusted_proxy_hops: int = Field(default=0, ge=0, le=8)
    """How many reverse proxies sit in front of this service.

    0 means `x-forwarded-for` is IGNORED entirely and the socket peer is used.

    This exists because the header is client-controlled. Taking its leftmost
    entry - the obvious reading of "the original client" - lets anyone send a
    different fake IP on every request, which turns a per-IP login throttle into
    no throttle at all and hands an attacker unlimited password guesses.

    Each proxy APPENDS the address it saw, so the trustworthy entry is the Nth
    from the RIGHT, where N is the number of proxies we actually run behind.
    Cloud Run behind no load balancer is 1. Getting this too high is as bad as
    trusting the header: it walks left into forged territory again.
    """

    cors_allow_origins: tuple[str, ...] = ()
    """Browser origins allowed to call this API.

    Empty by default, and it must stay a explicit list rather than a wildcard:
    the public site calls `/public/*` from a browser, but the same service also
    serves the admin control plane. `allow_credentials` is deliberately NOT
    enabled - the public endpoints use no cookies, and the admin panel reaches
    this API from its own server, never from the browser, so nothing here needs
    to carry a session.
    """

    public_api_url: str = "https://nxtutortwin.nxtutors.com"
    """Where *Cashfree and Meta* reach this service. Separate from the site URL
    because they are frequently different hosts, and because a callback URL
    pointed at the marketing site is a payment that never activates."""
    """Where an unsubscribed student is sent to subscribe. Also the base for the
    payment return URL, so it must be the address a browser can actually reach."""

    # --- runtime -------------------------------------------------------------
    request_timeout_seconds: int = Field(default=120, ge=1, le=3600)
    shutdown_grace_seconds: int = Field(default=20, ge=1, le=600)
    """SIGTERM to hard stop. Cloud Run allows 10s by default and up to 600s; the
    value here must be the smaller of the two or in-flight requests are killed."""

    # Observability
    log_level: str = "INFO"
    log_message_content: bool = False
    """When False (default) student message text is never written to logs."""

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @field_validator(
        "anthropic_api_key",
        "openai_api_key",
        "internal_api_key",
        "admin_bootstrap_password_hash",
        "r2_access_key_id",
        "r2_secret_access_key",
        "database_migration_url",
        "whatsapp_access_token",
        "whatsapp_verify_token",
        "whatsapp_app_secret",
        "cashfree_secret_key",
        mode="before",
    )
    @classmethod
    def _blank_secret_is_unset(cls, v: object) -> object:
        """An empty variable means "not configured", not "configured as empty".

        `TUTORTWIN_OPENAI_API_KEY=` with nothing after it is how a key is left to
        be filled in later, and it is what a deploy pipeline writes for a secret
        it could not resolve. Read literally it produces a *present* credential
        of zero length, so the vendor client is constructed and raises at
        startup - the service refuses to boot because of a variable nobody set.
        """
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator(
        "r2_account_id",
        "r2_bucket",
        "tasks_project",
        "tasks_location",
        "tasks_queue",
        "tasks_target_url",
        "tasks_service_account",
        "oidc_audience",
        "oidc_service_account",
        "admin_bootstrap_email",
        "tesseract_cmd",
        "whatsapp_phone_number_id",
        "cashfree_app_id",
        mode="before",
    )
    @classmethod
    def _blank_is_none(cls, v: object) -> object:
        """Same rule for the plain-string optionals.

        It matters most for the R2 and Cloud Tasks groups: `r2_configured` is an
        all-or-nothing check, and a blank string counted as present would let a
        deployment start believing it had object storage.
        """
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("database_url", "database_migration_url")
    @classmethod
    def _psycopg3_only(cls, v: SecretStr | None) -> SecretStr | None:
        """Reject a DSN this service cannot actually open.

        The project runs psycopg **3**. A `postgresql+psycopg2://` DSN - the
        shape most tutorials and most other services in the estate use - fails
        eight frames deep inside SQLAlchemy with
        `ModuleNotFoundError: No module named 'psycopg2'`, which reads like a
        broken install rather than a wrong URL and sends the reader to `pip`.

        Caught here instead, naming the fix. A bare `postgresql://` is fine:
        SQLAlchemy resolves the driver, and the project installs exactly one.
        """
        if v is None:
            return v
        dsn = v.get_secret_value()
        if "+psycopg2" in dsn:
            raise ValueError(
                "DSN uses the psycopg2 driver; this service runs psycopg 3. "
                "Change 'postgresql+psycopg2://' to 'postgresql+psycopg://'."
            )
        return v

    @field_validator("database_postgres_schema")
    @classmethod
    def _safe_schema(cls, v: str) -> str:
        """A schema name reaches SQL as an identifier, not a bound parameter.

        It comes from configuration rather than from a user, but configuration is
        edited by hand under time pressure, and `search_path` is not a place to
        discover a typo. Anything but a plain identifier is refused.
        """
        cleaned = v.strip()
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", cleaned):
            raise ValueError(
                "database_postgres_schema must be a lowercase identifier "
                "(letters, digits, underscore; not starting with a digit)."
            )
        return cleaned

    @property
    def search_path(self) -> str:
        """`<ours>,public` - ours first so CREATE lands there, public second so
        extension types like `vector` still resolve.

        **No space after the comma.** This value is passed to libpq through the
        connection `options` string, where a space separates arguments: with one,
        the server receives `search_path=tutor_twin,` and refuses the connection
        with "List syntax is invalid". A `SET search_path` statement tolerates the
        space, which is why migrations succeeded while the app could not connect.
        """
        if self.database_postgres_schema == "public":
            return "public"
        return f"{self.database_postgres_schema},public"

    @property
    def migration_dsn(self) -> str:
        """Direct DSN for Alembic; falls back to the app DSN locally."""
        target = self.database_migration_url or self.database_url
        return target.get_secret_value()

    @property
    def app_dsn(self) -> str:
        return self.database_url.get_secret_value()

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def is_deployed(self) -> bool:
        """Staging and production. The two environments that face real money."""
        return self.environment in ("staging", "production")

    @property
    def whatsapp_configured(self) -> bool:
        """All three are needed to hold a conversation.

        The token sends, the phone id says which number sends, and the app
        secret authenticates what arrives. Two out of three is a half-wired
        integration that fails at the least convenient moment, so it counts as
        not configured.
        """
        return all(
            (
                self.whatsapp_access_token,
                self.whatsapp_phone_number_id,
                self.whatsapp_app_secret,
            )
        )

    @property
    def cashfree_configured(self) -> bool:
        """Both halves, or payments are not live.

        The app id identifies the merchant and the secret both authenticates our
        calls and verifies their callbacks. One without the other is a payment
        page that either cannot create orders or cannot trust the result.
        """
        return bool(self.cashfree_app_id and self.cashfree_secret_key)

    @property
    def cashfree_base_url(self) -> str:
        if self.cashfree_env.strip().lower() == "production":
            return "https://api.cashfree.com/pg"
        return "https://sandbox.cashfree.com/pg"

    @property
    def r2_configured(self) -> bool:
        return all(
            (
                self.r2_account_id,
                self.r2_access_key_id,
                self.r2_secret_access_key,
                self.r2_bucket,
            )
        )

    @property
    def tasks_configured(self) -> bool:
        return all(
            (
                self.tasks_project,
                self.tasks_location,
                self.tasks_queue,
                self.tasks_target_url,
                self.tasks_service_account,
            )
        )

    def require_deployable(self) -> None:
        """Refuse to start a deployed environment that is quietly mis-wired.

        Every item here fails *silently* otherwise: media would be written to a
        container filesystem that disappears at the next request, jobs would be
        recorded and never dispatched, and the internal endpoint would be open.
        A service that boots and loses data is worse than one that will not boot.
        """
        if not self.is_deployed:
            return

        missing = []
        if not self.internal_api_key:
            missing.append("TUTORTWIN_INTERNAL_API_KEY")
        if not self.database_migration_url:
            missing.append("TUTORTWIN_DATABASE_MIGRATION_URL")

        # Only a serverless deployment needs these, and it needs them absolutely.
        # On a server the disk persists and the process stays alive, so the
        # filesystem blobstore and in-process dispatch are correct rather than a
        # compromise - see `deployment_target`.
        if self.is_serverless:
            if not self.r2_configured:
                missing.append("TUTORTWIN_R2_* (media would be written to ephemeral disk)")
            if not self.tasks_configured:
                missing.append("TUTORTWIN_TASKS_* (jobs would never be dispatched)")
        elif not self.public_api_url:
            # In-process dispatch calls this service back over loopback, so it
            # has to know its own address.
            missing.append("TUTORTWIN_PUBLIC_API_URL (in-process job dispatch needs it)")

        if missing:
            raise RuntimeError(f"environment={self.environment} requires: {', '.join(missing)}")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
