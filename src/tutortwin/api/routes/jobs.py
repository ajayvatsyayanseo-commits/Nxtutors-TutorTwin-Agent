"""Internal job endpoint, invoked by Cloud Tasks.

**Authenticated, always.** Cloud Tasks presents a Google-signed OIDC token; the
shared secret is the equivalent for callers that are not on Google's identity
plane, and for local runs. An unauthenticated caller must never be able to
trigger media processing, because processing is where the money is.

The payload carries only a job id. All durable state is read from Postgres, so a
retried task cannot act on a stale snapshot of the work.

Three properties this handler owns:

**Idempotent.** A duplicate delivery of a settled job is a no-op. Cloud Tasks
guarantees at-least-once, so a handler that is not idempotent will eventually
process the same PDF twice and bill for it twice.

**Bounded.** Attempts are capped by the row, not only by the queue, and the
response tells Cloud Tasks whether retrying is worth its time: a 200 for
"finished or permanently failed", a 503 for "try again later".

**Backpressure.** A ceiling on concurrent heavy jobs, checked in the database
rather than in process memory, because there is no single process - there are as
many containers as Cloud Run decided to start.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from tutortwin.api.dependencies import Container, get_container
from tutortwin.db.engine import session_scope
from tutortwin.db.models import Job, MediaObject, Subject
from tutortwin.domain.budget import (
    BudgetOutcome,
    BudgetReason,
    ExecutionBudgetDecision,
)
from tutortwin.domain.events import (
    InboundMessage,
    MessageType,
    NormalizedEvent,
    OutboundAction,
    OutboundActionType,
    SubjectRef,
)
from tutortwin.domain.models import ResolvedSubject, SubjectStatus
from tutortwin.domain.provider import ModelAlias
from tutortwin.media.pipeline import ProcessOutcome
from tutortwin.observability.logging import get_logger
from tutortwin.policies import retry_policy
from tutortwin.repositories import catalog as catalog_repo
from tutortwin.repositories import media as media_repo
from tutortwin.services import retention

router = APIRouter(tags=["internal"])
logger = get_logger(__name__)


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: UUID


class JobResponse(BaseModel):
    job_id: UUID
    state: str
    detail: str | None = None


async def _running_heavy_jobs(session: object) -> int:
    """How many jobs are mid-flight across every container.

    Counted in Postgres because the answer is fleet-wide. A per-process
    semaphore would bound one container and let Cloud Run start twenty more.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    assert isinstance(session, AsyncSession)  # noqa: S101 - narrowing for mypy
    stale_before = datetime.now(UTC) - timedelta(minutes=15)
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(Job)
                .where(Job.state == "RUNNING", Job.updated_at >= stale_before)
            )
        ).scalar_one()
        or 0
    )


