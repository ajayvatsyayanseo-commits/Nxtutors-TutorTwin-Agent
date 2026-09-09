"""Phase 03 mandatory media scenarios, against real PostgreSQL.

Every test asserts *exact counts* - downloads, OCR pages, vision calls,
transcriptions - because the phase exists to make accidental media spending
structurally hard, and a claim about spending is only as good as its counter.
"""

from __future__ import annotations

import io
import tempfile
from pathlib import Path
from uuid import uuid4

import pymupdf
import pytest
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tutortwin.db.models import Job, MediaExtraction, MediaObject, Subject
from tutortwin.domain.budget import (
    BudgetOutcome,
    BudgetReason,
    ExecutionBudgetDecision,
)
from tutortwin.domain.events import MediaRef, MessageType
from tutortwin.domain.media import MediaLimits, MediaState, RejectReason
from tutortwin.domain.provider import ModelAlias, ModelCatalogEntry, Provider
from tutortwin.media.adapters import InMemoryMediaSource, RecordingTaskQueue
from tutortwin.media.blobstore import FilesystemBlobStore
from tutortwin.media.ocr import OcrResult, UnavailableOCRProvider
from tutortwin.media.pipeline import MediaPipeline
from tutortwin.providers.fake_models import FakeModelProvider, ScriptedReply
from tutortwin.providers.gateway import ModelGateway

pytestmark = pytest.mark.integration


# --- fixtures and builders ----------------------------------------------------


class FakeOCR:
    """Scriptable OCR that counts pages read."""

    def __init__(self, *, available: bool = True, result: OcrResult | None = None) -> None:
        self._available = available
        self._result = result or OcrResult(
            text="Question 4. Solve for x in the equation two x plus five equals thirteen.",
            mean_confidence=0.91,
            engine="fake",
        )
        self.pages_read = 0

    @property
    def available(self) -> bool:
        return self._available

    async def read(self, image_png: bytes) -> OcrResult:
        self.pages_read += 1
        return self._result


VISION_CATALOG = {
    ModelAlias.VISION: ModelCatalogEntry(
        alias=ModelAlias.VISION,
        provider=Provider.FAKE,
        model_id="fake-vision",
        input_cost_micros_per_1k=3000,
        output_cost_micros_per_1k=15000,
        rate_version="test-v1",
    )
}

ALLOW = ExecutionBudgetDecision(
    outcome=BudgetOutcome.ALLOW_STANDARD,
    reason=BudgetReason.ROUTINE_MODERATE,
    alias=ModelAlias.STANDARD_TUTOR,
)


def digital_pdf(pages: int = 20) -> bytes:
    """A PDF with a real text layer - no OCR should ever be needed."""
    doc = pymupdf.open()
    topics = ["quadratic equations", "photosynthesis", "newton laws", "titration"]
    for i in range(pages):
        page = doc.new_page()
        topic = topics[i % len(topics)]
        page.insert_text((72, 100), f"Chapter {i + 1}: {topic}", fontsize=14)
        page.insert_text((72, 140), f"{i + 1}. Solve the {topic} problem below.", fontsize=11)
        page.insert_text((72, 170), (f"Detailed content about {topic}. " * 6), fontsize=9)
    data: bytes = doc.tobytes()
    doc.close()
    return data


def scanned_pdf() -> bytes:
    """Page 1 digital, page 2 image-only (a 'scan'), page 3 digital."""
    doc = pymupdf.open()
    p1 = doc.new_page()
    p1.insert_text((72, 100), "Chapter one about quadratic equations. " * 8, fontsize=11)
    p2 = doc.new_page()
    buf = io.BytesIO()
    Image.new("RGB", (600, 800), "white").save(buf, "PNG")
    p2.insert_image(pymupdf.Rect(0, 0, 600, 800), stream=buf.getvalue())
    p3 = doc.new_page()
    p3.insert_text((72, 100), "Chapter three about photosynthesis. " * 8, fontsize=11)
    data: bytes = doc.tobytes()
    doc.close()
    return data


