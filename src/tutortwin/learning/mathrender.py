"""Typeset mathematics as an image, so a student sees an equation, not ASCII.

WhatsApp has no maths rendering. A quadratic formula sent as text arrives as
`x = (-b +- sqrt(b^2 - 4ac)) / 2a`, which is the thing students misread in
exactly the way that loses them the mark. Sending a typeset image instead is the
single largest legibility win available on this channel.

**matplotlib's `mathtext`, not LaTeX.** mathtext is a self-contained TeX-subset
renderer built into matplotlib, which is already a dependency. Real LaTeX would
mean a 4GB TeX Live install on every container, a `subprocess` call per render,
and - the part that actually rules it out - **executing model-authored markup**:
`\\write18`, `\\input{/etc/passwd}` and a dozen other primitives turn a rendering
step into arbitrary file access. mathtext parses and draws; it never executes.

The trade is that mathtext supports a subset of LaTeX. That is the correct
trade here, because the subset covers what school and early-undergraduate
mathematics needs, and anything it cannot parse falls back to plain text rather
than failing the answer.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass

import matplotlib

matplotlib.use("Agg")  # headless; must precede the pyplot import

import matplotlib.pyplot as plt  # noqa: E402

from tutortwin.observability.logging import get_logger  # noqa: E402

logger = get_logger(__name__)

MAX_EXPRESSION_CHARS = 400
"""A whole worked solution is prose with maths in it, not one expression. Past
this length the render is unreadable on a phone anyway, so it stays as text."""

MAX_LINES = 12

DPI = 220
"""Deliberately high. The image is viewed on a phone, often zoomed, and a
subscript that blurs into the baseline is worse than no image at all."""

# Commands that mathtext does not implement, or that make no sense outside a
# full document. Their presence means "do not try" rather than "render badly".
#
# **Matched as a prefix, with no trailing `\b`.** A word boundary looks more
# precise and silently fails on the most dangerous entry in the list: in
# `\write18` the character after `write` is a digit, so `e` to `1` is not a
# boundary and `write\b` does not match. Prefix matching is safe for this
# vocabulary - no mathtext command begins with any of these strings, so
# `\int`, `\infty`, `\frac` and `\lambda` all still render.
_UNSUPPORTED = re.compile(
    r"\\(begin|end|newcommand|def|input|include|write|usepackage|documentclass"
    r"|label|ref|cite|verb|catcode|csname|openin|openout|read)"
)

# The delimiters a model wraps maths in. Stripped before rendering because
# mathtext supplies its own `$...$` wrapper.
_DELIMITERS = (
    (r"\[", r"\]"),
    (r"\(", r"\)"),
    ("$$", "$$"),
    ("$", "$"),
)


@dataclass(frozen=True, slots=True)
class RenderedMath:
    png: bytes
    width: int
    height: int


class UnrenderableMath(ValueError):
    """The expression is not something mathtext can typeset."""


def strip_delimiters(expression: str) -> str:
    """Remove the outer `$...$`, `\\[...\\]` or `\\(...\\)` a model adds."""
    text = expression.strip()
    for open_token, close_token in _DELIMITERS:
        if text.startswith(open_token) and text.endswith(close_token):
            inner = text[len(open_token) : -len(close_token)].strip()
            if inner:
                return inner
    return text


def is_renderable(expression: str) -> bool:
    """Cheap pre-check, so an unrenderable answer is never half-processed.

    Conservative on purpose: a false negative sends good text, while a false
    positive sends a broken image where an answer should be.
    """
    text = strip_delimiters(expression)
    if not text or len(text) > MAX_EXPRESSION_CHARS:
        return False
    if _UNSUPPORTED.search(text):
        return False
    if text.count("$") % 2:
        # An odd number of delimiters means mathtext will swallow the rest of
        # the string into maths mode and render nonsense.
        return False
    return any(ch in text for ch in "\\^_{}=+-/") or bool(
        re.search(r"[0-9]", text)
    )


def render(expression: str, *, colour: str = "#0b1020") -> RenderedMath:
    """Typeset one expression, or several lines, as a transparent PNG.

    Raises `UnrenderableMath` rather than returning a broken image: the caller
    falls back to sending the text, which is always better than sending a
    picture of a parse error.
    """
    text = strip_delimiters(expression)
    if not is_renderable(text):
        raise UnrenderableMath("expression is not safely renderable")

    lines = [line.strip() for line in text.splitlines() if line.strip()][:MAX_LINES]
    if not lines:
        raise UnrenderableMath("nothing to render")

    # Each line becomes its own maths group. Joining with a newline inside one
    # `$...$` is a mathtext parse error, not a line break.
    body = "\n".join(f"${line}$" for line in lines)

    figure = plt.figure(figsize=(0.01, 0.01))
    try:
        figure.text(0, 0, body, fontsize=26, color=colour, linespacing=1.7)
        buffer = io.BytesIO()
        # `bbox_inches="tight"` shrinks the canvas to the ink, which is what
        # lets a one-line formula arrive as a strip rather than a mostly-empty
        # square. The padding keeps descenders off the edge.
        figure.savefig(
            buffer,
            format="png",
            dpi=DPI,
            bbox_inches="tight",
            pad_inches=0.28,
            transparent=False,
            facecolor="white",
        )
    except (ValueError, RuntimeError) as exc:
        # mathtext raises on malformed markup. Model output is untrusted input,
        # so this is an expected branch, not an exceptional one.
        logger.info("math_render_failed", error_type=type(exc).__name__)
        raise UnrenderableMath(str(exc)) from exc
    finally:
        plt.close(figure)

    data = buffer.getvalue()
    # Read the size back out of the PNG, not off the canvas. `bbox_inches`
    # crops to the ink *during* the save and never resizes the figure, so
    # `canvas.get_width_height()` still reports the 0.01in placeholder - which
    # reaches WhatsApp as a claimed 1x1 image.
    width, height = _png_size(data)
    logger.info("math_rendered", lines=len(lines), bytes=len(data), size=f"{width}x{height}")
    return RenderedMath(png=data, width=width, height=height)


def _png_size(data: bytes) -> tuple[int, int]:
    """Width and height from the IHDR chunk: bytes 16-24 of any valid PNG."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return (0, 0)
    return (
        int.from_bytes(data[16:20], "big"),
        int.from_bytes(data[20:24], "big"),
    )


_BLOCK = re.compile(
    r"\$\$(?P<dd>.+?)\$\$|\\\[(?P<sq>.+?)\\\]",
    re.DOTALL,
)


def extract_display_math(answer: str) -> list[str]:
    """Pull out the *display* maths from a tutoring answer.

    Only `$$...$$` and `\\[...\\]`, never inline `$x$`. Inline maths is a symbol
    inside a sentence and reads perfectly well as text; rendering every `$n$` in
    a paragraph would produce a dozen images and bury the explanation.
    """
    found: list[str] = []
    for match in _BLOCK.finditer(answer):
        expression = (match.group("dd") or match.group("sq") or "").strip()
        if expression and is_renderable(expression):
            found.append(expression)
    return found


__all__ = [
    "MAX_EXPRESSION_CHARS",
    "RenderedMath",
    "UnrenderableMath",
    "extract_display_math",
    "is_renderable",
    "render",
    "strip_delimiters",
]
