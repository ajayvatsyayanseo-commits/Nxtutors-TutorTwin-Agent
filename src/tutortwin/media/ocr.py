"""Local OCR, and the rule for when it is not good enough.

Open-source first: Tesseract runs on our own CPU and costs nothing per page, so
it is always tried before a vision model. But Tesseract is genuinely unreliable
for handwriting and mathematical notation, and pretending otherwise would ship
confident nonsense to students.

So `assess` decides honestly whether the OCR output is usable, and the caller
escalates a *targeted crop* to a vision model when it is not - recording why.
"""

from __future__ import annotations

import io
import re
import shutil
from dataclasses import dataclass
from typing import Protocol

from tutortwin.observability.logging import get_logger

logger = get_logger(__name__)

OCR_VERSION = "tesseract-5"

PAGE_SEGMENTATION_MODES: tuple[int, ...] = (6, 4, 3)
"""Tried in order, most-likely-first, and the most confident result wins.

6 = one uniform block, which is what a photo of a single question is.
4 = a single column of varied sizes, which is what a worked solution is.
3 = fully automatic with column detection, Tesseract's default and the one that
    mis-splits a photographed page into imaginary columns.
"""

# Below this mean word confidence the output is not trustworthy enough to hand
# to a tutor as fact.
MIN_MEAN_CONFIDENCE = 0.55

MIN_USABLE_CHARS = 25

# Tesseract mangles mathematical notation into punctuation runs. A high ratio of
# non-alphanumeric characters is the tell.
MAX_GARBAGE_RATIO = 0.45

# Notation Tesseract is known to read badly. Their presence in a *low*-confidence
# result is what justifies escalating rather than retrying.
_MATH_HINTS = re.compile(r"[∫∑√±≤≥≠∞π]|\\frac|\^|_\{|\bdx\b|\bdy\b")


@dataclass(frozen=True, slots=True)
class OcrResult:
    text: str
    mean_confidence: float | None
    engine: str
    """Empty text with a None confidence means the engine did not run."""


@dataclass(frozen=True, slots=True)
class OcrAssessment:
    usable: bool
    reason: str


class OCRProvider(Protocol):
    @property
    def available(self) -> bool:
        """False when the engine is not installed. Callers must plan for this."""
        ...

    async def read(self, image_png: bytes) -> OcrResult: ...


def assess(result: OcrResult, *, expect_math: bool = False) -> OcrAssessment:
    """Is this OCR output good enough to use without a vision model?

    Deliberately conservative for maths: a wrong equation is worse for a student
    than a slightly slower answer.
    """
    text = result.text.strip()
    if not text:
        return OcrAssessment(False, "ocr_returned_nothing")
    if len(text) < MIN_USABLE_CHARS:
        return OcrAssessment(False, "ocr_output_too_short")

    non_alnum = sum(1 for c in text if not c.isalnum() and not c.isspace())
    if non_alnum / len(text) > MAX_GARBAGE_RATIO:
        return OcrAssessment(False, "ocr_output_mostly_symbols")

    if result.mean_confidence is not None and result.mean_confidence < MIN_MEAN_CONFIDENCE:
        return OcrAssessment(False, "ocr_confidence_below_threshold")

    if (expect_math or _MATH_HINTS.search(text)) and (
        result.mean_confidence is not None and result.mean_confidence < 0.75
    ):
        # Maths is where Tesseract fails quietly rather than loudly.
        return OcrAssessment(False, "math_notation_needs_visual_verification")

    return OcrAssessment(True, "ocr_output_usable")


def _collect(data: dict[str, list[object]]) -> OcrResult:
    """Turn one Tesseract pass into words and a mean confidence."""
    words: list[str] = []
    confidences: list[float] = []
    for text, confidence in zip(data["text"], data["conf"], strict=False):
        token = str(text or "").strip()
        if not token:
            continue
        words.append(token)
        try:
            value = float(confidence)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if value >= 0:  # Tesseract uses -1 for "no confidence"
            confidences.append(value / 100.0)

    mean = sum(confidences) / len(confidences) if confidences else None
    return OcrResult(text=" ".join(words), mean_confidence=mean, engine=OCR_VERSION)