def png_bytes(width: int = 400, height: int = 300) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, "PNG")
    return buf.getvalue()


def wav_bytes(seconds: int = 2) -> bytes:
    """Minimal RIFF/WAVE header plus silence."""
    import struct

    rate = 8000
    samples = rate * seconds
    data = b"\x00\x00" * samples
    header = (
        b"RIFF"
        + struct.pack("<I", 36 + len(data))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
        + b"data"
        + struct.pack("<I", len(data))
    )
    return header + data


def blurred_page() -> bytes:
    """A photograph of a page that is soft but recoverable.

    Radius 2 is chosen deliberately: it scores 5.7 on the focus measure, which
    the original code called hopeless, and enhances to a copy Tesseract reads
    at 4/4 words. It is the exact case the thresholds used to get wrong.
    """
    from PIL import ImageDraw, ImageFilter, ImageFont

    img = Image.new("RGB", (1200, 900), "white")
    draw = ImageDraw.Draw(img)
    # A real size, from Pillow's bundled face: the default bitmap font is ~11px
    # and a radius-2 blur erases it, which would make this test assert the
    # opposite of what it means to.
    font = ImageFont.load_default(size=48)
    y = 90
    for line in ("Question 4.", "Solve for x:", "2x + 5 = 13"):
        draw.text((90, y), line, fill=(10, 10, 10), font=font)
        y += 90
    buf = io.BytesIO()
    img.filter(ImageFilter.GaussianBlur(radius=2.0)).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def blobroot():
    with tempfile.TemporaryDirectory() as directory:
        yield Path(directory)


def build_pipeline(
    blobroot: Path,
    *,
    ocr: object | None = None,
    files: dict[str, bytes] | None = None,
    limits: MediaLimits | None = None,
) -> tuple[MediaPipeline, InMemoryMediaSource, RecordingTaskQueue, object]:
    source = InMemoryMediaSource()
    for media_id, payload in (files or {}).items():
        source.add(media_id, payload)
    queue = RecordingTaskQueue()
    engine = ocr if ocr is not None else FakeOCR()
    pipeline = MediaPipeline(
        source=source,
        blobstore=FilesystemBlobStore(root=blobroot),
        ocr=engine,  # type: ignore[arg-type]
        queue=queue,
        limits=limits or MediaLimits(),
    )
    return pipeline, source, queue, engine


async def make_subject(session: AsyncSession) -> Subject:
    subject = Subject(
        external_identity_type="test_phone",
        external_identity_value=f"+91{uuid4().hex[:10]}",
    )
    session.add(subject)
    await session.flush()
    await session.commit()
    return subject


def ref(media_id: str = "media_1", mime: str | None = None) -> MediaRef:
    return MediaRef(provider="test", media_id=media_id, mime_type_hint=mime)


def vision_gateway(text: str = "Transcribed page content from the vision model.") -> tuple:
    provider = FakeModelProvider(default=ScriptedReply(text=text))
    return ModelGateway({Provider.FAKE: provider}, VISION_CATALOG), provider


# --- 1. non-Pro sends PDF -> nothing happens ----------------------------------


async def test_non_pro_pdf_downloads_nothing(session: AsyncSession, blobroot: Path) -> None:
    subject = await make_subject(session)
    pipeline, source, queue, ocr = build_pipeline(blobroot, files={"media_1": digital_pdf()})

    outcome = await pipeline.intake(
        session,
        subject_id=subject.id,
        conversation_id=None,
        ref=ref(),
        message_type=MessageType.PDF,
        brief="solve question 4",
        entitled=False,
    )
    await session.commit()

    assert outcome.rejected is True
    assert outcome.reject_reason is RejectReason.ENTITLEMENT
    assert source.fetch_count == 0, "ineligible student must not trigger a download"
    assert ocr.pages_read == 0
    assert queue.depth == 0
    assert await session.scalar(select(func.count()).select_from(Job)) == 0
    assert MediaState(outcome.media.state) is MediaState.REJECTED


