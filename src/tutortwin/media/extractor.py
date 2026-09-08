"""Plan execution: digital text, then OCR, then vision - never out of order.

The planner already bounded which pages run and by which method. This module
executes that plan and handles the one decision the planner cannot make in
advance: whether OCR output turned out good enough, or whether this specific
page needs a vision model after all.

Every escalation is recorded with its reason, so "why did this request call a
vision model" is answerable from the extraction record alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tutortwin.domain.budget import ExecutionBudgetDecision
from tutortwin.domain.media import (
    ExtractionMethod,
    ExtractionPlan,
    ExtractionResult,
    MediaLimits,
    PageExtraction,
)
from tutortwin.domain.provider import (
    ImagePart,
    ModelAlias,
    ModelCall,
    ModelMessage,
    ModelRequest,
)
from tutortwin.media.ocr import OCR_VERSION, OCRProvider, assess
from tutortwin.media.pdf import PARSER_VERSION, PdfInspection, render_page_png
from tutortwin.observability.logging import get_logger
from tutortwin.providers.gateway import ModelGateway

logger = get_logger(__name__)

VISION_PROMPT = (
    "Transcribe the educational content of this page exactly. Preserve "
    "mathematical notation, question numbers and labels. Do not solve anything "
    "and do not add commentary - output only what is on the page."
)


@dataclass(slots=True)
class ExtractionOutcome:
    result: ExtractionResult
    calls: list[ModelCall] = field(default_factory=list)
    """Vision calls made. One usage_ledger row is written per entry."""

    ocr_pages: int = 0
    vision_pages: int = 0
    rendered_pages: int = 0


class PdfExtractor:
    """Executes an ExtractionPlan against PDF bytes."""

    def __init__(
        self,
        *,
        ocr: OCRProvider,
        gateway: ModelGateway | None,
        limits: MediaLimits,
    ) -> None:
        self._ocr = ocr
        self._gateway = gateway
        self._limits = limits

    async def run(
        self,
        *,
        data: bytes,
        inspection: PdfInspection,
        plan: ExtractionPlan,
        brief: str,
        decision: ExecutionBudgetDecision | None,
    ) -> ExtractionOutcome:
        digital_text = {p.page_number: p.text for p in inspection.pages}
        extractions: list[PageExtraction] = []
        outcome = ExtractionOutcome(result=ExtractionResult())
        escalation_reason: str | None = None

        for page_plan in plan.pages:
            page = page_plan.page_number

            if page_plan.method is ExtractionMethod.DIGITAL_TEXT:
                extractions.append(
                    PageExtraction(
                        page_number=page,
                        method=ExtractionMethod.DIGITAL_TEXT,
                        text=digital_text.get(page, ""),
                    )
                )
                continue

            if page_plan.method is ExtractionMethod.LOCAL_OCR and self._ocr.available:
                png = render_page_png(data, page)
                outcome.rendered_pages += 1
                ocr_result = await self._ocr.read(png)
                outcome.ocr_pages += 1

                verdict = assess(ocr_result, expect_math=_looks_mathematical(brief))
                if verdict.usable:
                    extractions.append(
                        PageExtraction(
                            page_number=page,
                            method=ExtractionMethod.LOCAL_OCR,
                            text=ocr_result.text,
                            confidence=ocr_result.mean_confidence,
                        )
                    )
                    continue

                # OCR ran and was not good enough. This is the only path that
                # justifies paying for vision on a page we could already see.
                escalation_reason = verdict.reason
                logger.info("ocr_escalating_to_vision", page=page, reason=verdict.reason)
                extraction = await self._vision_page(data, page, decision, outcome, rendered=png)
                if extraction is not None:
                    extractions.append(extraction)
                continue

            # Planned as VISION, or OCR unavailable.
            escalation_reason = escalation_reason or page_plan.reason
            extraction = await self._vision_page(data, page, decision, outcome)
            if extraction is not None:
                extractions.append(extraction)

        outcome.result = ExtractionResult(
            pages=tuple(extractions),
            parser_version=PARSER_VERSION,
            ocr_version=("tesseract-5" if outcome.ocr_pages else None),
            escalated_to_vision=outcome.vision_pages > 0,
            escalation_reason=escalation_reason if outcome.vision_pages else None,
        )
        return outcome

    async def _vision_page(
        self,
        data: bytes,
        page: int,
        decision: ExecutionBudgetDecision | None,
        outcome: ExtractionOutcome,
        *,
        rendered: bytes | None = None,
    ) -> PageExtraction | None:
        """One targeted vision call, hard-capped by max_vision_pages.

        Returns None rather than raising when vision is unavailable or capped:
        a missing page degrades the answer, it should not fail the request.
        """
        if outcome.vision_pages >= self._limits.max_vision_pages:
            logger.info("vision_page_budget_exhausted", page=page)
            return None
        if self._gateway is None:
            logger.info("vision_unavailable_no_gateway", page=page)
            return None
        if decision is None or not decision.permits_paid_call:
            logger.info("vision_refused_by_budget", page=page)
            return None

        if rendered is None:
            rendered = render_page_png(data, page)
            outcome.rendered_pages += 1

        request = ModelRequest(
            alias=ModelAlias.VISION,
            system=VISION_PROMPT,
            messages=(
                ModelMessage(
                    role="user",
                    content=f"Transcribe page {page}.",
                    images=(ImagePart(data=rendered, media_type="image/png"),),
                ),
            ),
            max_output_tokens=1024,
        )
        result = await self._gateway.invoke(request, max_attempts=1)
        outcome.calls.extend(result.attempts)
        outcome.vision_pages += 1

        if result.call is None:
            return None
        return PageExtraction(
            page_number=page, method=ExtractionMethod.VISION, text=result.call.text
        )


IMAGE_PARSER_VERSION = "image-1"
"""Versions the image path separately from the PDF parser, so a change to one
does not invalidate the other's cached extractions."""