@router.post("/internal/jobs/run", response_model=JobResponse)
async def run_job(
    payload: JobRequest,
    response: Response,
    container: Container = Depends(get_container),
    x_internal_key: str | None = Header(default=None, alias="x-internal-key"),
    authorization: str | None = Header(default=None, alias="authorization"),
) -> JobResponse:
    container.internal_auth.verify(authorization=authorization, internal_key=x_internal_key)

    settings = container.settings

    async with session_scope() as session:
        job = await media_repo.load_job(session, payload.job_id)
        if job is None:
            logger.info("job_not_found", job_id=str(payload.job_id))
            return JobResponse(job_id=payload.job_id, state="NOT_FOUND")

        # Idempotent: a duplicate delivery of an already-finished job is a
        # no-op rather than a second execution.
        if job.state in {"SUCCEEDED", "FAILED_PERMANENT"}:
            logger.info("job_already_settled", job_id=str(job.id), state=job.state)
            return JobResponse(job_id=job.id, state=job.state, detail="already settled")

        policy = retry_policy.for_job_type(job.job_type)
        if policy.exhausted(job.attempts) or job.attempts >= job.max_attempts:
            await media_repo.mark_job(
                session, job, state="FAILED_PERMANENT", error="max attempts exhausted"
            )
            return JobResponse(job_id=job.id, state="FAILED_PERMANENT")

        # Shed load rather than pile onto a saturated fleet. 429 is deliberate:
        # Cloud Tasks re-delivers on its own backoff, so the work is not lost, it
        # is postponed - which is exactly what a concurrency ceiling should do.
        in_flight = await _running_heavy_jobs(session)
        if in_flight >= settings.heavy_job_max_concurrency:
            logger.info(
                "job_deferred_concurrency",
                job_id=str(job.id),
                in_flight=in_flight,
                ceiling=settings.heavy_job_max_concurrency,
            )
            response.status_code = 429
            return JobResponse(job_id=job.id, state=job.state, detail="concurrency ceiling")

        job.attempts += 1
        attempt_no = job.attempts
        await media_repo.mark_job(session, job, state="RUNNING")
        job_type = job.job_type
        media_object_id = job.media_object_id

    if job_type != "MEDIA_EXTRACT" or media_object_id is None:
        async with session_scope() as session:
            settled = await media_repo.load_job(session, payload.job_id)
            if settled is not None:
                await media_repo.mark_job(
                    session, settled, state="FAILED_PERMANENT", error=f"unknown job type {job_type}"
                )
        return JobResponse(job_id=payload.job_id, state="FAILED_PERMANENT")

    pipeline = container.media_pipeline
    if pipeline is None:
        return JobResponse(
            job_id=payload.job_id, state="RUNNING", detail="media pipeline not configured"
        )

    # `process` commits the read transaction before downloading and uploading,
    # so no transaction spans those. The extraction call after it does still run
    # inside one.
    # ponytail: extraction holds a transaction for the length of one vision
    # call; split it once media concurrency exceeds the pool, which needs
    # resumable EXTRACTING state rather than the forward-only machine we have.
    try:
        async with session_scope() as session:
            media = (
                await session.execute(select(MediaObject).where(MediaObject.id == media_object_id))
            ).scalar_one_or_none()
            if media is None:
                failed = await media_repo.load_job(session, payload.job_id)
                if failed is not None:
                    await media_repo.mark_job(
                        session, failed, state="FAILED_PERMANENT", error="media object missing"
                    )
                return JobResponse(job_id=payload.job_id, state="FAILED_PERMANENT")

            outcome = await pipeline.process(
                session,
                media,
                decision=await _media_budget(container, media),
                gateway=container.build_gateway(),
            )
            # Media spend, on the same ledger as tutoring spend.
            #
            # Vision and transcription calls were the only paid provider calls
            # in the system that wrote no `usage_ledger` row. The consequence is
            # not a missing report: `load_quota_snapshot` reads that table, so
            # every per-student and system budget ceiling was blind to them, and
            # a student sending photos all day was billed as if they had sent
            # nothing.
            for call in outcome.calls:
                await catalog_repo.record_model_call(
                    session,
                    call=call,
                    subject_id=media.subject_id,
                    # No request event: the job runs long after the request that
                    # queued it, and the column is nullable for exactly this.
                    request_event_id=None,
                    capability=f"MEDIA_{media.kind or 'UNKNOWN'}",
                )

            done = await media_repo.load_job(session, payload.job_id)
            if done is not None:
                await media_repo.mark_job(session, done, state="SUCCEEDED")
    except Exception as exc:
        # A crash mid-processing must leave the row retryable, not RUNNING
        # forever. The next attempt re-reads durable state, and extraction is
        # content-addressed, so redoing it costs a cache hit rather than a
        # second vision bill.
        logger.warning(
            "media_job_failed",
            job_id=str(payload.job_id),
            attempt=attempt_no,
            error_type=type(exc).__name__,
        )
        delay = policy.delay_for(attempt_no + 1)
        async with session_scope() as session:
            failed = await media_repo.load_job(session, payload.job_id)
            if failed is not None:
                terminal = policy.exhausted(failed.attempts)
                retry_at = None if terminal else datetime.now(UTC) + timedelta(seconds=delay)
                await media_repo.mark_job(
                    session,
                    failed,
                    state="FAILED_PERMANENT" if terminal else "FAILED",
                    error=f"{type(exc).__name__}: {exc}",
                    next_retry_at=retry_at,
                )
        # 503 asks Cloud Tasks to retry; 200 tells it to stop. Returning 200 on a
        # transient failure silently drops the student's document.
        response.status_code = 200 if policy.exhausted(attempt_no) else 503
        return JobResponse(job_id=payload.job_id, state="FAILED", detail=type(exc).__name__)

    logger.info(
        "media_job_complete",
        job_id=str(payload.job_id),
        attempt=attempt_no,
        media_state=outcome.state.value,
        ocr_pages=outcome.ocr_pages,
        vision_pages=outcome.vision_pages,
        cache_hits=outcome.cache_hits,
    )

    # Answer the student.
    #
    # Intake already told them "I am reading your file now, I will come back with
    # the answer shortly". Until this call existed, nothing ever did: the text was
    # extracted, written to `media_extractions`, and the conversation stopped.
    # A promise made in the request path has to be kept in the job path.
    if outcome.enhanced_note is not None:
        # They asked for a clearer picture, so a picture is the answer. Routing
        # this through the tutor instead would reply to a question nobody asked.
        await _send_enhanced_image(container, media_object_id, outcome)
    else:
        await _answer_from_media(container, media_object_id)
    return JobResponse(job_id=payload.job_id, state="SUCCEEDED", detail=outcome.state.value)