# --- 2. Pro sends PDF, no brief -> WAITING_FOR_BRIEF, zero cost ---------------


async def test_pro_pdf_without_brief_waits_at_zero_cost(
    session: AsyncSession, blobroot: Path
) -> None:
    subject = await make_subject(session)
    pipeline, source, queue, ocr = build_pipeline(blobroot, files={"media_1": digital_pdf()})

    outcome = await pipeline.intake(
        session,
        subject_id=subject.id,
        conversation_id=None,
        ref=ref(),
        message_type=MessageType.PDF,
        brief=None,
        entitled=True,
    )
    await session.commit()

    assert outcome.needs_brief is True
    assert MediaState(outcome.media.state) is MediaState.WAITING_FOR_BRIEF
    assert source.fetch_count == 0, "no brief must mean no download"
    assert ocr.pages_read == 0
    assert queue.depth == 0
    assert await session.scalar(select(func.count()).select_from(Job)) == 0


async def test_brief_arriving_later_unlocks_the_same_media(
    session: AsyncSession, blobroot: Path
) -> None:
    """The held media resumes rather than starting a second pipeline."""
    subject = await make_subject(session)
    pipeline, source, queue, _ = build_pipeline(blobroot, files={"media_1": digital_pdf()})

    first = await pipeline.intake(
        session,
        subject_id=subject.id,
        conversation_id=None,
        ref=ref(),
        message_type=MessageType.PDF,
        brief=None,
        entitled=True,
    )
    await session.commit()
    assert first.needs_brief is True

    second = await pipeline.intake(
        session,
        subject_id=subject.id,
        conversation_id=None,
        ref=ref(),
        message_type=MessageType.PDF,
        brief="explain page 2",
        entitled=True,
    )
    await session.commit()

    assert second.media.id == first.media.id
    assert MediaState(second.media.state) is MediaState.FETCH_QUEUED
    assert second.job_created is True
    assert queue.depth == 1
    assert await session.scalar(select(func.count()).select_from(MediaObject)) == 1


# --- 3. brief names page 13 -> only page 13 -----------------------------------


async def test_brief_targets_the_named_page(session: AsyncSession, blobroot: Path) -> None:
    subject = await make_subject(session)
    pipeline, source, _, ocr = build_pipeline(blobroot, files={"media_1": digital_pdf(20)})

    intake = await pipeline.intake(
        session,
        subject_id=subject.id,
        conversation_id=None,
        ref=ref(),
        message_type=MessageType.PDF,
        brief="explain the graph on page 13",
        entitled=True,
    )
    result = await pipeline.process(session, intake.media, decision=ALLOW)
    await session.commit()

    assert result.state is MediaState.READY_FOR_CAPABILITY
    assert result.extraction is not None
    pages = [p.page_number for p in result.extraction.pages]
    assert pages == [13], "a 20-page PDF must not be read whole for one page"
    assert source.fetch_count == 1
    assert ocr.pages_read == 0
    assert result.vision_pages == 0


# --- 4. digital PDF -> no OCR at all ------------------------------------------


async def test_digital_pdf_never_runs_ocr(session: AsyncSession, blobroot: Path) -> None:
    subject = await make_subject(session)
    pipeline, _, _, ocr = build_pipeline(blobroot, files={"media_1": digital_pdf(5)})

    intake = await pipeline.intake(
        session,
        subject_id=subject.id,
        conversation_id=None,
        ref=ref(),
        message_type=MessageType.PDF,
        brief="summarize this document",
        entitled=True,
    )
    result = await pipeline.process(session, intake.media, decision=ALLOW)
    await session.commit()

    assert ocr.pages_read == 0
    assert result.ocr_pages == 0
    assert result.vision_pages == 0
    assert result.extraction is not None
    assert "quadratic" in result.extraction.text.lower()