class ImageExtractor:
    """A photo of a homework page. The commonest thing a student sends.

    Same rule as the PDF path - free local OCR first, a paid vision model only
    when the OCR output is not trustworthy - but there are no pages to plan, so
    the whole image is the unit and the decision is made once.

    Unlike the PDF path this escalates when OCR is *absent*, not just when it is
    poor. A deployment without Tesseract must still be able to read a photo; it
    simply pays a vision model to do it.
    """

    def __init__(
        self,
        *,
        ocr: OCRProvider,
        gateway: ModelGateway | None,
        limits: MediaLimits,
    ) -> None:
        self._ocr = ocr
        self._gateway = gateway
        self._limits = limits

    async def run(
        self,
        *,
        data: bytes,
        mime_type: str,
        brief: str,
        decision: ExecutionBudgetDecision | None,
    ) -> ExtractionOutcome:
        outcome = ExtractionOutcome(result=ExtractionResult())
        expect_math = _looks_mathematical(brief)

        if self._ocr.available:
            ocr_result = await self._ocr.read(data)
            outcome.ocr_pages = 1
            verdict = assess(ocr_result, expect_math=expect_math)
            if verdict.usable:
                outcome.result = ExtractionResult(
                    pages=(
                        PageExtraction(
                            page_number=1,
                            method=ExtractionMethod.LOCAL_OCR,
                            text=ocr_result.text,
                            confidence=ocr_result.mean_confidence,
                        ),
                    ),
                    parser_version=IMAGE_PARSER_VERSION,
                    ocr_version=OCR_VERSION,
                )
                logger.info(
                    "image_read_by_ocr",
                    confidence=ocr_result.mean_confidence,
                    chars=len(ocr_result.text),
                )
                return outcome
            reason = verdict.reason
        else:
            reason = "ocr_engine_unavailable"

        logger.info("image_escalating_to_vision", reason=reason)
        text = await self._transcribe(data, mime_type, decision, outcome)

        # Nothing readable and nothing paid for. An empty extraction is the
        # honest result: the capability layer asks the student for a clearer
        # photo rather than answering a question it never saw.
        pages = (
            (PageExtraction(page_number=1, method=ExtractionMethod.VISION, text=text),)
            if text is not None
            else ()
        )
        outcome.result = ExtractionResult(
            pages=pages,
            parser_version=IMAGE_PARSER_VERSION,
            ocr_version=OCR_VERSION if outcome.ocr_pages else None,
            escalated_to_vision=outcome.vision_pages > 0,
            escalation_reason=reason if outcome.vision_pages else None,
        )
        return outcome

    async def _transcribe(
        self,
        data: bytes,
        mime_type: str,
        decision: ExecutionBudgetDecision | None,
        outcome: ExtractionOutcome,
    ) -> str | None:
        if self._gateway is None:
            logger.info("vision_unavailable_no_gateway")
            return None
        if decision is None or not decision.permits_paid_call:
            logger.info("vision_refused_by_budget")
            return None

        request = ModelRequest(
            alias=ModelAlias.VISION,
            system=VISION_PROMPT,
            messages=(
                ModelMessage(
                    role="user",
                    content="Transcribe this image.",
                    # The real MIME, not a hard-coded PNG: vendors reject an
                    # image whose declared type does not match its bytes, and a
                    # WhatsApp photo is a JPEG.
                    images=(ImagePart(data=data, media_type=mime_type),),
                ),
            ),
            max_output_tokens=1024,
        )
        result = await self._gateway.invoke(request, max_attempts=1)
        outcome.calls.extend(result.attempts)
        outcome.vision_pages = 1
        return result.call.text if result.call is not None else None


def _looks_mathematical(brief: str) -> bool:
    lowered = brief.lower()
    return any(
        term in lowered
        for term in (
            "solve",
            "equation",
            "integral",
            "derivative",
            "calculate",
            "algebra",
            "formula",
            "prove",
        )
    )
