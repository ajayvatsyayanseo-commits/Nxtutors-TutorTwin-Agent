"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from tutortwin.api.dependencies import build_container, get_container, set_container
from tutortwin.api.middleware import BodySizeLimitMiddleware, RequestContextMiddleware
from tutortwin.api.routes import events, health, jobs, public, whatsapp
from tutortwin.api.routes.admin import router as admin_router
from tutortwin.config import Settings, get_settings
from tutortwin.db.engine import dispose_engine, init_engine
from tutortwin.domain.errors import ErrorCode, TutorTwinError
from tutortwin.observability.logging import configure_logging, get_logger
from tutortwin.runtime import configure_event_loop_policy

logger = get_logger(__name__)

configure_event_loop_policy()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(level=settings.log_level, log_message_content=settings.log_message_content)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Refuse a mis-wired deployment before opening a port. Every item this
        # checks fails silently otherwise - media to a disk that vanishes, jobs
        # that are never dispatched, an open internal endpoint.
        settings.require_deployable()
        init_engine(settings)
        container = build_container(settings)
        container.internal_auth.require_configured()
        set_container(app, container)

        # On a shared database, refuse to serve if any table we query would
        # resolve to somebody else's schema. Reading another product's rows is
        # silent - no error, just wrong data - so this is checked once at boot
        # rather than discovered in an incident.
        if settings.database_postgres_schema != "public":
            from tutortwin.db.engine import get_session_factory
            from tutortwin.db.migration_guard import assert_no_cross_schema_fallthrough

            async with get_session_factory()() as probe:
                offenders = await assert_no_cross_schema_fallthrough(
                    probe, settings.database_postgres_schema
                )
            if offenders:
                raise RuntimeError(
                    "Tables resolve outside the configured schema "
                    f"'{settings.database_postgres_schema}': {offenders}. "
                    "Run `alembic upgrade head` against this database."
                )
            logger.info(
                "schema_isolation_verified",
                schema=settings.database_postgres_schema,
                search_path=settings.search_path,
            )

        logger.info(
            "service_started",
            environment=settings.environment,
            internal_auth=container.internal_auth.mode,
        )
        try:
            yield
        finally:
            await dispose_engine()
            logger.info("service_stopped")

    app = FastAPI(
        title="TutorTwin API",
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS, narrowly. The public site is a browser origin calling /public/*;
    # everything else here is server-to-server. Origins are listed explicitly
    # and credentials stay off, so a permissive entry cannot become a way to
    # ride an admin session from another site.
    if settings.cors_allow_origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_allow_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type"],
            max_age=600,
        )
        logger.info("cors_enabled", origins=list(settings.cors_allow_origins))

    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)
    app.add_middleware(RequestContextMiddleware)

    app.include_router(health.router)
    app.include_router(events.router, prefix="/v1")
    # The control plane. Its own session auth, RBAC and CSRF - the shared
    # internal secret is for service-to-service calls and grants no admin rights.
    app.include_router(admin_router)
    # Internal only: Cloud Tasks calls this with an OIDC token in production.
    app.include_router(jobs.router)
    # No /v1 prefix and no internal-key header: Meta owns this URL's shape
    # and authenticates by signature.
    app.include_router(whatsapp.router)
    # Signup, payment and the Cashfree callback. Public by necessity;
    # each endpoint documents what stops it being abused.
    app.include_router(public.router)

    @app.exception_handler(TutorTwinError)
    async def _domain_error(_request: Request, exc: TutorTwinError) -> JSONResponse:
        logger.warning("domain_error", code=str(exc.code))
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": str(exc.code), "message": exc.message}},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # Field locations are returned; submitted values are not, since they may
        # contain student content.
        fields = [".".join(str(p) for p in err["loc"]) for err in exc.errors()]
        logger.info("validation_failed", fields=fields)
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": str(ErrorCode.VALIDATION_FAILED),
                    "message": "Request failed validation.",
                    "fields": fields,
                }
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled_error", error_type=type(exc).__name__)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": str(ErrorCode.INTERNAL_ERROR),
                    "message": "An internal error occurred.",
                }
            },
        )

    return app


__all__ = ["create_app", "get_container"]