# --- 5 & 6. scanned page -> OCR only that page, and no vision if OCR is good --


async def test_scanned_page_ocrs_only_that_page(session: AsyncSession, blobroot: Path) -> None:
    subject = await make_subject(session)
    pipeline, _, _, ocr = build_pipeline(blobroot, files={"media_1": scanned_pdf()})
    gateway, vision = vision_gateway()

    intake = await pipeline.intake(
        session,
        subject_id=subject.id,
        conversation_id=None,
        ref=ref(),
        message_type=MessageType.PDF,
        brief="summarize the whole document",
        entitled=True,
    )
    result = await pipeline.process(session, intake.media, decision=ALLOW, gateway=gateway)
    await session.commit()

    # Three pages, only the image-only one needs OCR.
    assert ocr.pages_read == 1
    assert result.ocr_pages == 1
    # Good OCR means no vision escalation.
    assert result.vision_pages == 0
    assert vision.call_count == 0
    assert result.extraction is not None
    assert result.extraction.escalated_to_vision is False


# --- 7. OCR insufficient -> exactly one targeted vision escalation ------------


async def test_poor_ocr_escalates_once_to_vision(session: AsyncSession, blobroot: Path) -> None:
    subject = await make_subject(session)
    # Symbol soup: the assessment must judge this unusable.
    poor = FakeOCR(result=OcrResult("|| ~~ ,,, ;;; ((( ))) |||", 0.2, "fake"))
    pipeline, _, _, ocr = build_pipeline(blobroot, ocr=poor, files={"media_1": scanned_pdf()})
    gateway, vision = vision_gateway("2x + 5 = 13, solve for x")

    intake = await pipeline.intake(
        session,
        subject_id=subject.id,
        conversation_id=None,
        ref=ref(),
        message_type=MessageType.PDF,
        brief="explain page 2",
        entitled=True,
    )
    result = await pipeline.process(session, intake.media, decision=ALLOW, gateway=gateway)
    await session.commit()

    assert ocr.pages_read == 1, "local OCR must be tried before paying for vision"
    assert vision.call_count == 1, "exactly one targeted vision call"
    assert result.vision_pages == 1
    assert result.extraction is not None
    assert result.extraction.escalated_to_vision is True
    assert result.extraction.escalation_reason == "ocr_output_mostly_symbols"
    assert len(result.calls) == 1


async def test_vision_is_refused_when_budget_forbids_it(
    session: AsyncSession, blobroot: Path
) -> None:
    """A quota-exhausted student gets degraded extraction, not a surprise bill."""
    subject = await make_subject(session)
    poor = FakeOCR(result=OcrResult("|| ~~ ,,,", 0.1, "fake"))
    pipeline, _, _, _ = build_pipeline(blobroot, ocr=poor, files={"media_1": scanned_pdf()})
    gateway, vision = vision_gateway()

    refused = ExecutionBudgetDecision(
        outcome=BudgetOutcome.REJECT_QUOTA, reason=BudgetReason.DAILY_QUOTA_EXHAUSTED
    )
    intake = await pipeline.intake(
        session,
        subject_id=subject.id,
        conversation_id=None,
        ref=ref(),
        message_type=MessageType.PDF,
        brief="explain page 2",
        entitled=True,
    )
    result = await pipeline.process(session, intake.media, decision=refused, gateway=gateway)
    await session.commit()

    assert vision.call_count == 0
    assert result.vision_pages == 0


# --- 8 & 9. images -----------------------------------------------------------


