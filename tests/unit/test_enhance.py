"""Image enhancement, calibrated against what OCR can actually read.

The thresholds in `enhance` are the whole module: get them wrong in one
direction and every clean page is reported as broken, wrong in the other and a
student is told their perfectly recoverable photo is beyond rescue. So these
tests pin the measurements rather than the implementation - they render a page,
blur it by a known radius, and assert the verdict matches what Tesseract could
read from the enhanced copy when the thresholds were set.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from tutortwin.media import enhance

LINES = ("Question 4.", "Solve for x:", "2x + 5 = 13", "Show your working.")


def page(blur: float = 0.0) -> bytes:
    """A rendered homework page, optionally blurred by a known radius."""
    img = Image.new("RGB", (1200, 900), "white")
    draw = ImageDraw.Draw(img)
    # Pillow's bundled face at a real size. A system font path would make these
    # thresholds pass on one machine and fail on another, and the default
    # bitmap face is ~11px, which any blur destroys outright.
    font = ImageFont.load_default(size=48)
    y = 90
    for line in LINES:
        draw.text((90, y), line, fill=(10, 10, 10), font=font)
        y += 90
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(radius=blur))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# radius -> is the enhanced copy readable? Ground truth is Tesseract reading the
# enhanced image back; the full table with its OCR scores is in the
# `HOPELESS_THRESHOLD` docstring. Radius 4 is included deliberately: it is the
# closest case to the threshold, at enhanced sharpness 21.2 against a cutoff of
# 18, and it is the one a careless retune would break first.
@pytest.mark.parametrize(
    ("radius", "readable"),
    [
        (0.0, True),
        (1.0, True),
        (2.0, True),
        (3.0, True),
        (4.0, True),
        (5.0, False),
        (8.0, False),
    ],
)
def test_rescued_matches_what_ocr_could_read(radius: float, readable: bool) -> None:
    assert enhance.enhance(page(radius)).rescued is readable


def test_a_recoverable_photo_is_not_written_off() -> None:
    """The regression this replaced.

    `is_hopeless` was read off the ORIGINAL image, where a radius-2 blur scores
    5.7 - far below any threshold. Enhancement takes it to 199.8 and OCR reads
    it at 4/4 words, but the student was told to retake the photo and got no
    picture back.
    """
    result = enhance.enhance(page(2.0))

    assert result.before.is_severely_blurred is True
    assert result.rescued is True
    assert "too blurred for me to rescue" not in enhance.describe(result)


def test_a_destroyed_photo_is_not_promised_a_rescue() -> None:
    """And the failure in the other direction stays fixed.

    `improved` cannot be the test: sharpening multiplies a near-zero baseline,
    so a radius-8 blur "improves" by a large factor while remaining unreadable.
    """
    result = enhance.enhance(page(8.0))

    assert result.improved is True
    assert result.rescued is False
    assert "too blurred for me to rescue" in enhance.describe(result)


def test_a_clean_page_is_not_reported_as_faulty() -> None:
    """Document thresholds, not photographic ones.

    Most of a page is paper, so a healthy scan reads bright and flat. The
    photographic defaults this module started with called every one of them
    washed out.
    """
    assessment = enhance.assess(page())

    assert assessment.is_severely_blurred is False
    assert "dark" not in assessment.problems
    assert "low resolution" not in assessment.problems


@pytest.mark.parametrize(
    "brief",
    [
        "make this clearer",
        "photo is blurry",
        "can you sharpen this",
        "I can't read this",
        "better quality please",
    ],
)
def test_asking_for_a_clearer_picture_is_recognised(brief: str) -> None:
    assert enhance.wants_enhancement(brief) is True


@pytest.mark.parametrize(
    "brief",
    [
        # The important half: a question that merely mentions the photo quality
        # is still a question. Getting this wrong sends a student who asked for
        # help a photograph instead of an answer.
        "solve this, the photo is blurry",
        "explain question 4, sorry it's unclear",
        "what is the answer, cant read my own writing",
        "answer this",
        "solve question 4",
        None,
        "",
    ],
)
def test_a_question_is_not_mistaken_for_an_enhancement_request(brief: str | None) -> None:
    assert enhance.wants_enhancement(brief) is False