async def _send_enhanced_image(
    container: Container, media_object_id: UUID, outcome: ProcessOutcome
) -> None:
    """Send the cleaned-up photo back.

    The note goes through the ordinary outbound gateway so it reaches whatever
    channel the student is on; the image itself goes as raw bytes through the
    WhatsApp client, the same way typeset maths does, because Meta fetching a
    link would require the blob store to be publicly reachable and it is not.

    Never raises, for the same reason `_answer_from_media` does not: the work is
    done and stored, and failing here would re-run the whole job to fix a
    delivery problem.
    """
    try:
        async with session_scope() as session:
            media = (
                await session.execute(select(MediaObject).where(MediaObject.id == media_object_id))
            ).scalar_one_or_none()
            if media is None:
                return
            subject_row = (
                await session.execute(select(Subject).where(Subject.id == media.subject_id))
            ).scalar_one_or_none()
            if subject_row is None:
                return
            subject = ResolvedSubject(
                id=subject_row.id,
                external_type=subject_row.external_identity_type,
                external_id=subject_row.external_identity_value,
                display_name=subject_row.display_name,
                status=SubjectStatus.ACTIVE,
            )

        # One message, not two: the explanation rides as the image's caption so
        # the student sees what changed next to the picture that changed.
        if (
            outcome.enhanced_png is not None
            and container.whatsapp is not None
            and subject.external_type == "whatsapp"
        ):
            sent = await container.whatsapp.send_media(
                subject.external_id,
                data=outcome.enhanced_png,
                mime_type="image/png",
                filename="cleaned.png",
                kind="image",
                caption=outcome.enhanced_note,
            )
            if sent is not None:
                return
            logger.info("enhanced_image_upload_failed", media_id=str(media_object_id))

        # Either there was no picture worth sending back, the channel cannot
        # carry one, or the upload failed. The note still goes out - it is the
        # honest answer on its own, and silence is the one outcome to avoid.
        await container.outbound.deliver(
            subject,
            (OutboundAction(type=OutboundActionType.SEND_TEXT, text=outcome.enhanced_note),),
        )
    except Exception as exc:
        logger.warning(
            "enhanced_image_delivery_failed",
            media_id=str(media_object_id),
            error_type=type(exc).__name__,
        )


async def _answer_from_media(container: Container, media_object_id: UUID) -> None:
    """Turn extracted text into the answer the student was promised.

    Routed back through the ordinary entry service rather than answering here,
    so the extracted question gets exactly what a typed one gets: intent
    routing, the budget gate, the tutor persona, conversation memory,
    persistence and delivery. A second answer path would drift from the first
    one within a month.

    The synthetic event id is derived from the media id, so a Cloud Tasks
    redelivery lands on the same idempotency key and replays the stored answer
    instead of paying for a second one.

    Never raises. The extraction succeeded and is stored; failing the job here
    would retry the whole thing - re-downloading and possibly re-paying for
    vision - to fix a delivery problem.
    """
    try:
        async with session_scope() as session:
            media = (
                await session.execute(select(MediaObject).where(MediaObject.id == media_object_id))
            ).scalar_one_or_none()
            if media is None:
                return

            subject = (
                await session.execute(select(Subject).where(Subject.id == media.subject_id))
            ).scalar_one_or_none()
            if subject is None:
                logger.warning("media_answer_no_subject", media_id=str(media_object_id))
                return

            rows = await media_repo.load_extractions(session, media)
            extracted = "\n\n".join(r.text for r in rows if r.text and r.text.strip())
            brief = (media.brief or "").strip()
            subject_id = subject.id
            external_type = subject.external_identity_type
            external_id = subject.external_identity_value

        if not extracted:
            # Read nothing. Say so - the alternative is asking a model to answer
            # a question that was never recovered, which produces a confident
            # answer to an invented question.
            await container.outbound.deliver(
                ResolvedSubject(
                    id=subject_id,
                    external_type=external_type,
                    external_id=external_id,
                    display_name=None,
                    status=SubjectStatus.ACTIVE,
                ),
                (
                    OutboundAction(
                        type=OutboundActionType.SEND_TEXT,
                        text=(
                            "I could not read anything from that file. Could you send a "
                            "clearer photo, or type the question out?"
                        ),
                    ),
                ),
            )
            logger.info("media_answer_empty_extraction", media_id=str(media_object_id))
            return

        # The brief leads, because it is what the student actually asked -
        # "solve question 4" against a page holding six questions.
        question = (
            f"{brief}\n\nHere is what the file says:\n{extracted}"
            if brief
            else f"Here is what the file says:\n{extracted}"
        )

        event_id = f"media-{media_object_id}"
        await container.entry_service.handle_event(
            NormalizedEvent(
                event_id=event_id,
                request_id=event_id,
                correlation_id=event_id,
                source=external_type,
                source_agent="media_job",
                subject=SubjectRef(external_type=external_type, external_id=external_id),
                message=InboundMessage(
                    message_id=event_id,
                    type=MessageType.TEXT,
                    text=question[:16_000],
                ),
                occurred_at=datetime.now(UTC),
            )
        )
        logger.info("media_answer_delivered", media_id=str(media_object_id))
    except Exception as exc:  # noqa: BLE001 - delivery must not fail the job
        logger.error(
            "media_answer_failed",
            media_id=str(media_object_id),
            error_type=type(exc).__name__,
        )


