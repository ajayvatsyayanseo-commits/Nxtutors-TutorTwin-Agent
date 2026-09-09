"""Make a badly-photographed page readable again.

Students photograph homework at night, at an angle, with a hand shadow across
the page. That image is hard for a person to read and hard for OCR, and the
usual outcome is the tutor guessing at a misread question.

**What this can and cannot do, stated plainly.** Enhancement redistributes
contrast and sharpens edges that are already present. It cannot recover detail
the camera never captured: a genuinely out-of-focus photo has lost that
information, and no amount of processing brings it back. What it reliably fixes
is the common case, which is not focus at all - it is a dim, low-contrast,
slightly skewed photo of a white page, where the text is perfectly present and
merely buried.

So `assess` measures the image first and says honestly whether enhancement is
likely to help, and the caller tells the student the truth either way. Promising
a clear picture and returning the same blur is worse than saying "retake it".

Pillow and NumPy only - both already dependencies. OpenCV would add ~60MB to
every container image for filters Pillow already has.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass

from tutortwin.observability.logging import get_logger

logger = get_logger(__name__)

MAX_DIMENSION = 2400
"""Enhancement is O(pixels), and a modern phone photo is 12 megapixels of which
maybe 2 carry the text. Downscaling first bounds both the CPU and the memory,
and improves OCR - Tesseract does not benefit from more pixels than the glyphs
need."""

MIN_DIMENSION = 900
"""Below this, upscale before sharpening: Tesseract wants roughly 300 DPI, and
sharpening a too-small image amplifies noise instead of text."""

SHARP_THRESHOLD = 120.0
"""Variance of the Laplacian, the standard focus measure. Above this a page is
in focus.

It says nothing about whether a softer page can be fixed - measurement says a
page scoring 5.7 enhances to one Tesseract reads perfectly. This decides which
words to use when describing the photo; `HOPELESS_THRESHOLD` decides whether
there is a photo worth sending back."""

HOPELESS_THRESHOLD = 18.0
"""Applied to the sharpness AFTER enhancement, not before.

Measured, not assumed. A page of 48pt text was blurred by a known radius, then
the enhanced copy was read back with Tesseract:

    blur  before   after   OCR of the enhanced copy
    0.0    442.3  1032.3   4/4 words, confidence 0.95
    1.0     39.9   643.1   4/4, 0.95
    2.0      5.7   199.8   4/4, 0.86
    3.0      1.1    54.8   3/4, 0.63
    4.0      0.3    21.2   4/4, 0.73
    5.0      0.2    14.1   0/4, 0.43
    8.0      0.1    13.4   0/4, 0.17

Two things to read from that. First, the `before` column separates nothing:
5.7 is perfectly recoverable and 0.3 is borderline, and both sit far below any
threshold that would admit 39.9. Whether a photo was beyond rescue is only
knowable once it has been rescued or not, which is why this is applied to
`Enhanced.after` and lives on `Enhanced.rescued`.