async def test_image_without_brief_costs_nothing(session: AsyncSession, blobroot: Path) -> None:
    subject = await make_subject(session)
    pipeline, source, queue, ocr = build_pipeline(blobroot, files={"media_1": png_bytes()})

    outcome = await pipeline.intake(
        session,
        subject_id=subject.id,
        conversation_id=None,
        ref=ref(),
        message_type=MessageType.IMAGE,
        brief=None,
        entitled=True,
    )
    await session.commit()

    assert outcome.needs_brief is True
    assert source.fetch_count == 0
    assert ocr.pages_read == 0
    assert queue.depth == 0


async def test_image_with_brief_runs_the_pipeline(session: AsyncSession, blobroot: Path) -> None:
    subject = await make_subject(session)
    pipeline, source, queue, _ = build_pipeline(blobroot, files={"media_1": png_bytes()})

    intake = await pipeline.intake(
        session,
        subject_id=subject.id,
        conversation_id=None,
        ref=ref(),
        message_type=MessageType.IMAGE,
        brief="solve Q4 in this photo",
        entitled=True,
    )
    result = await pipeline.process(session, intake.media, decision=ALLOW)
    await session.commit()

    assert intake.job_created is True
    assert queue.depth == 1
    assert source.fetch_count == 1
    assert result.state is MediaState.READY_FOR_CAPABILITY
    assert intake.media.sha256 is not None
    assert intake.media.blob_key.startswith(f"media/{subject.id}/")


# --- 10. duplicate media event -> no duplicate job ---------------------------


async def test_duplicate_media_event_creates_one_job(session: AsyncSession, blobroot: Path) -> None:
    subject = await make_subject(session)
    pipeline, _, queue, _ = build_pipeline(blobroot, files={"media_1": digital_pdf(3)})

    first = await pipeline.intake(
        session,
        subject_id=subject.id,
        conversation_id=None,
        ref=ref(),
        message_type=MessageType.PDF,
        brief="summarize this",
        entitled=True,
    )
    await session.commit()
    second = await pipeline.intake(
        session,
        subject_id=subject.id,
        conversation_id=None,
        ref=ref(),
        message_type=MessageType.PDF,
        brief="summarize this",
        entitled=True,
    )
    await session.commit()

    assert first.job_created is True
    assert second.job_created is False, "a redelivered event must not queue twice"
    assert queue.depth == 1
    assert await session.scalar(select(func.count()).select_from(Job)) == 1
    assert await session.scalar(select(func.count()).select_from(MediaObject)) == 1


# --- 11. cached extraction -> no repeated OCR --------------------------------


async def test_cached_extraction_skips_ocr_the_second_time(
    session: AsyncSession, blobroot: Path
) -> None:
    subject = await make_subject(session)
    # One byte string reused for both sends. PyMuPDF stamps a creation time into
    # the file, so calling scanned_pdf() twice yields different bytes - which
    # would be different content, correctly missing a content-addressed cache.
    pdf = scanned_pdf()
    pipeline, _, _, ocr = build_pipeline(blobroot, files={"media_1": pdf})
    gateway, _ = vision_gateway()

    first = await pipeline.intake(
        session,
        subject_id=subject.id,
        conversation_id=None,
        ref=ref("media_1"),
        message_type=MessageType.PDF,
        brief="explain page 2",
        entitled=True,
    )
    await pipeline.process(session, first.media, decision=ALLOW, gateway=gateway)
    await session.commit()
    assert ocr.pages_read == 1

    # The same file arriving again under a new media id.
    pipeline._source.add("media_2", pdf)  # type: ignore[attr-defined]
    second = await pipeline.intake(
        session,
        subject_id=subject.id,
        conversation_id=None,
        ref=ref("media_2"),
        message_type=MessageType.PDF,
        brief="explain page 2",
        entitled=True,
    )
    result = await pipeline.process(session, second.media, decision=ALLOW, gateway=gateway)
    await session.commit()

    assert ocr.pages_read == 1, "identical content must not be OCR'd twice"
    assert result.cache_hits == 1
    assert result.ocr_pages == 0


