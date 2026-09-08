"""Reading a photo of a homework page.

The commonest thing a student sends, and until the image path existed the one
that silently produced nothing: the file was fetched, validated, stored and
marked ready, with no text for the tutor to answer from.

The rule under test is the same as the PDF path's - free local OCR first, a paid
vision model only when OCR cannot be trusted - with one difference. A PDF page
falls back to its digital text layer when Tesseract is missing; a photo has no
such layer, so no OCR engine means vision or nothing.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any

import pytest
from PIL import Image

from tutortwin.domain.budget import BudgetOutcome, BudgetReason, ExecutionBudgetDecision
from tutortwin.domain.media import DEFAULT_LIMITS, ExtractionMethod
from tutortwin.domain.provider import (
    ErrorCategory,
    ModelAlias,
    ModelCall,
    ModelRequest,
    Provider,
    StopReason,
)
from tutortwin.media.extractor import IMAGE_PARSER_VERSION, ImageExtractor
from tutortwin.media.ocr import OcrResult


def photo(width: int = 800, height: int = 600) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="JPEG")
    return buffer.getvalue()


@dataclass(slots=True)
class StubOCR:
    """An OCR engine whose answer the test chooses."""

    result: OcrResult
    is_available: bool = True
    reads: int = 0

    @property
    def available(self) -> bool:
        return self.is_available

    async def read(self, image_png: bytes) -> OcrResult:
        self.reads += 1
        return self.result


@dataclass(slots=True)
class StubGatewayResult:
    call: ModelCall | None
    attempts: list[ModelCall] = field(default_factory=list)


@dataclass(slots=True)
class StubGateway:
    """Records what would have been sent to a vision model."""

    text: str = "Q3. Solve for x: 2x + 5 = 13"
    requests: list[ModelRequest] = field(default_factory=list)

    async def invoke(self, request: ModelRequest, **kwargs: Any) -> StubGatewayResult:
        self.requests.append(request)
        call = ModelCall(
            alias=request.alias,
            provider=Provider.FAKE,
            model_id="stub-vision",
            text=self.text,
            input_tokens=100,
            output_tokens=50,
            cached_tokens=0,
            latency_ms=10,
            provider_request_id=None,
            stop_reason=StopReason.END_TURN,
            error_category=ErrorCategory.NONE,
            error_message=None,
            estimated_cost_micros=1000,
            rate_version="test",
            attempt=1,
        )
        return StubGatewayResult(call=call, attempts=[call])


def allowed() -> ExecutionBudgetDecision:
    return ExecutionBudgetDecision(
        outcome=BudgetOutcome.ALLOW_STANDARD,
        reason=BudgetReason.ROUTINE_MODERATE,
        alias=ModelAlias.VISION,
    )


def good_ocr(text: str = "Question 3. Name the parts of a plant cell.") -> OcrResult:
    return OcrResult(text=text, mean_confidence=0.92, engine="tesseract-5")


async def run(
    ocr: StubOCR,
    gateway: StubGateway | None,
    *,
    brief: str = "solve question 3",
    decision: ExecutionBudgetDecision | None = None,
) -> Any:
    return await ImageExtractor(
        ocr=ocr,
        gateway=gateway,  # type: ignore[arg-type]
        limits=DEFAULT_LIMITS,
    ).run(
        data=photo(),
        mime_type="image/jpeg",
        brief=brief,
        decision=decision if decision is not None else allowed(),
    )


class TestFreeFirst:
    @pytest.mark.asyncio
    async def test_confident_ocr_is_used_and_nothing_is_paid_for(self) -> None:
        """The whole cost argument: a clean printed page costs nothing to read."""
        ocr = StubOCR(result=good_ocr())
        gateway = StubGateway()

        outcome = await run(ocr, gateway, brief="what is this about")

        assert outcome.vision_pages == 0
        assert gateway.requests == []
        assert outcome.calls == []
        assert outcome.result.pages[0].method is ExtractionMethod.LOCAL_OCR
        assert "plant cell" in outcome.result.pages[0].text

    @pytest.mark.asyncio
    async def test_the_image_is_read_before_any_model_is_considered(self) -> None:
        ocr = StubOCR(result=good_ocr())
        await run(ocr, StubGateway(), brief="explain")
        assert ocr.reads == 1


class TestEscalation:
    @pytest.mark.asyncio
    async def test_unusable_ocr_escalates_to_vision(self) -> None:
        """Tesseract on a handwritten page returns punctuation soup. Handing
        that to a tutor produces a confident answer to the wrong question."""
        ocr = StubOCR(
            result=OcrResult(
                text="~~ |[ 2x +5 =: |3 ?? //", mean_confidence=0.21, engine="tesseract-5"
            )
        )
        gateway = StubGateway()

        outcome = await run(ocr, gateway)

        assert outcome.vision_pages == 1
        assert outcome.result.escalated_to_vision
        assert outcome.result.escalation_reason
        assert outcome.result.pages[0].method is ExtractionMethod.VISION
        assert "2x + 5" in outcome.result.pages[0].text

    @pytest.mark.asyncio
    async def test_no_ocr_engine_still_reads_the_photo(self) -> None:
        """The difference from the PDF path. A photo has no text layer to fall
        back to, so a deployment without Tesseract must pay a vision model
        rather than return an empty extraction.
        """
        ocr = StubOCR(result=good_ocr(), is_available=False)
        gateway = StubGateway()

        outcome = await run(ocr, gateway)

        assert ocr.reads == 0
        assert outcome.vision_pages == 1
        assert outcome.result.escalation_reason == "ocr_engine_unavailable"
        assert outcome.result.pages[0].text

    @pytest.mark.asyncio
    async def test_maths_is_escalated_at_a_higher_bar(self) -> None:
        """A misread equation is worse for a student than a slower answer, so
        OCR output good enough for prose is not good enough for algebra."""
        # Long enough to clear the minimum-length gate, so confidence is the
        # only thing separating the two runs below.
        middling = OcrResult(
            text="Solve the equation 2x + 5 = 13 and give the value of x.",
            mean_confidence=0.62,
            engine="tesseract-5",
        )

        prose = await run(StubOCR(result=middling), StubGateway(), brief="what does this say")
        maths = await run(StubOCR(result=middling), StubGateway(), brief="solve this equation")

        assert prose.vision_pages == 0
        assert maths.vision_pages == 1


class TestVisionRequest:
    @pytest.mark.asyncio
    async def test_the_real_mime_type_is_sent_not_a_hard_coded_png(self) -> None:
        """A WhatsApp photo is a JPEG. Vendors reject an image whose declared
        media type does not match its bytes."""
        gateway = StubGateway()
        await run(StubOCR(result=good_ocr(), is_available=False), gateway)

        image = gateway.requests[0].messages[0].images[0]
        assert image.media_type == "image/jpeg"

    @pytest.mark.asyncio
    async def test_the_vision_call_transcribes_rather_than_solves(self) -> None:
        """Extraction and tutoring are separate steps. A vision model that
        answers the question here bypasses the pedagogy layer entirely - the
        student gets a solution where the product promises a hint first."""
        gateway = StubGateway()
        await run(StubOCR(result=good_ocr(), is_available=False), gateway)

        system = gateway.requests[0].system.lower()
        assert "transcribe" in system
        assert "do not solve" in system

    @pytest.mark.asyncio
    async def test_the_vision_alias_is_used_so_the_ceiling_applies(self) -> None:
        gateway = StubGateway()
        await run(StubOCR(result=good_ocr(), is_available=False), gateway)
        assert gateway.requests[0].alias is ModelAlias.VISION


class TestRefusalIsHonest:
    @pytest.mark.asyncio
    async def test_no_gateway_returns_an_empty_extraction_not_a_crash(self) -> None:
        """A deployment with no model key must degrade, not fail. The capability
        layer asks for a clearer photo; it does not answer a question it never
        saw."""
        outcome = await run(StubOCR(result=good_ocr(), is_available=False), None)

        assert outcome.result.pages == ()
        assert outcome.vision_pages == 0
        assert outcome.calls == []

    @pytest.mark.asyncio
    async def test_a_refused_budget_does_not_call_the_model(self) -> None:
        gateway = StubGateway()
        refused = ExecutionBudgetDecision(
            outcome=BudgetOutcome.REJECT_SYSTEM_BUDGET,
            reason=BudgetReason.SYSTEM_BUDGET_EXHAUSTED,
            alias=None,
        )

        outcome = await run(
            StubOCR(result=good_ocr(), is_available=False), gateway, decision=refused
        )

        assert gateway.requests == []
        assert outcome.result.pages == ()

    @pytest.mark.asyncio
    async def test_the_extraction_is_versioned_separately_from_the_pdf_parser(self) -> None:
        """Shared cache, different readers. A change to the PDF parser must not
        invalidate every cached photo, and vice versa."""
        from tutortwin.media.pdf import PARSER_VERSION

        outcome = await run(StubOCR(result=good_ocr()), StubGateway(), brief="explain")
        assert outcome.result.parser_version == IMAGE_PARSER_VERSION
        assert IMAGE_PARSER_VERSION != PARSER_VERSION