def _better(candidate: OcrResult, incumbent: OcrResult | None) -> bool:
    """Is this pass a better read than the best so far?

    Confidence alone picks the wrong winner: a mode that finds three characters
    it is completely sure of scores 0.99 and loses the question. So a pass that
    reads substantially more text wins even at somewhat lower confidence, and
    confidence only decides between passes of comparable length.
    """
    if incumbent is None:
        return True

    new_len, old_len = len(candidate.text.strip()), len(incumbent.text.strip())
    if not new_len:
        return False
    if not old_len:
        return True

    new_conf = candidate.mean_confidence or 0.0
    old_conf = incumbent.mean_confidence or 0.0

    # Half again as much text is a different reading of the page, not noise.
    if new_len > old_len * 1.5:
        return True
    if old_len > new_len * 1.5:
        return False
    return new_conf > old_conf


class TesseractOCRProvider:
    """Local Tesseract. Reports honestly when the binary is absent."""

    def __init__(self, *, language: str = "eng", command: str | None = None) -> None:
        self._language = language
        # An explicit path wins over PATH because the Windows installer puts the
        # binary in Program Files and adds nothing to PATH, so a machine with
        # Tesseract installed reports it missing and quietly pays a vision model
        # for every photo.
        self._binary = command or shutil.which("tesseract")

    @property
    def available(self) -> bool:
        return self._binary is not None

    async def read(self, image_png: bytes) -> OcrResult:
        if not self.available:
            return OcrResult(text="", mean_confidence=None, engine="unavailable")

        import asyncio

        return await asyncio.to_thread(self._read_sync, image_png)

    def _read_sync(self, image_png: bytes) -> OcrResult:
        import pytesseract
        from PIL import Image

        # pytesseract shells out to whatever this points at, and its default is
        # the bare name `tesseract` - which fails on any machine where the
        # binary is installed but not on PATH.
        pytesseract.pytesseract.tesseract_cmd = self._binary

        with Image.open(io.BytesIO(image_png)) as image:
            prepared = preprocess(image)

            # Read the page more than once and keep the most confident answer.
            #
            # Tesseract's segmentation mode changes the result far more than any
            # other knob, and no single mode is best for what students send.
            # PSM 3 assumes a full page with columns and mis-splits a photo of
            # one question; PSM 6 assumes one uniform block and reads it
            # cleanly; PSM 4 handles a column of varied text sizes, which is
            # what a worked solution actually looks like.
            #
            # Three passes on an already-decoded image costs tens of
            # milliseconds of local CPU. Escalating to a vision model because
            # the default mode misread the page costs a paid API call, every
            # time. The cheap thing is to try again.
            best: OcrResult | None = None
            for psm in PAGE_SEGMENTATION_MODES:
                try:
                    data = pytesseract.image_to_data(
                        prepared,
                        lang=self._language,
                        config=f"--psm {psm} --oem 1",
                        output_type=pytesseract.Output.DICT,
                    )
                except pytesseract.TesseractError:
                    # A mode the installed build rejects is skipped, not fatal.
                    continue

                candidate = _collect(data)
                if _better(candidate, best):
                    best = candidate

        if best is None:
            return OcrResult(text="", mean_confidence=None, engine=OCR_VERSION)
        return best


def preprocess(image: object) -> object:
    """Grayscale, upscale small text, and increase contrast.

    Tesseract's accuracy is dominated by input quality; these three steps cost
    milliseconds and routinely move a page from unusable to usable, which is a
    vision call avoided.
    """
    from PIL import Image, ImageOps

    assert isinstance(image, Image.Image)
    prepared = ImageOps.exif_transpose(image)
    prepared = prepared.convert("L")

    # Tesseract wants roughly 300 DPI; upscale anything obviously smaller.
    width, height = prepared.size
    if max(width, height) < 1200:
        scale = 1200 / max(width, height)
        prepared = prepared.resize(
            (int(width * scale), int(height * scale)), Image.Resampling.LANCZOS
        )

    return ImageOps.autocontrast(prepared)


class UnavailableOCRProvider:
    """Explicit 'no OCR here'. Used when the deployment has no Tesseract.

    A null object rather than None, so the planner branches on `available`
    instead of every call site checking for None.
    """

    @property
    def available(self) -> bool:
        return False

    async def read(self, image_png: bytes) -> OcrResult:
        return OcrResult(text="", mean_confidence=None, engine="unavailable")