async def test_extraction_cache_is_owner_scoped(session: AsyncSession, blobroot: Path) -> None:
    """Identical bytes from two students are two private documents."""
    alice = await make_subject(session)
    bob = await make_subject(session)
    pdf = scanned_pdf()
    pipeline, _, _, ocr = build_pipeline(blobroot, files={"a": pdf, "b": pdf})
    gateway, _ = vision_gateway()

    for subject_id, media_id in ((alice.id, "a"), (bob.id, "b")):
        intake = await pipeline.intake(
            session,
            subject_id=subject_id,
            conversation_id=None,
            ref=ref(media_id),
            message_type=MessageType.PDF,
            brief="explain page 2",
            entitled=True,
        )
        await pipeline.process(session, intake.media, decision=ALLOW, gateway=gateway)
        await session.commit()

    # Two students, two OCR runs - no cross-student cache reuse.
    assert ocr.pages_read == 2
    rows = await session.scalar(select(func.count()).select_from(MediaExtraction))
    assert rows == 2


# --- 12 & 13. rejections before any provider ---------------------------------


async def test_oversized_file_is_rejected_before_any_processing(
    session: AsyncSession, blobroot: Path
) -> None:
    subject = await make_subject(session)
    tiny = MediaLimits(max_pdf_bytes=500)
    pipeline, _, _, ocr = build_pipeline(blobroot, files={"media_1": digital_pdf(10)}, limits=tiny)
    gateway, vision = vision_gateway()

    intake = await pipeline.intake(
        session,
        subject_id=subject.id,
        conversation_id=None,
        ref=ref(),
        message_type=MessageType.PDF,
        brief="summarize this",
        entitled=True,
    )
    result = await pipeline.process(session, intake.media, decision=ALLOW, gateway=gateway)
    await session.commit()

    assert result.state is MediaState.REJECTED
    assert result.reject_reason is RejectReason.TOO_LARGE
    assert ocr.pages_read == 0
    assert vision.call_count == 0


async def test_malformed_pdf_fails_in_a_controlled_way(
    session: AsyncSession, blobroot: Path
) -> None:
    subject = await make_subject(session)
    broken = b"%PDF-1.4\n" + b"garbage" * 200
    pipeline, _, _, ocr = build_pipeline(blobroot, files={"media_1": broken})
    gateway, vision = vision_gateway()

    intake = await pipeline.intake(
        session,
        subject_id=subject.id,
        conversation_id=None,
        ref=ref(),
        message_type=MessageType.PDF,
        brief="summarize this",
        entitled=True,
    )
    result = await pipeline.process(session, intake.media, decision=ALLOW, gateway=gateway)
    await session.commit()

    assert result.state is MediaState.REJECTED
    assert result.reject_reason in {RejectReason.CORRUPT, RejectReason.UNSUPPORTED_MIME}
    assert ocr.pages_read == 0
    assert vision.call_count == 0


async def test_executable_disguised_as_pdf_is_rejected(
    session: AsyncSession, blobroot: Path
) -> None:
    subject = await make_subject(session)
    pipeline, _, _, _ = build_pipeline(blobroot, files={"media_1": b"MZ\x90\x00" + b"\x00" * 500})

    intake = await pipeline.intake(
        session,
        subject_id=subject.id,
        conversation_id=None,
        ref=ref(mime="application/pdf"),
        message_type=MessageType.PDF,
        brief="summarize this",
        entitled=True,
    )
    result = await pipeline.process(session, intake.media, decision=ALLOW)
    await session.commit()

    assert result.state is MediaState.REJECTED
    assert result.reject_reason is RejectReason.EXECUTABLE


# --- state machine ------------------------------------------------------------


