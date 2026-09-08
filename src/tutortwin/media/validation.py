"""File validation. Runs on fetched bytes, before any parser or provider.

Every check here answers "is it safe and within budget to open this file", and
all of them are cheap. The expensive pipeline runs only after this passes, so a
hostile or oversized file costs one download and nothing more.

MIME comes from **sniffing the magic bytes**, never from the filename or the
sender's claimed type - both are attacker-controlled.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

import filetype

from tutortwin.domain.media import MediaKind, MediaLimits, RejectReason

# Magic-byte prefixes for formats `filetype` does not cover or that we must
# reject explicitly.
_EXECUTABLE_MAGIC: tuple[bytes, ...] = (
    b"MZ",  # DOS/PE  (.exe, .dll)
    b"\x7fELF",  # ELF
    b"\xca\xfe\xba\xbe",  # Mach-O fat / Java class
    b"\xcf\xfa\xed\xfe",  # Mach-O 64
    b"#!",  # shebang script
)

_ARCHIVE_MAGIC: tuple[bytes, ...] = (
    b"PK\x03\x04",  # zip (also docx/xlsx - see note below)
    b"Rar!",
    b"\x1f\x8b",  # gzip
    b"7z\xbc\xaf\x27\x1c",
    b"BZh",
    b"\xfd7zXZ",
)

SUPPORTED_MIMES: dict[str, MediaKind] = {
    "image/jpeg": MediaKind.IMAGE,
    "image/png": MediaKind.IMAGE,
    "image/webp": MediaKind.IMAGE,
    "image/heic": MediaKind.IMAGE,
    "image/gif": MediaKind.IMAGE,
    "application/pdf": MediaKind.PDF,
    "audio/mpeg": MediaKind.AUDIO,
    "audio/mp4": MediaKind.AUDIO,
    "audio/ogg": MediaKind.AUDIO,
    "audio/wav": MediaKind.AUDIO,
    "audio/x-wav": MediaKind.AUDIO,
    "audio/webm": MediaKind.AUDIO,
    "audio/amr": MediaKind.AUDIO,
}

_MAX_BYTES: dict[MediaKind, str] = {
    MediaKind.IMAGE: "max_image_bytes",
    MediaKind.PDF: "max_pdf_bytes",
    MediaKind.AUDIO: "max_audio_bytes",
    MediaKind.DOCUMENT: "max_document_bytes",
}

_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]")


@dataclass(frozen=True, slots=True)
class ValidationResult:
    ok: bool
    kind: MediaKind | None = None
    mime: str | None = None
    reason: RejectReason | None = None
    detail: str = ""


def sanitize_filename(name: str | None, *, fallback: str = "upload") -> str:
    """Strip path components and anything that could escape a directory.

    Returns a flat, ASCII, extension-preserving name. Never returns an empty
    string, and never returns something containing a separator.
    """
    if not name:
        return fallback
    # Take the basename under BOTH separators: a POSIX host still receives
    # Windows-style paths from clients.
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    # Normalize away homoglyphs and combining characters before filtering.
    base = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode("ascii")
    base = _UNSAFE_FILENAME.sub("_", base)
    # Strip leading dots and underscores AFTER substitution: doing it before
    # lets "  ..hidden" become "__..hidden", which is still a dotfile.
    base = base.lstrip("._")[:120]
    return base or fallback


def sniff_mime(data: bytes) -> str | None:
    """Real content type from magic bytes. Filename and sender hint are ignored."""
    kind = filetype.guess(data)
    return kind.mime if kind else None


def _starts_with_any(data: bytes, prefixes: tuple[bytes, ...]) -> bool:
    return any(data.startswith(p) for p in prefixes)


def validate(
    data: bytes,
    *,
    limits: MediaLimits,
    declared_mime: str | None = None,
) -> ValidationResult:
    """Cheap safety and budget checks on fetched bytes.

    Order matters: the cheapest rejections come first, and nothing here opens a
    parser or allocates proportional to the decoded content.
    """
    if not data:
        return ValidationResult(False, reason=RejectReason.CORRUPT, detail="empty file")

    # 1. Executables - rejected regardless of extension or claimed type.
    if _starts_with_any(data, _EXECUTABLE_MAGIC):
        return ValidationResult(False, reason=RejectReason.EXECUTABLE, detail="executable content")

    # 2. Archives. Not supported initially: an archive is a container whose real
    #    contents are unknown until expanded, and expanding it is exactly the
    #    decompression-bomb surface we decline to have.
    if _starts_with_any(data, _ARCHIVE_MAGIC):
        return ValidationResult(False, reason=RejectReason.ARCHIVE, detail="archive content")

    # 3. Real MIME.
    mime = sniff_mime(data)
    if mime is None:
        return ValidationResult(
            False, reason=RejectReason.UNSUPPORTED_MIME, detail="unrecognised format"
        )
    kind = SUPPORTED_MIMES.get(mime)
    if kind is None:
        return ValidationResult(False, reason=RejectReason.UNSUPPORTED_MIME, detail=mime)

    # 4. Declared vs actual. A mismatch is not fatal on its own - clients send
    #    sloppy types - but a mismatch of *category* means someone is lying.
    if declared_mime and declared_mime.split("/")[0] != mime.split("/")[0]:
        return ValidationResult(
            False,
            reason=RejectReason.MIME_MISMATCH,
            detail=f"declared {declared_mime}, actual {mime}",
        )

    # 5. Size, per kind.
    limit = int(getattr(limits, _MAX_BYTES[kind]))
    if len(data) > limit:
        return ValidationResult(
            False,
            kind=kind,
            mime=mime,
            reason=RejectReason.TOO_LARGE,
            detail=f"{len(data)} bytes exceeds {limit}",
        )

    return ValidationResult(True, kind=kind, mime=mime)


def validate_image_dimensions(data: bytes, limits: MediaLimits) -> ValidationResult:
    """Decompression-bomb guard, using the header only.

    Pillow reads dimensions from the header without decoding pixels, so this
    stays cheap even for a hostile file that claims to be 60000x60000.
    """
    import io

    from PIL import Image, UnidentifiedImageError

    try:
        # Do not let Pillow's own bomb check raise before ours reports a reason.
        previous = Image.MAX_IMAGE_PIXELS
        Image.MAX_IMAGE_PIXELS = None
        try:
            with Image.open(io.BytesIO(data)) as img:
                width, height = img.size
        finally:
            Image.MAX_IMAGE_PIXELS = previous
    except UnidentifiedImageError:
        return ValidationResult(False, reason=RejectReason.CORRUPT, detail="unreadable image")
    except Exception as exc:  # noqa: BLE001 - any decode failure is a rejection
        return ValidationResult(False, reason=RejectReason.CORRUPT, detail=type(exc).__name__)

    if width > limits.max_image_dimension or height > limits.max_image_dimension:
        return ValidationResult(
            False,
            reason=RejectReason.DIMENSIONS,
            detail=f"{width}x{height} exceeds {limits.max_image_dimension}",
        )
    if width * height > limits.max_image_pixels:
        return ValidationResult(
            False,
            reason=RejectReason.DECOMPRESSION_BOMB,
            detail=f"{width * height} pixels exceeds {limits.max_image_pixels}",
        )
    return ValidationResult(True, kind=MediaKind.IMAGE)
