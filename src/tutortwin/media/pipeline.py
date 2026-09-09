"""The media pipeline: from a reference to extracted text, gated at every step.

This is where the phase's core invariant lives:

    NO BRIEF -> NO EXPENSIVE PROCESSING

`intake()` is the gate. It runs on the inbound event, inside the request, and
its only expensive act for an unbriefed attachment is writing one row. No fetch,
no parse, no OCR, no vision, no embedding.

`process()` runs afterwards - in a job for real deployments - and only ever sees
media that already passed both the entitlement check and the brief gate, because
the state machine will not let it reach FETCH_QUEUED otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from tutortwin.db.models import MediaObject
from tutortwin.domain.budget import ExecutionBudgetDecision, QuotaSnapshot
from tutortwin.domain.events import MediaRef, MessageType
from tutortwin.domain.media import (
    DEFAULT_LIMITS,
    ExtractionMethod,
    ExtractionResult,
    MediaKind,
    MediaLimits,
    MediaState,
    PageExtraction,
    RejectReason,
)
from tutortwin.domain.provider import ModelCall
from tutortwin.media import enhance
from tutortwin.media.adapters import MediaNotFound, MediaSource, TaskQueue
from tutortwin.media.audio import TranscriptionProvider, VoicePipeline
from tutortwin.media.blobstore import DEFAULT_RETENTION_DAYS, BlobStore
from tutortwin.media.extractor import IMAGE_PARSER_VERSION, ImageExtractor, PdfExtractor
from tutortwin.media.ocr import OCRProvider
from tutortwin.media.pdf import PARSER_VERSION, PdfError, inspect, plan_extraction
from tutortwin.media.validation import validate, validate_image_dimensions
from tutortwin.observability.logging import get_logger
from tutortwin.repositories import media as media_repo

logger = get_logger(__name__)

AUDIO_PARSER_VERSION = "audio-1"
"""Versions transcripts separately from page extractions, so the shared
content-addressed cache never confuses a transcript with a page of OCR."""

BRIEF_PROMPT = (
    "I can see your attachment. Tell me what you would like me to do with it - "
    'for example "solve question 4" or "summarize pages 2-3".'
)

MIN_BRIEF_CHARS = 3

MEDIA_MESSAGE_TYPES: frozenset[MessageType] = frozenset(
    {MessageType.IMAGE, MessageType.PDF, MessageType.DOCUMENT}
)
"""AUDIO is deliberately excluded: a voice note IS the request, so there is
nothing to brief. It is still entitlement-gated before transcription."""

_KIND_FOR_MESSAGE: dict[MessageType, MediaKind] = {
    MessageType.IMAGE: MediaKind.IMAGE,
    MessageType.PDF: MediaKind.PDF,
    MessageType.DOCUMENT: MediaKind.DOCUMENT,
    MessageType.AUDIO: MediaKind.AUDIO,
}


def has_brief(text: str | None) -> bool:
    return len((text or "").strip()) >= MIN_BRIEF_CHARS


_KIND_FOR_MESSAGE_TYPE: dict[MessageType, MediaKind] = {
    MessageType.IMAGE: MediaKind.IMAGE,
    MessageType.PDF: MediaKind.PDF,
    MessageType.AUDIO: MediaKind.AUDIO,
    MessageType.DOCUMENT: MediaKind.DOCUMENT,
}
"""What the SENDER said it is, recorded at intake.

