"""PDF inspection and the local-first extraction planner.

The planner's whole job is to answer "which pages, by which method" *before*
anything expensive runs, using only local parsing. A 100-page PDF where the
student asked about one equation must produce a plan covering one page.

Method order is always cheapest-first:

    DIGITAL_TEXT  free      the PDF already contains a text layer
    LOCAL_OCR     cheap     scanned page, Tesseract on our own CPU
    VISION        expensive a frontier model, only when local extraction cannot
                            answer - and only for the targeted pages

Page selection is deterministic when the brief names pages ("page 13",
"pages 2-3"). Otherwise it uses local keyword scoring over the digital text -
no embeddings, because embeddings cost money and BM25-style scoring answers
"which page mentions quadratics" perfectly well.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

import pymupdf

from tutortwin.domain.media import (
    ExtractionMethod,
    ExtractionPlan,
    MediaLimits,
    PagePlan,
    RejectReason,
)

PARSER_VERSION = "pymupdf-1.28"

# A page with a real text layer yields far more than this. Below it, the page is
# either blank or a scan, and OCR is the only way to read it.
MIN_USABLE_CHARS = 80

# Ratio of alphanumeric characters. Broken embedded fonts decode to punctuation
# soup that looks like text to a length check but is useless to a model.
MIN_ALNUM_RATIO = 0.35

_PAGE_RANGE = re.compile(r"\bpages?\s*(\d{1,4})\s*(?:-|to|–|—)\s*(\d{1,4})\b", re.I)
_PAGE_SINGLE = re.compile(r"\bp(?:age|g)?\.?\s*(\d{1,4})\b", re.I)
_QUESTION_NUMBER = re.compile(r"\b(?:q(?:uestion)?|ex(?:ercise)?|problem)\s*(\d{1,3})\b", re.I)

_STOPWORDS = frozenset(
    """the a an and or of to in on for with is are was were be been this that these those
    what which who whom how why when where can could would should do does did my your it
    its from at by as into about please help me explain solve show tell give""".split()
)


class PdfError(Exception):
    def __init__(self, reason: RejectReason, detail: str = "") -> None:
        super().__init__(detail or reason.value)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class PageText:
    page_number: int
    text: str

    @property
    def usable(self) -> bool:
        """Does this page have a text layer worth using instead of OCR?"""
        stripped = self.text.strip()
        if len(stripped) < MIN_USABLE_CHARS:
            return False
        alnum = sum(1 for c in stripped if c.isalnum())
        return alnum / len(stripped) >= MIN_ALNUM_RATIO


@dataclass(frozen=True, slots=True)
class PdfInspection:
    page_count: int
    pages: tuple[PageText, ...]
    encrypted: bool = False

    @property
    def digital_page_numbers(self) -> frozenset[int]:
        return frozenset(p.page_number for p in self.pages if p.usable)


def inspect(data: bytes, limits: MediaLimits) -> PdfInspection:
    """Open, count pages, and pull the text layer. Local only, no network.

    Page count is checked *before* text extraction so a 5000-page document is
    refused without parsing all of it.
    """
    try:
        document = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 - any parse failure is a rejection
        raise PdfError(RejectReason.CORRUPT, type(exc).__name__) from exc

    try:
        if document.needs_pass:
            raise PdfError(RejectReason.ENCRYPTED, "password protected")

        page_count = document.page_count
        if page_count < 1:
            raise PdfError(RejectReason.CORRUPT, "no pages")
        if page_count > limits.max_pdf_pages:
            raise PdfError(
                RejectReason.TOO_MANY_PAGES,
                f"{page_count} pages exceeds {limits.max_pdf_pages}",
            )

        pages: list[PageText] = []
        for index in range(page_count):
            try:
                text = document.load_page(index).get_text("text") or ""
            except Exception:  # noqa: BLE001 - one bad page must not kill the doc
                text = ""
            pages.append(PageText(page_number=index + 1, text=text))
    finally:
        document.close()

    return PdfInspection(page_count=page_count, pages=tuple(pages))


def parse_requested_pages(brief: str, page_count: int) -> tuple[int, ...]:
    """Explicit page references in the brief. Deterministic, no model."""
    requested: set[int] = set()

    for start, end in _PAGE_RANGE.findall(brief):
        low, high = int(start), int(end)
        if low > high:
            low, high = high, low
        requested.update(range(low, min(high, page_count) + 1))

    for match in _PAGE_SINGLE.findall(brief):
        page = int(match)
        if 1 <= page <= page_count:
            requested.add(page)

    return tuple(sorted(p for p in requested if 1 <= p <= page_count))


def _tokenize(text: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if token not in _STOPWORDS and len(token) > 1
    ]


def score_pages_by_brief(inspection: PdfInspection, brief: str) -> list[tuple[int, float]]:
    """Local BM25-ish relevance. No embeddings, no provider call.

    Embeddings would cost money to answer "which page mentions quadratics",
    which term overlap already answers well on a document this small.
    """
    query = _tokenize(brief)
    if not query:
        return []

    # Question numbers are high-signal: "solve Q4" should find the page printing
    # "4." even though the digit alone is a weak token.
    boosts = {m for m in _QUESTION_NUMBER.findall(brief)}

    docs = {p.page_number: Counter(_tokenize(p.text)) for p in inspection.pages}
    doc_count = max(len(docs), 1)
    containing: Counter[str] = Counter()
    for counts in docs.values():
        containing.update(set(counts))

    scored: list[tuple[int, float]] = []
    for page_number, counts in docs.items():
        length = sum(counts.values()) or 1
        score = 0.0
        for term in query:
            frequency = counts.get(term, 0)
            if not frequency:
                continue
            idf = math.log(1 + doc_count / (1 + containing[term]))
            score += (frequency / length) * idf * 100
        scored.append((page_number, score))

    # Apply question-number boost against the raw page text, where punctuation
    # survives tokenisation.
    if boosts:
        raw = {p.page_number: p.text for p in inspection.pages}
        boosted: list[tuple[int, float]] = []
        for page_number, score in scored:
            bonus = 0.0
            for boost in boosts:
                if re.search(rf"(?m)^\s*{re.escape(boost)}\s*[.)]", raw.get(page_number, "")):
                    bonus += 5.0
            boosted.append((page_number, score + bonus))
        scored = boosted

    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return [pair for pair in scored if pair[1] > 0]


def plan_extraction(
    inspection: PdfInspection,
    brief: str,
    limits: MediaLimits,
    *,
    ocr_available: bool = True,
) -> ExtractionPlan:
    """Decide which pages to extract and how. Pure, local, no cost incurred.

    The page budget is applied *here*, so executing the plan cannot overspend.
    """
    digital = inspection.digital_page_numbers

    explicit = parse_requested_pages(brief, inspection.page_count)
    if explicit:
        selected = list(explicit)
        targeting = f"brief named page(s) {', '.join(map(str, explicit))}"
    else:
        ranked = score_pages_by_brief(inspection, brief)
        if ranked:
            selected = [page for page, _ in ranked]
            targeting = f"local keyword scoring matched {len(ranked)} page(s)"
        elif inspection.page_count <= limits.max_pages_processed:
            # Short document, no signal: reading all of it is within budget.
            selected = [p.page_number for p in inspection.pages]
            targeting = "short document, no targeting signal"
        else:
            selected = [p.page_number for p in inspection.pages[: limits.max_pages_processed]]
            targeting = "no targeting signal, first pages only"

    selected = selected[: limits.max_pages_processed]

    plans: list[PagePlan] = []
    ocr_used = 0
    for page_number in selected:
        if page_number in digital:
            plans.append(
                PagePlan(
                    page_number=page_number,
                    method=ExtractionMethod.DIGITAL_TEXT,
                    reason="page has a usable text layer",
                )
            )
        elif ocr_available and ocr_used < limits.max_ocr_pages:
            ocr_used += 1
            plans.append(
                PagePlan(
                    page_number=page_number,
                    method=ExtractionMethod.LOCAL_OCR,
                    reason="no text layer; local OCR before any vision model",
                )
            )
        else:
            # OCR unavailable or budget spent. Vision is the last resort and is
            # still capped by max_vision_pages at execution time.
            plans.append(
                PagePlan(
                    page_number=page_number,
                    method=ExtractionMethod.VISION,
                    reason=(
                        "no text layer and local OCR unavailable"
                        if not ocr_available
                        else "no text layer and OCR page budget exhausted"
                    ),
                )
            )

    return ExtractionPlan(
        pages=tuple(plans),
        total_pages=inspection.page_count,
        targeting_reason=targeting,
    )


def render_page_png(data: bytes, page_number: int, *, dpi: int = 200) -> bytes:
    """Rasterize one page for OCR or vision. Only the requested page is rendered."""
    document = pymupdf.open(stream=data, filetype="pdf")
    try:
        page = document.load_page(page_number - 1)
        pixmap = page.get_pixmap(dpi=dpi)
        png: bytes = pixmap.tobytes("png")
        return png
    finally:
        document.close()
