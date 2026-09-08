"""Content-aware chunking.

Splitting on a fixed character count is the naive approach and it is genuinely
bad here: it cuts equations in half, separates a question from its answer, and
strips the heading that told you what the passage was about. Retrieval then
returns fragments that are individually meaningless.

So the splitter respects structure first and only falls back to size:

    page boundary  >  heading  >  numbered problem  >  paragraph  >  hard split

Every chunk carries its page and section, because a citation without a page is
not a citation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from tutortwin.domain.knowledge import Chunk

CHUNKER_VERSION = "structural-v1"

TARGET_TOKENS = 220
"""Chunks around this size retrieve well: large enough to carry an idea, small
enough that five of them fit a context budget."""

MAX_TOKENS = 400
MIN_TOKENS = 8
"""Below this a chunk is a fragment - a bare heading or a page number - and
indexing it only adds ranking noise.

Deliberately low. A real exercise ("1. Solve x^2 - 5x + 6 = 0") is around
nineteen tokens, and a higher threshold silently discarded exactly the content
students ask about by number."""

CHARS_PER_TOKEN = 4

# `[page N]` markers are how the media pipeline hands over extracted PDF text.
_PAGE_MARKER = re.compile(r"^\[page (\d+)\]\s*$", re.MULTILINE)

# Headings: markdown, numbered sections, or a short all-caps line.
_HEADING = re.compile(
    r"^(?:#{1,6}\s+.+"
    r"|\d+(?:\.\d+)*\.?\s+[A-Z].{0,80}"
    # All-caps titles routinely carry colons and digits:
    # "CHAPTER ONE: QUADRATIC EQUATIONS", "UNIT 3 - TRIGONOMETRY".
    r"|[A-Z][A-Z0-9 \t&':,\-]{4,60})$",
    re.MULTILINE,
)

# Numbered problems: "4.", "Q4)", "Exercise 12". Splitting between these keeps a
# question with its own working rather than merging two unrelated problems.
_PROBLEM = re.compile(
    r"^\s*(?:(?:Q(?:uestion)?|Ex(?:ercise)?|Problem)\s*)?\d{1,3}\s*[.)\]]\s+",
    re.MULTILINE | re.IGNORECASE,
)


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)


@dataclass(frozen=True, slots=True)
class _Block:
    text: str
    page: int | None
    section: str | None


def _split_pages(text: str) -> list[tuple[int | None, str]]:
    """Separate `[page N]`-marked regions, preserving the page number."""
    matches = list(_PAGE_MARKER.finditer(text))
    if not matches:
        return [(None, text)]

    pages: list[tuple[int | None, str]] = []
    preamble = text[: matches[0].start()].strip()
    if preamble:
        pages.append((None, preamble))

    for index, match in enumerate(matches):
        page = int(match.group(1))
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end() : end].strip()
        if body:
            pages.append((page, body))
    return pages


def _current_heading(text: str, position: int) -> str | None:
    """The nearest heading at or above `position`, for section provenance.

    A problem line ("1. Solve ...") is not a section: it would make every
    problem its own heading and lose the chapter it belongs to. So headings that
    a problem-splitter would also have matched are skipped here.
    """
    heading: str | None = None
    for match in _HEADING.finditer(text):
        # `>` not `>=`: a piece that starts exactly at its heading owns it.
        if match.start() > position:
            break
        candidate = match.group(0).strip()
        if _PROBLEM.match(candidate):
            continue
        heading = candidate.lstrip("#").strip()
    return heading


def _structural_pieces(body: str) -> list[tuple[str, str | None]]:
    """Split on headings and problem numbers, keeping each boundary's section."""
    boundaries = sorted(
        {0}
        | {m.start() for m in _HEADING.finditer(body)}
        | {m.start() for m in _PROBLEM.finditer(body)}
        | {len(body)}
    )
    pieces: list[tuple[str, str | None]] = []
    for start, end in zip(boundaries, boundaries[1:], strict=False):
        segment = body[start:end].strip()
        if segment:
            pieces.append((segment, _current_heading(body, start)))
    return pieces or [(body.strip(), None)]


def _split_oversized(text: str) -> list[str]:
    """Break a too-large piece at paragraph, then sentence, then hard limit."""
    if estimate_tokens(text) <= MAX_TOKENS:
        return [text]

    out: list[str] = []
    buffer: list[str] = []
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        candidate = "\n\n".join([*buffer, paragraph])
        if estimate_tokens(candidate) > TARGET_TOKENS and buffer:
            out.append("\n\n".join(buffer))
            buffer = [paragraph]
        else:
            buffer.append(paragraph)
    if buffer:
        out.append("\n\n".join(buffer))

    # A single paragraph can still exceed the ceiling - split on sentences.
    final: list[str] = []
    for piece in out:
        if estimate_tokens(piece) <= MAX_TOKENS:
            final.append(piece)
            continue
        sentences = re.split(r"(?<=[.!?])\s+", piece)
        current: list[str] = []
        for sentence in sentences:
            candidate = " ".join([*current, sentence])
            if estimate_tokens(candidate) > TARGET_TOKENS and current:
                final.append(" ".join(current))
                current = [sentence]
            else:
                current.append(sentence)
        if current:
            final.append(" ".join(current))

    # Last resort: a single sentence longer than the ceiling gets cut by size.
    bounded: list[str] = []
    limit = MAX_TOKENS * CHARS_PER_TOKEN
    for piece in final:
        if len(piece) <= limit:
            bounded.append(piece)
        else:
            bounded.extend(piece[i : i + limit] for i in range(0, len(piece), limit))
    return bounded


def chunk_text(text: str) -> tuple[Chunk, ...]:
    """Split into retrievable chunks, preserving page and section provenance."""
    if not text or not text.strip():
        return ()

    chunks: list[Chunk] = []
    ordinal = 0

    for page, body in _split_pages(text):
        for piece, section in _structural_pieces(body):
            for part in _split_oversized(piece):
                cleaned = part.strip()
                tokens = estimate_tokens(cleaned)
                if not cleaned or tokens < MIN_TOKENS:
                    # Stray headings and page numbers add ranking noise.
                    continue
                chunks.append(
                    Chunk(
                        text=cleaned,
                        ordinal=ordinal,
                        page_number=page,
                        section=section,
                        token_estimate=tokens,
                    )
                )
                ordinal += 1

    return tuple(chunks)


def normalize_for_hash(text: str) -> str:
    """Canonical form for deduplication and embedding-cache keys.

    Whitespace-only differences must not produce a second embedding of the same
    content - re-uploading a file with different line endings is common.
    """
    return re.sub(r"\s+", " ", text).strip().lower()