`record_fetched` overwrites this with what the bytes actually are, which is the
authoritative answer - but that happens after the download, and a file parked in
WAITING_FOR_BRIEF has not been downloaded. Leaving it NULL until then meant a
resumed brief looked up its own media type and found nothing, defaulted to
DOCUMENT, and `_projected_units` returned an empty dict - so the student's daily
OCR ceiling was not checked on that path at all. Answering the brief prompt was
a way to bypass the media budget entirely.
"""


def _projected_units(message_type: MessageType) -> dict[str, int]:
    """What one more of this media type would consume.

    A single unit, not a guess at the real size: the page count and duration are
    only known after the file is fetched, and fetching to decide whether we are
    allowed to fetch is the cost this gate exists to avoid.
    """
    if message_type is MessageType.PDF:
        return {"pdf_pages": 1}
    if message_type is MessageType.IMAGE:
        return {"ocr_pages": 1}
    if message_type is MessageType.AUDIO:
        return {"voice_seconds": 1}
    return {}


@dataclass(slots=True)
class IntakeOutcome:
    """What intake decided. `needs_brief` is the zero-cost hold."""

    media: MediaObject | None
    needs_brief: bool = False
    rejected: bool = False
    reject_reason: RejectReason | None = None
    job_created: bool = False
    job_id: UUID | None = None


@dataclass(slots=True)
class ProcessOutcome:
    state: MediaState
    extraction: ExtractionResult | None = None
    reject_reason: RejectReason | None = None
    calls: list[ModelCall] = field(default_factory=list)
    ocr_pages: int = 0
    vision_pages: int = 0
    cache_hits: int = 0
    fetched: bool = False

    enhanced_png: bytes | None = None
    """Set when the student asked for a cleaner photo rather than an answer.

    Its presence is what tells the job to send a picture back instead of
    routing extracted text to the tutor."""

    enhanced_note: str | None = None
    """What to say alongside it. Written by `enhance.describe`, which is
    careful never to claim it recovered focus that was not in the image."""


class MediaPipeline:
    def __init__(
        self,
        *,
        source: MediaSource,
        blobstore: BlobStore,
        ocr: OCRProvider,
        queue: TaskQueue | None = None,
        limits: MediaLimits = DEFAULT_LIMITS,
        transcriber: TranscriptionProvider | None = None,
    ) -> None:
        self._source = source
        self._blobstore = blobstore
        self._ocr = ocr
        self._queue = queue
        self._limits = limits
        # None means "this deployment cannot hear voice notes". They are still
        # accepted and stored; they simply produce no transcript, and the
        # capability layer says so rather than answering silence.
        self._transcriber = transcriber

    @property
    def blobstore(self) -> BlobStore:
        """Exposed for the retention sweeper, which deletes what this wrote."""
        return self._blobstore

    # ------------------------------------------------------------- intake ---
    async def intake(
        self,
        session: AsyncSession,
        *,
        subject_id: UUID,
        conversation_id: UUID | None,
        ref: MediaRef,
        message_type: MessageType,
        brief: str | None,
        entitled: bool,
        correlation_id: str | None = None,
        allowance: QuotaSnapshot | None = None,
    ) -> IntakeOutcome:
        """Runs inside the request. Costs one row at most.

        Ordering is the cost policy: entitlement before anything, then the
        student's own daily allowance, then the brief gate, then - and only then
        - a fetch job.

        `allowance` is the caller's quota snapshot. None means "not enforced",
        which is what the unit tests and the local harness want; the entry
        service always passes one.
        """
        media = await media_repo.get_or_create_media(
            session, subject_id=subject_id, conversation_id=conversation_id, ref=ref
        )
        # Only while it is still unknown: once the file has been fetched and
        # sniffed, the validator's answer is the true one and this claim is not.
        if media.kind is None:
            declared = _KIND_FOR_MESSAGE_TYPE.get(message_type)
            if declared is not None:
                media.kind = str(declared)
        state = MediaState(media.state)

        # Already past intake: a redelivered event must not restart the pipeline.
        # WAITING_FOR_BRIEF is excluded here deliberately - that state is resumable,
        # because the brief it is waiting for may be in this very message.
        if state not in {MediaState.RECEIVED_REFERENCE, MediaState.WAITING_FOR_BRIEF}:
            logger.info("media_intake_replay", state=state.value)
            return IntakeOutcome(media=media)

        # 1. Entitlement, before a single byte is fetched.
        if not entitled:
            await media_repo.transition(
                session, media, MediaState.REJECTED, reject_reason=RejectReason.ENTITLEMENT
            )
            logger.info("media_rejected_entitlement", fetched=False)
            return IntakeOutcome(media=media, rejected=True, reject_reason=RejectReason.ENTITLEMENT)

        if state is MediaState.RECEIVED_REFERENCE:
            await media_repo.transition(session, media, MediaState.ENTITLEMENT_CHECKED)

        # 1b. The student's own daily ceiling. Checked here because this is the
        #     last point at which refusing costs nothing: one step further and
        #     the bytes are fetched, and a page read by vision is already paid
        #     for. One unit is projected rather than the real page count, which
        #     is not known until the file is read - the ceiling is therefore
        #     "may they process anything more today", not an exact accounting.
        if allowance is not None:
            projected = _projected_units(message_type)
            exceeded = allowance.media_allowance_exceeded(**projected)
            if exceeded is not None:
                await media_repo.transition(
                    session, media, MediaState.REJECTED, reject_reason=RejectReason.DAILY_ALLOWANCE
                )
                logger.info("media_rejected_allowance", allowance=exceeded, fetched=False)
                return IntakeOutcome(
                    media=media, rejected=True, reject_reason=RejectReason.DAILY_ALLOWANCE
                )

        # 2. The brief gate. Audio is exempt - the voice is the request.
        if message_type in MEDIA_MESSAGE_TYPES and not has_brief(brief):
            await media_repo.transition(session, media, MediaState.WAITING_FOR_BRIEF)
            logger.info("media_waiting_for_brief", fetched=False, ocr=0, vision=0)
            return IntakeOutcome(media=media, needs_brief=True)

        await media_repo.attach_brief(session, media, brief or "")

        # 3. Only now may work be scheduled.
        await media_repo.transition(session, media, MediaState.FETCH_QUEUED)
        job, created = await media_repo.enqueue_job(
            session,
            job_type="MEDIA_EXTRACT",
            idempotency_key=f"media:{media.id}",
            owner_subject_id=subject_id,
            media_object_id=media.id,
            correlation_id=correlation_id,
        )
        if created and job is not None and self._queue is not None:
            await self._queue.enqueue(job.id)

        return IntakeOutcome(
            media=media,
            job_created=created,
            job_id=job.id if job else None,
        )

    # ------------------------------------------------------------ process ---
    async def process(
        self,
        session: AsyncSession,
        media: MediaObject,
        *,
        decision: ExecutionBudgetDecision | None,
        gateway: object | None = None,
    ) -> ProcessOutcome:
        """Fetch, validate, plan and extract. Runs outside the request path.

        Refuses to act on media that has not reached FETCH_QUEUED, so the brief
        gate cannot be bypassed by calling this directly.
        """
        state = MediaState(media.state)
        if state not in {MediaState.FETCH_QUEUED, MediaState.FETCHED}:
            logger.warning("media_process_refused", state=state.value)
            return ProcessOutcome(state=state)

        subject_id = media.subject_id

        # Close the caller's read transaction before the two slowest hops.
        #
        # The job loads `media` with a SELECT, which opens a transaction, and
        # the next writes are minutes of wall-clock away: a download from Meta
        # and a multi-megabyte upload to R2. Holding a transaction across those
        # pins one of only four pooled connections and, on a database shared
        # with another product, holds back autovacuum's xmin for the duration.
        #
        # Nothing has been written yet, so this commits an empty transaction -
        # there is no state here to lose, and a crash before the next write
        # still leaves the row at FETCH_QUEUED for the retry. Commit rather
        # than rollback because rollback expires `media` and the next attribute
        # read would lazy-load from inside async code.
        await session.commit()

        # --- fetch -----------------------------------------------------------
        try:
            data = await self._source.fetch(media.source, media.source_media_id)
        except MediaNotFound:
            await media_repo.transition(session, media, MediaState.FAILED)
            return ProcessOutcome(state=MediaState.FAILED)

        # --- validate --------------------------------------------------------
        result = validate(data, limits=self._limits, declared_mime=media.mime_type)
        if not result.ok or result.kind is None or result.mime is None:
            await media_repo.transition(
                session, media, MediaState.FETCHED
            )  # bytes were fetched; record that before rejecting
            await media_repo.transition(
                session, media, MediaState.REJECTED, reject_reason=result.reason
            )
            logger.info("media_rejected", reason=result.reason.value if result.reason else None)
            return ProcessOutcome(
                state=MediaState.REJECTED, reject_reason=result.reason, fetched=True
            )

        if result.kind is MediaKind.IMAGE:
            dimensions = validate_image_dimensions(data, self._limits)
            if not dimensions.ok:
                await media_repo.transition(session, media, MediaState.FETCHED)
                await media_repo.transition(
                    session, media, MediaState.REJECTED, reject_reason=dimensions.reason
                )
                return ProcessOutcome(
                    state=MediaState.REJECTED,
                    reject_reason=dimensions.reason,
                    fetched=True,
                )

        blob = await self._blobstore.put(
            subject_id=subject_id,
            data=data,
            content_type=result.mime,
            extension=result.mime.split("/")[-1],
            retention_days=DEFAULT_RETENTION_DAYS,
        )
        await media_repo.record_fetched(
            session,
            media,
            blob_key=blob.key,
            sha256=blob.sha256,
            mime_type=result.mime,
            size_bytes=len(data),
            kind=result.kind,
            expires_at=blob.expires_at,
        )
        await media_repo.transition(session, media, MediaState.VALIDATED)

        if result.kind is MediaKind.IMAGE:
            return await self._process_image(
                session,
                media,
                data=data,
                mime_type=result.mime,
                sha256=blob.sha256,
                subject_id=subject_id,
                decision=decision,
                gateway=gateway,
            )

        if result.kind is MediaKind.AUDIO:
            return await self._process_audio(
                session,
                media,
                data=data,
                mime_type=result.mime,
                sha256=blob.sha256,
                subject_id=subject_id,
                decision=decision,
            )

        if result.kind is not MediaKind.PDF:
            # Other documents. Their content is handled by the capability that
            # asked for them, not read here.
            await media_repo.transition(session, media, MediaState.EXTRACTION_PLANNED)
            await media_repo.transition(session, media, MediaState.EXTRACTING)
            await media_repo.transition(session, media, MediaState.READY_FOR_CAPABILITY)
            return ProcessOutcome(state=MediaState.READY_FOR_CAPABILITY, fetched=True)

        # --- plan ------------------------------------------------------------
        try:
            inspection = inspect(data, self._limits)
        except PdfError as exc:
            await media_repo.transition(
                session, media, MediaState.REJECTED, reject_reason=exc.reason
            )
            logger.info("pdf_rejected", reason=exc.reason.value, detail=exc.detail)
            return ProcessOutcome(state=MediaState.REJECTED, reject_reason=exc.reason, fetched=True)

        media.page_count = inspection.page_count
        plan = plan_extraction(
            inspection,
            media.brief or "",
            self._limits,
            ocr_available=self._ocr.available,
        )
        await media_repo.transition(session, media, MediaState.EXTRACTION_PLANNED)
        logger.info(
            "extraction_planned",
            total_pages=inspection.page_count,
            planned_pages=len(plan.pages),
            ocr_pages=plan.ocr_page_count,
            vision_pages=plan.vision_page_count,
            targeting=plan.targeting_reason,
        )

        # --- cache -----------------------------------------------------------
        wanted = tuple(p.page_number for p in plan.pages)
        cached = await media_repo.load_cached_extraction(
            session,
            subject_id=subject_id,
            sha256=blob.sha256,
            parser_version=PARSER_VERSION,
            pages=wanted,
        )
        remaining = tuple(p for p in plan.pages if p.page_number not in cached)
        cache_hits = len(cached)

        await media_repo.transition(session, media, MediaState.EXTRACTING)

        pages: list[PageExtraction] = [
            media_repo.to_page_extraction(row) for row in cached.values()
        ]
        outcome_calls: list[ModelCall] = []
        ocr_pages = vision_pages = 0
        escalated = False
        escalation_reason: str | None = None

        if remaining:
            from tutortwin.domain.media import ExtractionPlan

            extractor = PdfExtractor(
                ocr=self._ocr,
                gateway=gateway,  # type: ignore[arg-type]
                limits=self._limits,
            )
            extraction = await extractor.run(
                data=data,
                inspection=inspection,
                plan=ExtractionPlan(
                    pages=remaining,
                    total_pages=inspection.page_count,
                    targeting_reason=plan.targeting_reason,
                ),
                brief=media.brief or "",
                decision=decision,
            )
            pages.extend(extraction.result.pages)
            outcome_calls.extend(extraction.calls)
            ocr_pages = extraction.ocr_pages
            vision_pages = extraction.vision_pages
            escalated = extraction.result.escalated_to_vision
            escalation_reason = extraction.result.escalation_reason

            for page in extraction.result.pages:
                await media_repo.store_extraction(
                    session,
                    subject_id=subject_id,
                    sha256=blob.sha256,
                    parser_version=PARSER_VERSION,
                    ocr_version=extraction.result.ocr_version,
                    page=page,
                )

        pages.sort(key=lambda p: p.page_number)
        await media_repo.transition(session, media, MediaState.READY_FOR_CAPABILITY)

        logger.info(
            "extraction_complete",
            pages=len(pages),
            ocr_pages=ocr_pages,
            vision_pages=vision_pages,
            cache_hits=cache_hits,
        )
        return ProcessOutcome(
            state=MediaState.READY_FOR_CAPABILITY,
            extraction=ExtractionResult(
                pages=tuple(pages),
                parser_version=PARSER_VERSION,
                ocr_version="tesseract-5" if ocr_pages else None,
                escalated_to_vision=escalated,
                escalation_reason=escalation_reason,
            ),
            calls=outcome_calls,
            ocr_pages=ocr_pages,
            vision_pages=vision_pages,
            cache_hits=cache_hits,
            fetched=True,
        )

    async def _process_audio(
        self,
        session: AsyncSession,
        media: MediaObject,
        *,
        data: bytes,
        mime_type: str,
        sha256: str,
        subject_id: UUID,
        decision: ExecutionBudgetDecision | None,
    ) -> ProcessOutcome:
        """Turn a voice note into text.

        A voice note is exempt from the brief gate because the voice *is* the
        request - which only holds if somebody actually listens to it. Storing
        the audio and marking it ready leaves the tutor answering silence.

        Entitlement was checked at intake; the budget decision is checked again
        here because transcription is billed by audio duration and this is the
        point where that bill is incurred.
        """
        await media_repo.transition(session, media, MediaState.EXTRACTION_PLANNED)
        await media_repo.transition(session, media, MediaState.EXTRACTING)

        cached = await media_repo.load_cached_extraction(
            session,
            subject_id=subject_id,
            sha256=sha256,
            parser_version=AUDIO_PARSER_VERSION,
            pages=(1,),
        )
        pages: tuple[PageExtraction, ...] = ()
        if cached:
            pages = tuple(media_repo.to_page_extraction(row) for row in cached.values())
            await media_repo.transition(session, media, MediaState.READY_FOR_CAPABILITY)
            return ProcessOutcome(
                state=MediaState.READY_FOR_CAPABILITY,
                extraction=ExtractionResult(pages=pages, parser_version=AUDIO_PARSER_VERSION),
                cache_hits=len(pages),
                fetched=True,
            )

        permitted = decision is not None and decision.permits_paid_call
        result = await VoicePipeline(transcriber=self._transcriber, limits=self._limits).transcribe(
            data, mime_type=mime_type, entitled=permitted
        )

        if result.text.strip():
            page = PageExtraction(
                page_number=1, method=ExtractionMethod.TRANSCRIPTION, text=result.text
            )
            pages = (page,)
            await media_repo.store_extraction(
                session,
                subject_id=subject_id,
                sha256=sha256,
                parser_version=AUDIO_PARSER_VERSION,
                ocr_version=None,
                page=page,
            )

        await media_repo.transition(session, media, MediaState.READY_FOR_CAPABILITY)
        logger.info(
            "audio_transcription_complete",
            chars=len(result.text),
            seconds=result.duration_seconds,
            paid=result.call is not None,
        )
        return ProcessOutcome(
            state=MediaState.READY_FOR_CAPABILITY,
            extraction=ExtractionResult(pages=pages, parser_version=AUDIO_PARSER_VERSION),
            calls=[result.call] if result.call is not None else [],
            fetched=True,
        )

    async def _process_image(
        self,
        session: AsyncSession,
        media: MediaObject,
        *,
        data: bytes,
        mime_type: str,
        sha256: str,
        subject_id: UUID,
        decision: ExecutionBudgetDecision | None,
        gateway: object | None,
    ) -> ProcessOutcome:
        """Read a photo of a homework page.

        The commonest thing a student sends on WhatsApp, and the one case where
        storing the file without reading it would leave the tutor answering a
        question it never saw.

        Content-addressed like the PDF path: the same photo sent twice - which
        happens constantly when a student is not sure the first one went
        through - is read once and paid for once.
        """
        await media_repo.transition(session, media, MediaState.EXTRACTION_PLANNED)
        await media_repo.transition(session, media, MediaState.EXTRACTING)

        # "This is blurry, can you make it clearer?" is a request for a picture,
        # not for an answer. Handled before extraction because the student did
        # not ask a question - running OCR and then a vision model here would
        # bill them for reading a page they only wanted cleaned up.
        #
        # `wants_enhancement` is deliberately narrow and rejects "solve this,
        # the photo is blurry", which stays on the ordinary path below.
        if enhance.wants_enhancement(media.brief):
            try:
                cleaned = enhance.enhance(data)
            except Exception:
                # Presentation, not correctness: fall through and answer the
                # page instead of failing the job over a filter.
                logger.warning("image_enhancement_failed", media_id=str(media.id))
            else:
                await media_repo.transition(session, media, MediaState.READY_FOR_CAPABILITY)
                logger.info(
                    "image_enhanced",
                    sharpness_before=round(cleaned.before.sharpness, 1),
                    sharpness_after=round(cleaned.after.sharpness, 1),
                    improved=cleaned.improved,
                )
                return ProcessOutcome(
                    state=MediaState.READY_FOR_CAPABILITY,
                    fetched=True,
                    # No picture when the result is still unreadable:
                    # `describe` says so and tells them how to retake it, and
                    # sending an image under that sentence contradicts it.
                    enhanced_png=cleaned.png if cleaned.rescued else None,
                    enhanced_note=enhance.describe(cleaned),
                )

        cached = await media_repo.load_cached_extraction(
            session,
            subject_id=subject_id,
            sha256=sha256,
            parser_version=IMAGE_PARSER_VERSION,
            pages=(1,),
        )
        if cached:
            pages = tuple(media_repo.to_page_extraction(row) for row in cached.values())
            await media_repo.transition(session, media, MediaState.READY_FOR_CAPABILITY)
            logger.info("image_extraction_cache_hit", chars=sum(len(p.text) for p in pages))
            return ProcessOutcome(
                state=MediaState.READY_FOR_CAPABILITY,
                extraction=ExtractionResult(pages=pages, parser_version=IMAGE_PARSER_VERSION),
                cache_hits=len(pages),
                fetched=True,
            )

        extraction = await ImageExtractor(
            ocr=self._ocr,
            gateway=gateway,  # type: ignore[arg-type]
            limits=self._limits,
        ).run(
            data=data,
            mime_type=mime_type,
            brief=media.brief or "",
            decision=decision,
        )

        for page in extraction.result.pages:
            await media_repo.store_extraction(
                session,
                subject_id=subject_id,
                sha256=sha256,
                parser_version=IMAGE_PARSER_VERSION,
                ocr_version=extraction.result.ocr_version,
                page=page,
            )

        await media_repo.transition(session, media, MediaState.READY_FOR_CAPABILITY)
        logger.info(
            "image_extraction_complete",
            ocr_pages=extraction.ocr_pages,
            vision_pages=extraction.vision_pages,
            chars=sum(len(p.text) for p in extraction.result.pages),
        )
        return ProcessOutcome(
            state=MediaState.READY_FOR_CAPABILITY,
            extraction=extraction.result,
            calls=extraction.calls,
            ocr_pages=extraction.ocr_pages,
            vision_pages=extraction.vision_pages,
            fetched=True,
        )


def retention_expiry(days: int = DEFAULT_RETENTION_DAYS) -> datetime:
    return datetime.now(UTC) + timedelta(days=days)
