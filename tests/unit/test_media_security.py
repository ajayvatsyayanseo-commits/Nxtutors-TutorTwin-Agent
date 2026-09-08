"""File validation, filename sanitisation, OCR assessment and the state machine.

Pure logic - no database, no network. These are the checks that stand between a
hostile upload and the expensive pipeline.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from tutortwin.domain.media import (
    DEFAULT_LIMITS,
    ZERO_COST_STATES,
    InvalidTransition,
    MediaKind,
    MediaLimits,
    MediaState,
    RejectReason,
    assert_transition,
    can_transition,
)
from tutortwin.media.audio import check_audio, probe_duration
from tutortwin.media.ocr import OcrResult, UnavailableOCRProvider, assess
from tutortwin.media.validation import (
    sanitize_filename,
    sniff_mime,
    validate,
    validate_image_dimensions,
)


def png(width: int = 50, height: int = 50) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, "PNG")
    return buf.getvalue()


def wav(seconds: int = 2, rate: int = 8000) -> bytes:
    import struct

    data = b"\x00\x00" * (rate * seconds)
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(data))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
        + b"data"
        + struct.pack("<I", len(data))
        + data
    )


# --- the cost invariant, as a property of the state machine -------------------


def test_fetching_is_unreachable_without_a_brief() -> None:
    """The invariant is structural: no edge leads to fetching without a brief."""
    assert can_transition(MediaState.RECEIVED_REFERENCE, MediaState.FETCH_QUEUED) is False
    assert can_transition(MediaState.ENTITLEMENT_CHECKED, MediaState.FETCH_QUEUED) is False
    assert can_transition(MediaState.WAITING_FOR_BRIEF, MediaState.FETCH_QUEUED) is False
    # Only after the brief.
    assert can_transition(MediaState.BRIEF_RECEIVED, MediaState.FETCH_QUEUED) is True


def test_zero_cost_states_never_involve_a_fetch() -> None:
    assert MediaState.FETCHED not in ZERO_COST_STATES
    assert MediaState.EXTRACTING not in ZERO_COST_STATES
    assert MediaState.WAITING_FOR_BRIEF in ZERO_COST_STATES


def test_transitions_are_idempotent() -> None:
    """A retried job re-applying its own transition must be harmless."""
    for state in MediaState:
        assert can_transition(state, state) is True


def test_terminal_states_are_terminal() -> None:
    for terminal in (MediaState.REJECTED, MediaState.FAILED, MediaState.EXPIRED):
        assert can_transition(terminal, MediaState.FETCH_QUEUED) is False
        assert can_transition(terminal, MediaState.EXTRACTING) is False


def test_invalid_transition_raises() -> None:
    with pytest.raises(InvalidTransition):
        assert_transition(MediaState.WAITING_FOR_BRIEF, MediaState.EXTRACTING)


# --- filename sanitisation ----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        ("C:\\Windows\\system32\\evil.exe", "evil.exe"),
        ("/absolute/path/homework.pdf", "homework.pdf"),
        ("normal homework.pdf", "normal_homework.pdf"),
        ("  ..hidden", "hidden"),
        ("...", "upload"),
        ("..", "upload"),
        (".env", "env"),
        (None, "upload"),
        ("", "upload"),
    ],
)
def test_filename_sanitisation(raw: str | None, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_sanitized_filename_never_contains_a_separator() -> None:
    for raw in ("a/b/c.pdf", "a\\b\\c.pdf", "..\\..\\x", "/etc/shadow"):
        cleaned = sanitize_filename(raw)
        assert "/" not in cleaned
        assert "\\" not in cleaned
        assert not cleaned.startswith(".")


def test_long_filename_is_bounded() -> None:
    assert len(sanitize_filename("x" * 5000)) <= 120


# --- validation ---------------------------------------------------------------


def test_real_png_is_accepted() -> None:
    result = validate(png(), limits=DEFAULT_LIMITS)
    assert result.ok is True
    assert result.kind is MediaKind.IMAGE
    assert result.mime == "image/png"


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (b"MZ\x90\x00" + b"\x00" * 100, RejectReason.EXECUTABLE),
        (b"\x7fELF\x02\x01" + b"\x00" * 100, RejectReason.EXECUTABLE),
        (b"#!/bin/sh\nrm -rf /", RejectReason.EXECUTABLE),
        (b"PK\x03\x04" + b"\x00" * 100, RejectReason.ARCHIVE),
        (b"\x1f\x8b\x08" + b"\x00" * 100, RejectReason.ARCHIVE),
        (b"Rar!\x1a\x07" + b"\x00" * 100, RejectReason.ARCHIVE),
        (b"", RejectReason.CORRUPT),
        (b"just some text" * 20, RejectReason.UNSUPPORTED_MIME),
    ],
)
def test_dangerous_content_is_rejected(data: bytes, reason: RejectReason) -> None:
    result = validate(data, limits=DEFAULT_LIMITS)
    assert result.ok is False
    assert result.reason is reason


def test_extension_cannot_disguise_an_executable() -> None:
    """MIME comes from magic bytes; the claimed type is attacker-controlled."""
    result = validate(
        b"MZ\x90\x00" + b"\x00" * 200,
        limits=DEFAULT_LIMITS,
        declared_mime="application/pdf",
    )
    assert result.ok is False
    assert result.reason is RejectReason.EXECUTABLE


def test_category_mismatch_is_rejected() -> None:
    result = validate(png(), limits=DEFAULT_LIMITS, declared_mime="audio/mpeg")
    assert result.ok is False
    assert result.reason is RejectReason.MIME_MISMATCH


def test_sloppy_subtype_is_tolerated() -> None:
    """Clients send imprecise types; only a category lie is fatal."""
    result = validate(png(), limits=DEFAULT_LIMITS, declared_mime="image/jpeg")
    assert result.ok is True


def test_oversized_file_is_rejected() -> None:
    result = validate(png(200, 200), limits=MediaLimits(max_image_bytes=10))
    assert result.ok is False
    assert result.reason is RejectReason.TOO_LARGE


def test_sniff_returns_none_for_unknown_content() -> None:
    assert sniff_mime(b"nonsense bytes here") is None


# --- decompression bomb -------------------------------------------------------


def test_normal_image_dimensions_pass() -> None:
    assert validate_image_dimensions(png(400, 300), DEFAULT_LIMITS).ok is True


def test_oversized_dimension_is_rejected() -> None:
    result = validate_image_dimensions(png(20_000, 20), DEFAULT_LIMITS)
    assert result.ok is False
    assert result.reason is RejectReason.DIMENSIONS


def test_pixel_bomb_is_rejected() -> None:
    """A modest-dimension image can still decode to an enormous pixel count."""
    limits = MediaLimits(max_image_pixels=1000, max_image_dimension=99_999)
    result = validate_image_dimensions(png(200, 200), limits)
    assert result.ok is False
    assert result.reason is RejectReason.DECOMPRESSION_BOMB


def test_corrupt_image_is_rejected_not_raised() -> None:
    result = validate_image_dimensions(b"\x89PNG\r\n\x1a\ncorrupt", DEFAULT_LIMITS)
    assert result.ok is False
    assert result.reason is RejectReason.CORRUPT


# --- OCR assessment -----------------------------------------------------------


def test_clean_ocr_output_is_usable() -> None:
    result = OcrResult("The mitochondrion produces ATP through cellular respiration.", 0.93, "t")
    assert assess(result).usable is True


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        (OcrResult("", None, "t"), "ocr_returned_nothing"),
        (OcrResult("abc", 0.9, "t"), "ocr_output_too_short"),
        (
            OcrResult("|| -- ~~ ,,, ;;; ((( ))) ||| ~~~ ,,,,,", 0.8, "t"),
            "ocr_output_mostly_symbols",
        ),
        (
            OcrResult("Some words that were read from the page poorly", 0.30, "t"),
            "ocr_confidence_below_threshold",
        ),
    ],
)
def test_unusable_ocr_is_named_honestly(result: OcrResult, reason: str) -> None:
    verdict = assess(result)
    assert verdict.usable is False
    assert verdict.reason == reason


def test_maths_at_middling_confidence_escalates() -> None:
    """Tesseract fails quietly on notation; a wrong equation is worse than a delay."""
    text = "the integral of x^2 dx equals x^3 over three plus C"
    assert assess(OcrResult(text, 0.60, "t")).usable is False
    assert assess(OcrResult(text, 0.88, "t")).usable is True


def test_expect_math_flag_raises_the_bar() -> None:
    text = "the answer is twenty seven point five for this question here"
    assert assess(OcrResult(text, 0.60, "t"), expect_math=True).usable is False
    assert assess(OcrResult(text, 0.60, "t"), expect_math=False).usable is True


async def test_unavailable_ocr_reports_itself_honestly() -> None:
    provider = UnavailableOCRProvider()
    assert provider.available is False
    result = await provider.read(png())
    assert result.text == ""
    assert result.engine == "unavailable"


# --- audio --------------------------------------------------------------------


def test_wav_duration_is_measured_from_the_header() -> None:
    assert probe_duration(wav(3), "audio/wav") == pytest.approx(3.0, abs=0.05)


def test_compressed_audio_duration_is_unknown() -> None:
    """Size caps do the bounding; decoding would need a codec dependency."""
    assert probe_duration(b"\xff\xfb\x90\x00" * 100, "audio/mpeg") is None


def test_audio_within_limits_passes() -> None:
    assert check_audio(wav(2), mime_type="audio/wav", limits=DEFAULT_LIMITS).ok is True


def test_overlong_audio_is_rejected() -> None:
    result = check_audio(wav(10), mime_type="audio/wav", limits=MediaLimits(max_audio_seconds=5))
    assert result.ok is False
    assert result.reason is RejectReason.TOO_LONG


def test_oversized_audio_is_rejected() -> None:
    result = check_audio(wav(5), mime_type="audio/wav", limits=MediaLimits(max_audio_bytes=100))
    assert result.ok is False
    assert result.reason is RejectReason.TOO_LARGE