async def test_process_refuses_media_that_never_got_a_brief(
    session: AsyncSession, blobroot: Path
) -> None:
    """Calling process() directly cannot bypass the brief gate."""
    subject = await make_subject(session)
    pipeline, source, _, ocr = build_pipeline(blobroot, files={"media_1": digital_pdf(3)})

    intake = await pipeline.intake(
        session,
        subject_id=subject.id,
        conversation_id=None,
        ref=ref(),
        message_type=MessageType.PDF,
        brief=None,
        entitled=True,
    )
    await session.commit()

    result = await pipeline.process(session, intake.media, decision=ALLOW)

    assert result.state is MediaState.WAITING_FOR_BRIEF
    assert source.fetch_count == 0
    assert ocr.pages_read == 0


async def test_ocr_unavailable_still_produces_a_plan(session: AsyncSession, blobroot: Path) -> None:
    """No Tesseract on the host must degrade to vision, not crash."""
    subject = await make_subject(session)
    pipeline, _, _, _ = build_pipeline(
        blobroot, ocr=UnavailableOCRProvider(), files={"media_1": scanned_pdf()}
    )
    gateway, vision = vision_gateway()

    intake = await pipeline.intake(
        session,
        subject_id=subject.id,
        conversation_id=None,
        ref=ref(),
        message_type=MessageType.PDF,
        brief="explain page 2",
        entitled=True,
    )
    result = await pipeline.process(session, intake.media, decision=ALLOW, gateway=gateway)
    await session.commit()

    assert result.state is MediaState.READY_FOR_CAPABILITY
    assert result.ocr_pages == 0
    assert vision.call_count == 1


# --- 14 & 15. voice ----------------------------------------------------------


class CountingTranscriber:
    """Records every transcription so the counter tests can assert zero."""

    def __init__(self, text: str = "Explain photosynthesis please.") -> None:
        self.text = text
        self.calls = 0

    async def transcribe(self, audio: bytes, *, mime_type: str, model_id: str):
        from tutortwin.domain.provider import ModelAlias, ModelCall, Provider
        from tutortwin.media.audio import TranscriptionResult, probe_duration

        self.calls += 1
        return TranscriptionResult(
            text=self.text,
            duration_seconds=probe_duration(audio, mime_type),
            call=ModelCall(
                alias=ModelAlias.TRANSCRIBE,
                provider=Provider.FAKE,
                model_id="fake-whisper",
                text=self.text,
            ),
        )


async def test_non_pro_voice_makes_zero_transcriptions() -> None:
    from tutortwin.media.audio import VoicePipeline

    transcriber = CountingTranscriber()
    voice = VoicePipeline(transcriber=transcriber, limits=MediaLimits())

    result = await voice.transcribe(wav_bytes(3), mime_type="audio/wav", entitled=False)

    assert transcriber.calls == 0, "ineligible student must not be transcribed"
    assert result.call is None
    assert result.text == ""


async def test_eligible_voice_transcribes_exactly_once() -> None:
    from tutortwin.media.audio import VoicePipeline

    transcriber = CountingTranscriber()
    voice = VoicePipeline(transcriber=transcriber, limits=MediaLimits())

    result = await voice.transcribe(wav_bytes(3), mime_type="audio/wav", entitled=True)

    assert transcriber.calls == 1
    assert result.text == "Explain photosynthesis please."
    assert result.call is not None
    assert result.call.alias is ModelAlias.TRANSCRIBE
    # The transcript now re-enters the ordinary text pipeline.
    assert result.duration_seconds == pytest.approx(3.0, abs=0.1)


async def test_overlong_voice_is_rejected_before_transcription() -> None:
    from tutortwin.media.audio import VoicePipeline

    transcriber = CountingTranscriber()
    voice = VoicePipeline(transcriber=transcriber, limits=MediaLimits(max_audio_seconds=2))

    result = await voice.transcribe(wav_bytes(10), mime_type="audio/wav", entitled=True)

    assert transcriber.calls == 0
    assert result.call is None