Second, the boundary is soft, and the table is kept in full rather than tidied
because of it: radius 3 scores WORSE on OCR than radius 4 despite being less
blurred, because sharpening a nearly-flat image produces ringing that helps or
hurts by luck. Readability collapses for good between 21.2 and 14.1, so the
threshold sits between them - with the margin on the conservative side, since
telling a student to retake a photo we could have cleaned is a smaller failure
than handing back an unreadable one under the words "here it is, cleaned up"."""


# Document thresholds, measured rather than assumed. A clean rendered page reads
# brightness 249 / contrast 35; a legibly dim one 111 / 16; an unreadable one
# 62 / 9. Photographic defaults (dark below 90, low contrast below 40) would
# call every healthy document faulty.
DARK_THRESHOLD = 100.0
LOW_CONTRAST_THRESHOLD = 18.0
BLOWN_THRESHOLD = 250.0


@dataclass(frozen=True, slots=True)
class ImageAssessment:
    """What is actually wrong with this photo."""

    sharpness: float
    brightness: float
    contrast: float
    width: int
    height: int

    @property
    def is_blurred(self) -> bool:
        return self.sharpness < SHARP_THRESHOLD

    @property
    def is_severely_blurred(self) -> bool:
        """Badly out of focus - a description of this image, not a prediction.

        Do NOT read this as "cannot be rescued": measurement says a page at
        sharpness 5.9 enhances to fully readable. `Enhanced.rescued` is the
        property that answers that question, and it can only be asked
        afterwards.
        """
        return self.sharpness < HOPELESS_THRESHOLD

    @property
    def is_dim(self) -> bool:
        return self.brightness < DARK_THRESHOLD or self.contrast < LOW_CONTRAST_THRESHOLD

    @property
    def problems(self) -> tuple[str, ...]:
        """Only real faults.

        These thresholds are tuned for DOCUMENTS, not photographs, and the
        difference is large enough that photographic defaults report every
        healthy page as broken. Measured on clean rendered pages: brightness
        ~249 and contrast ~35 are what a perfectly good white page with black
        text looks like, because most of the frame is paper. A photographic
        "washed out above 215, low contrast below 40" flags all of them.
        """
        found: list[str] = []
        if self.is_severely_blurred:
            found.append("very blurred")
        elif self.is_blurred:
            found.append("slightly out of focus")

        if self.brightness < DARK_THRESHOLD:
            found.append("dark")
        elif self.brightness > BLOWN_THRESHOLD and self.contrast < LOW_CONTRAST_THRESHOLD:
            # Bright AND flat together: the page is genuinely blown out and the
            # ink has gone with it. Bright alone is just paper.
            found.append("washed out")

        if self.contrast < LOW_CONTRAST_THRESHOLD:
            found.append("low contrast")
        if max(self.width, self.height) < MIN_DIMENSION:
            found.append("low resolution")
        return tuple(found)


@dataclass(frozen=True, slots=True)
class Enhanced:
    png: bytes
    before: ImageAssessment
    after: ImageAssessment

    @property
    def rescued(self) -> bool:
        """Is the result actually readable?

        The ratio test below cannot answer this: sharpening a destroyed photo
        multiplies a near-zero baseline into a large-looking gain, so `improved`
        is True for a blur radius of 8 that OCR reads at 0/4 words. Absolute
        sharpness after the fact is what separates them.
        """
        return self.after.sharpness >= HOPELESS_THRESHOLD

    @property
    def improved(self) -> bool:
        """Measurably better, not just different.

        A 15% sharpness gain is inside the noise of the measure itself; below
        that the student is being sent a second copy of their own photo.
        """
        return self.after.sharpness > self.before.sharpness * 1.15


def _laplacian_variance(grey: object) -> float:
    """Focus measure: the variance of the second spatial derivative.

    A sharp edge has a large second derivative; a blurred one does not. This is
    the standard measure and it is implemented directly because pulling in
    OpenCV for one 3x3 convolution is not a trade worth making.
    """
    import numpy as np
    from PIL import Image

    assert isinstance(grey, Image.Image)  # noqa: S101 - narrowing
    array = np.asarray(grey, dtype=np.float64)
    if array.ndim != 2 or min(array.shape) < 3:
        return 0.0

    # 4-neighbour Laplacian, computed by slicing rather than convolution.
    centre = array[1:-1, 1:-1]
    lap = array[:-2, 1:-1] + array[2:, 1:-1] + array[1:-1, :-2] + array[1:-1, 2:] - 4.0 * centre
    return float(lap.var())


def assess(image_bytes: bytes) -> ImageAssessment:
    """Measure the photo. Cheap, and it decides whether to bother."""
    import numpy as np
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(image_bytes)) as opened:
        upright = ImageOps.exif_transpose(opened)
        grey = upright.convert("L")
        array = np.asarray(grey, dtype=np.float64)
        return ImageAssessment(
            sharpness=_laplacian_variance(grey),
            brightness=float(array.mean()) if array.size else 0.0,
            contrast=float(array.std()) if array.size else 0.0,
            width=upright.width,
            height=upright.height,
        )


def enhance(image_bytes: bytes) -> Enhanced:
    """Clean up a photograph of a page.

    The order matters and is the order a darkroom would use: straighten the
    orientation, bound the size, lift the exposure, remove speckle, then sharpen
    LAST. Sharpening before denoising amplifies the noise into permanent
    artefacts that look exactly like punctuation to OCR.
    """
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps

    before = assess(image_bytes)

    with Image.open(io.BytesIO(image_bytes)) as opened:
        # Phones record orientation in EXIF rather than rotating the pixels, so
        # a portrait photo arrives sideways to anything that ignores the tag.
        working = ImageOps.exif_transpose(opened).convert("RGB")

        longest = max(working.size)
        if longest > MAX_DIMENSION:
            scale = MAX_DIMENSION / longest
            working = working.resize(
                (int(working.width * scale), int(working.height * scale)),
                Image.Resampling.LANCZOS,
            )
        elif longest < MIN_DIMENSION:
            scale = MIN_DIMENSION / longest
            working = working.resize(
                (int(working.width * scale), int(working.height * scale)),
                Image.Resampling.LANCZOS,
            )

        # Exposure. `autocontrast` with a small cutoff ignores the brightest and
        # darkest 1% before stretching, so a single glare spot or ink blot does
        # not define the whole range and flatten everything between.
        working = ImageOps.autocontrast(working, cutoff=1)

        if before.brightness < DARK_THRESHOLD:
            factor = min(1.9, 128.0 / max(before.brightness, 1.0))
            working = ImageEnhance.Brightness(working).enhance(factor)

        if before.contrast < LOW_CONTRAST_THRESHOLD * 1.6:
            working = ImageEnhance.Contrast(working).enhance(1.35)

        # Denoise before sharpening, and only when the photo is noisy enough to
        # need it - a median filter costs real detail on an already-clean scan.
        if before.sharpness < SHARP_THRESHOLD * 2:
            working = working.filter(ImageFilter.MedianFilter(size=3))

        # Unsharp mask. `percent` is deliberately moderate: over-sharpening
        # creates white halos around letters that OCR reads as spaces, turning
        # a legible word into two illegible ones.
        working = working.filter(ImageFilter.UnsharpMask(radius=2.0, percent=145, threshold=3))

        buffer = io.BytesIO()
        working.save(buffer, format="PNG", optimize=True)

    data = buffer.getvalue()
    after = assess(data)
    logger.info(
        "image_enhanced",
        sharpness_before=round(before.sharpness, 1),
        sharpness_after=round(after.sharpness, 1),
        problems=",".join(before.problems) or "none",
    )
    return Enhanced(png=data, before=before, after=after)


def describe(result: Enhanced) -> str:
    """What to tell the student, honestly.

    Never claims to have fixed focus. A photo that was out of focus is still out
    of focus; what changed is contrast and edge definition.
    """
    if not result.rescued:
        return (
            "That photo is too blurred for me to rescue - the detail is not in the "
            "image to recover. Retake it with the phone flat above the page, in as "
            "much light as you have, and tap the screen on the text to focus."
        )

    problems = result.before.problems
    if not problems and not result.improved:
        return "That photo is already clear - here it is with the contrast lifted a little."

    if not result.improved:
        return (
            "I cleaned that up as much as I can, but it has not improved much. "
            "A retake with more light would help more than any processing."
        )

    was = ", ".join(problems) if problems else "a little soft"
    gain = result.after.sharpness / max(result.before.sharpness, 1.0)
    return (
        f"That photo was {was}. Here it is cleaned up - about {gain:.1f}x sharper, "
        "with the contrast and lighting evened out."
    )


# What a student says when they want the photo cleaned rather than answered.
_CLEAN_INTENT = (
    "clear",
    "clearer",
    "clean",
    "cleaner",
    "sharpen",
    "sharper",
    "enhance",
    "improve",
    "readable",
    "blurry",
    "blurred",
    "blur",
    "not visible",
    "cant read",
    "can't read",
    "cannot read",
    "unclear",
    "fix this photo",
    "fix the photo",
    "better quality",
)


def wants_enhancement(text: str | None) -> bool:
    """Is the student asking for a better picture, or for the answer?

    Deliberately narrow. Getting this wrong in the permissive direction means a
    student who asked a question receives a photograph back instead of an
    answer, which is a far worse failure than not offering to clean an image.
    """
    if not text:
        return False
    lowered = text.lower()
    if not any(phrase in lowered for phrase in _CLEAN_INTENT):
        return False

    # "solve this, the photo is blurry" is a request to solve, not to enhance.
    solving = ("solve", "answer", "explain", "what is", "how do", "calculate", "prove")
    return not any(word in lowered for word in solving)


def sharpness_gain(before: float, after: float) -> float:
    """Ratio, guarded against a zero baseline."""
    if before <= 0:
        return math.inf if after > 0 else 1.0
    return after / before


__all__ = [
    "BLOWN_THRESHOLD",
    "DARK_THRESHOLD",
    "HOPELESS_THRESHOLD",
    "LOW_CONTRAST_THRESHOLD",
    "MAX_DIMENSION",
    "MIN_DIMENSION",
    "SHARP_THRESHOLD",
    "Enhanced",
    "ImageAssessment",
    "assess",
    "describe",
    "enhance",
    "sharpness_gain",
    "wants_enhancement",
]
