"""Composition root.

Every adapter is chosen here and nowhere else, so Phases 08/09 swap real NX
gateways in by editing this one file.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from fastapi import FastAPI, Request

from tutortwin.config import Settings
from tutortwin.domain.models import EntitlementSnapshot, ResolvedSubject
from tutortwin.domain.ports import EntitlementGateway, IdentityGateway, OutboundGateway
from tutortwin.integrations.cashfree import CashfreeClient
from tutortwin.integrations.whatsapp.client import (
    RoutingMediaSource,
    WhatsAppClient,
    WhatsAppMediaSource,
    WhatsAppOutboundGateway,
)
from tutortwin.media.adapters import (
    CloudTasksQueue,
    InProcessTaskQueue,
    LocalFileMediaSource,
    RecordingTaskQueue,
    TaskQueue,
)
from tutortwin.media.audio import OpenAITranscriptionProvider, TranscriptionProvider
from tutortwin.media.blobstore import BlobStore, FilesystemBlobStore, R2BlobStore
from tutortwin.media.ocr import TesseractOCRProvider
from tutortwin.media.pipeline import MediaPipeline
from tutortwin.observability.logging import get_logger
from tutortwin.orchestration.entry_service import EntryDependencies, TutorTwinEntryService
from tutortwin.policies.budget_policy import SystemLimits
from tutortwin.providers.fakes import (
    FakeEntitlementGateway,
    FakeIdentityGateway,
    FakeOutboundGateway,
    SystemClock,
)
from tutortwin.providers.gateway import ModelGateway
from tutortwin.providers.registry import build_gateway_factory
from tutortwin.security.auth import InternalAuth
from tutortwin.services.entitlements import DatabaseEntitlementGateway
from tutortwin.services.tutors import DatabaseTutorGateway

logger = get_logger(__name__)


@dataclass(slots=True)
class Container:
    settings: Settings
    entry_service: TutorTwinEntryService
    internal_auth: InternalAuth
    outbound: OutboundGateway
    identity: IdentityGateway
    entitlement: EntitlementGateway
    """Exposed so the media job can re-check entitlement before spending.

    The job runs minutes after intake, in a different request; a subscription
    can lapse in between, and a job that trusted intake's decision would spend
    on a student who is no longer paying."""
    media_pipeline: MediaPipeline | None = None
    gateway_factory: Callable[[], ModelGateway | None] | None = None
    whatsapp: WhatsAppClient | None = None
    """Present only when Meta credentials are configured. Held so shutdown
    can close its connection pool."""

    cashfree: CashfreeClient | None = None
    """Present only when payment credentials are configured."""

    task_queue: TaskQueue | None = None
    """Held so startup can re-dispatch jobs a previous process left behind.
    The pipeline owns the same object; this is the handle, not a second queue."""

    def build_gateway(self) -> ModelGateway | None:
        """A fresh gateway per job. None when no vendor is configured."""
        return self.gateway_factory() if self.gateway_factory is not None else None


def build_container(settings: Settings) -> Container:
    from tutortwin.db.engine import get_session_factory

    # Returns None unless a vendor key is configured, so a deployment with no
    # credentials degrades to deterministic replies rather than failing.
    gateway_factory = build_gateway_factory(settings)

    # WhatsApp is decided from whether the credentials exist, not from the
    # environment name: a deployment holding real Meta keys is talking to real
    # students whatever it calls itself, and one holding none cannot send at all.
    whatsapp = _build_whatsapp(settings)

    outbound: OutboundGateway = (
        WhatsAppOutboundGateway(client=whatsapp) if whatsapp else FakeOutboundGateway()
    )
    identity = FakeIdentityGateway()
    entitlement = _build_entitlement(settings, get_session_factory())
    deps = EntryDependencies(
        identity=identity,
        # Read from the `entitlements` table, which is what both payment
        # activation and the admin override endpoint already write. The old
        # config-list gateway meant a paid subscription and an operator's grant
        # both landed in a table nobody consulted.
        entitlement=entitlement,
        # The name the student chose at signup, read from `tutor_assignments`.
        # The fake returned a constant, so the persona field the form collects
        # reached the system prompt as somebody else's name.
        tutor=DatabaseTutorGateway(get_session_factory()),
        outbound=outbound,
        clock=SystemClock(),
        session_factory=get_session_factory(),
        gateway_factory=gateway_factory,
        # Configuration, not constants: the ceilings differ between staging and
        # production, and an operator raising one should not need a deploy of
        # new code to do it.
        subscribe_url=settings.public_site_url,
        system_limits=SystemLimits(
            daily_budget_micros=settings.system_daily_budget_micros,
            hourly_budget_micros=settings.system_hourly_budget_micros,
            provider_daily_budget_micros=settings.provider_daily_budget_micros,
            failure_circuit=settings.provider_failure_circuit,
        ),
    )
    # Media. The *only* place that decides filesystem-vs-R2 and recorder-vs-Cloud
    # Tasks, and it decides from whether the credentials exist rather than from
    # the environment name: a staging deployment with no R2 configured is
    # mis-wired whatever it calls itself, and `require_deployable()` has already
    # refused to start it. Tesseract reports itself unavailable when the binary
    # is absent, and the planner routes around it.
    #
    media_root = Path(settings.media_root)
    local_source = LocalFileMediaSource(root=media_root / "incoming")
    # Routed by `MediaRef.provider`, so a WhatsApp photo downloads from the
    # Graph API while a staged local fixture still resolves from disk.
    source = (
        RoutingMediaSource(
            default=local_source,
            routes={"whatsapp": WhatsAppMediaSource(client=whatsapp)},
        )
        if whatsapp
        else local_source
    )
    task_queue = _build_task_queue(settings)
    pipeline = MediaPipeline(
        source=source,
        blobstore=_build_blobstore(settings, media_root),
        ocr=TesseractOCRProvider(command=settings.tesseract_cmd),
        queue=task_queue,
        transcriber=_build_transcriber(settings),
    )

    # The pipeline is built before the entry service so the service can hold it:
    # an inbound attachment has to reach intake, or the media machinery is only
    # reachable from a job that nothing creates.
    deps = replace(deps, media_pipeline=pipeline)

    return Container(
        settings=settings,
        entry_service=TutorTwinEntryService(deps),
        internal_auth=InternalAuth(settings),
        outbound=outbound,
        identity=identity,
        entitlement=entitlement,
        media_pipeline=pipeline,
        gateway_factory=gateway_factory,
        task_queue=task_queue,
        whatsapp=whatsapp,
        cashfree=_build_cashfree(settings),
    )


def _build_whatsapp(settings: Settings) -> WhatsAppClient | None:
    if not settings.whatsapp_configured:
        logger.info("whatsapp_not_configured")
        return None

    assert settings.whatsapp_access_token and settings.whatsapp_phone_number_id  # noqa: S101
    logger.info(
        "whatsapp_enabled",
        phone_number_id=settings.whatsapp_phone_number_id,
        api_version=settings.whatsapp_graph_api_version,
        send_enabled=settings.whatsapp_send_enabled,
    )
    return WhatsAppClient(
        access_token=settings.whatsapp_access_token.get_secret_value(),
        phone_number_id=settings.whatsapp_phone_number_id,
        api_version=settings.whatsapp_graph_api_version,
        timeout_seconds=settings.whatsapp_http_timeout_seconds,
        send_enabled=settings.whatsapp_send_enabled,
    )


def _build_entitlement(settings: Settings, session_factory: object) -> EntitlementGateway:
    """Database first. The static list survives only as a local testing escape.

    `TUTORTWIN_FAKE_PRO_SUBJECTS` still works for a machine with no rows and no
    payment gateway, but it is now additive: a number in the list is treated as
    PRO, and everybody else is resolved from the database. In a deployed
    environment the list is normally empty and this is purely the database.
    """
    from tutortwin.services.entitlements import ACTIVE, FREE_PLAN

    database = DatabaseEntitlementGateway(session_factory)  # type: ignore[arg-type]
    if not settings.fake_pro_subjects:
        return database

    logger.warning(
        "entitlement_override_list_active",
        count=len(settings.fake_pro_subjects),
        detail="TUTORTWIN_FAKE_PRO_SUBJECTS grants PRO without payment",
    )
    fake = FakeEntitlementGateway(plans=dict.fromkeys(settings.fake_pro_subjects, "PRO"))

    class _ListThenDatabase:
        async def snapshot(self, subject: ResolvedSubject) -> EntitlementSnapshot:
            listed = await fake.snapshot(subject)
            if listed.plan_code != FREE_PLAN and listed.status.value == ACTIVE:
                return listed
            return await database.snapshot(subject)

    return _ListThenDatabase()


def _build_cashfree(settings: Settings) -> CashfreeClient | None:
    if not settings.cashfree_configured:
        logger.info("cashfree_not_configured")
        return None

    assert settings.cashfree_app_id and settings.cashfree_secret_key  # noqa: S101
    logger.info("cashfree_enabled", env=settings.cashfree_env, base_url=settings.cashfree_base_url)
    return CashfreeClient(
        app_id=settings.cashfree_app_id,
        secret_key=settings.cashfree_secret_key.get_secret_value(),
        base_url=settings.cashfree_base_url,
        api_version=settings.cashfree_api_version,
    )


def _build_transcriber(settings: Settings) -> TranscriptionProvider | None:
    """Speech-to-text, which only OpenAI provides among the permitted vendors.

    A deployment with an Anthropic key and no OpenAI key can tutor perfectly
    well and simply cannot hear voice notes, so this returns None rather than
    refusing to start.
    """
    if settings.openai_api_key is None:
        logger.info("transcription_unavailable_no_openai_key")
        return None
    return OpenAITranscriptionProvider(api_key=settings.openai_api_key.get_secret_value())


def _build_blobstore(settings: Settings, media_root: Path) -> BlobStore:
    if not settings.r2_configured:
        logger.info("blobstore_filesystem", root=str(media_root / "objects"))
        return FilesystemBlobStore(root=media_root / "objects")

    assert settings.r2_account_id and settings.r2_bucket  # noqa: S101 - checked above
    assert settings.r2_access_key_id and settings.r2_secret_access_key  # noqa: S101
    logger.info("blobstore_r2", bucket=settings.r2_bucket)
    return R2BlobStore(
        account_id=settings.r2_account_id,
        access_key_id=settings.r2_access_key_id.get_secret_value(),
        secret_access_key=settings.r2_secret_access_key.get_secret_value(),
        bucket=settings.r2_bucket,
    )


def _build_task_queue(settings: Settings) -> TaskQueue:
    # A server deployment dispatches to itself. Cloud Tasks exists to work
    # around Cloud Run throttling CPU between requests; on an always-on box it
    # would add a Google Cloud account, a queue and an OIDC round trip to solve
    # a problem that is not there.
    if not settings.is_serverless and settings.is_deployed:
        assert settings.internal_api_key  # noqa: S101 - require_deployable checked it
        logger.info("task_queue_in_process", base_url=settings.public_api_url)
        return InProcessTaskQueue(
            base_url=settings.public_api_url,
            internal_key=settings.internal_api_key.get_secret_value(),
        )

    if not settings.tasks_configured:
        # Recorded, not dispatched. Local and test runs drive the handler
        # directly; a fake that pretended to dispatch would hide that.
        logger.info("task_queue_recording")
        return RecordingTaskQueue()

    assert settings.tasks_project and settings.tasks_location  # noqa: S101 - checked above
    assert settings.tasks_queue and settings.tasks_target_url  # noqa: S101
    assert settings.tasks_service_account  # noqa: S101
    logger.info("task_queue_cloud_tasks", queue=settings.tasks_queue)
    return CloudTasksQueue(
        project=settings.tasks_project,
        location=settings.tasks_location,
        queue=settings.tasks_queue,
        target_url=settings.tasks_target_url,
        service_account_email=settings.tasks_service_account,
    )


def set_container(app: FastAPI, container: Container) -> None:
    app.state.container = container


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container