async def test_oversized_voice_is_rejected_before_transcription() -> None:
    from tutortwin.media.audio import VoicePipeline

    transcriber = CountingTranscriber()
    voice = VoicePipeline(transcriber=transcriber, limits=MediaLimits(max_audio_bytes=100))

    result = await voice.transcribe(wav_bytes(5), mime_type="audio/wav", entitled=True)

    assert transcriber.calls == 0
    assert result.call is None


async def test_voice_is_exempt_from_the_brief_gate(session: AsyncSession, blobroot: Path) -> None:
    """A voice note IS the request, so there is nothing to brief."""
    from tutortwin.media.pipeline import MEDIA_MESSAGE_TYPES

    assert MessageType.AUDIO not in MEDIA_MESSAGE_TYPES
    assert MessageType.PDF in MEDIA_MESSAGE_TYPES
    assert MessageType.IMAGE in MEDIA_MESSAGE_TYPES

    subject = await make_subject(session)
    pipeline, _, queue, _ = build_pipeline(blobroot, files={"media_1": wav_bytes(2)})

    outcome = await pipeline.intake(
        session,
        subject_id=subject.id,
        conversation_id=None,
        ref=ref(),
        message_type=MessageType.AUDIO,
        brief=None,
        entitled=True,
    )
    await session.commit()

    assert outcome.needs_brief is False
    assert outcome.job_created is True
    assert queue.depth == 1


async def test_asking_for_a_clearer_photo_returns_a_photo_and_spends_nothing(
    session: AsyncSession, blobroot
) -> None:
    """ "Make this clearer" is answered with a picture, not with the tutor.

    Two things are asserted together because they are the same decision: the
    student gets the cleaned-up image back, and nothing is billed for reading a
    page they never asked a question about. `enhance` was a complete module with
    no caller at all until this path existed - the feature simply did not run.
    """
    subject = await make_subject(session)
    pipeline, _, _, ocr = build_pipeline(blobroot, files={"media_1": blurred_page()})
    gateway, vision = vision_gateway()

    intake = await pipeline.intake(
        session,
        subject_id=subject.id,
        conversation_id=None,
        ref=ref(),
        message_type=MessageType.IMAGE,
        brief="this is blurry, can you make it clearer",
        entitled=True,
    )
    result = await pipeline.process(session, intake.media, decision=ALLOW, gateway=gateway)
    await session.commit()

    assert result.enhanced_png is not None
    assert result.enhanced_note
    assert result.state is MediaState.READY_FOR_CAPABILITY

    # Nothing read, nothing billed, nothing cached: the student asked for a
    # picture, so no page was extracted to answer a question with.
    assert ocr.pages_read == 0
    assert vision.call_count == 0
    assert result.calls == []
    assert result.extraction is None
    stored = await session.scalar(
        select(func.count())
        .select_from(MediaExtraction)
        .where(MediaExtraction.subject_id == subject.id)
    )
    assert stored == 0


async def test_a_blurry_photo_with_a_question_still_gets_answered(
    session: AsyncSession, blobroot
) -> None:
    """The failure that matters most in the other direction.

    "Solve this, the photo is blurry" mentions the blur but asks for an answer.
    Routing it to the enhancer would send a student who wanted help a photograph
    of their own homework.
    """
    subject = await make_subject(session)
    pipeline, _, _, ocr = build_pipeline(blobroot, files={"media_1": blurred_page()})
    gateway, _ = vision_gateway()

    intake = await pipeline.intake(
        session,
        subject_id=subject.id,
        conversation_id=None,
        ref=ref(),
        message_type=MessageType.IMAGE,
        brief="solve this, the photo is a bit blurry",
        entitled=True,
    )
    result = await pipeline.process(session, intake.media, decision=ALLOW, gateway=gateway)
    await session.commit()

    assert result.enhanced_png is None
    assert result.enhanced_note is None
    assert result.extraction is not None
    assert ocr.pages_read == 1