async def _media_budget(container: Container, media: MediaObject) -> ExecutionBudgetDecision:
    """May this media job spend money?

    **This function existing at all is the fix for a critical bug.** The call
    site used to pass `decision=None`, and every paid media path checks
    `if decision is None: return None`. Since this job handler is the ONLY path
    that runs media in production, that single argument meant:

      - a photo whose local OCR scored badly was never escalated to vision,
      - a voice note was never transcribed,
      - a scanned PDF page was never read.

    All of it failed silently, marked the media READY_FOR_CAPABILITY, and left
    the tutor answering a question it had not seen.

    The gate is re-established here rather than trusted from intake because
    intake ran in a different request, possibly minutes ago: a subscription can
    lapse in between, and the job must not spend on a student who is no longer
    entitled. Entitlement is the primary cost gate; the per-provider and
    system-wide ceilings are enforced independently inside the model gateway.
    """
    subject = ResolvedSubject(
        id=media.subject_id,
        external_type="whatsapp",
        external_id=str(media.subject_id),
        display_name=None,
        status=SubjectStatus.ACTIVE,
    )
    snapshot = await container.entitlement.snapshot(subject)

    if not snapshot.allows_paid_ai:
        logger.info(
            "media_budget_refused",
            subject_id=str(media.subject_id),
            plan_code=snapshot.plan_code,
        )
        return ExecutionBudgetDecision(
            outcome=BudgetOutcome.REJECT_PLAN,
            reason=BudgetReason.PLAN_DISALLOWS_AI,
            alias=None,
        )

    # VISION is the alias the extractor requests; the transcriber asks for
    # TRANSCRIBE itself. Only `permits_paid_call` is read from this decision,
    # so the alias names the dominant cost rather than constraining the call.
    return ExecutionBudgetDecision(
        outcome=BudgetOutcome.ALLOW_STANDARD,
        reason=BudgetReason.ROUTINE_MODERATE,
        alias=ModelAlias.VISION,
    )


class SweepRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_size: int = retention.DEFAULT_BATCH


class SweepResponse(BaseModel):
    blobs_deleted: int
    media_rows_deleted: int
    extractions_deleted: int
    blob_errors: int


@router.post("/internal/retention/sweep", response_model=SweepResponse)
async def sweep_retention(
    payload: SweepRequest,
    container: Container = Depends(get_container),
    x_internal_key: str | None = Header(default=None, alias="x-internal-key"),
    authorization: str | None = Header(default=None, alias="authorization"),
) -> SweepResponse:
    """Delete expired media rows and their extracted text.

    Driven by Cloud Scheduler, which presents the same OIDC identity Cloud Tasks
    does. It is a plain endpoint rather than a queued job because the work is
    idempotent, stateless and bounded: a job row would add durability that a
    sweep does not need, since the next scheduled run picks up whatever this one
    missed.

    Deliberately not batch-unbounded. One call deletes at most `batch_size`
    objects; the schedule, not the request, is what eventually clears a backlog.
    """
    container.internal_auth.verify(authorization=authorization, internal_key=x_internal_key)

    pipeline = container.media_pipeline
    if pipeline is None:
        return SweepResponse(
            blobs_deleted=0, media_rows_deleted=0, extractions_deleted=0, blob_errors=0
        )

    async with session_scope() as session:
        result = await retention.sweep(
            session,
            blobstore=pipeline.blobstore,
            batch_size=max(1, min(payload.batch_size, 1000)),
        )

    return SweepResponse(
        blobs_deleted=result.blobs_deleted,
        media_rows_deleted=result.media_rows_deleted,
        extractions_deleted=result.extractions_deleted,
        blob_errors=result.blob_errors,
    )
