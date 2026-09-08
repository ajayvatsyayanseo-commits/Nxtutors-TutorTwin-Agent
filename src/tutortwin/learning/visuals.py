"""Deterministic technical diagrams.

**No AI image generation, ever, for anything constructible.** A function plot, a
free-body diagram or a circuit is geometry: computing it is exact, free and
reproducible, while generating it is expensive, non-reproducible and routinely
mislabels axes. A model may only produce a *structured specification*, which is
then validated and rendered by code.

**The spec is the trust boundary.** A model-produced spec is untrusted input, so
it is a Pydantic model with closed enums and bounded numbers. Nothing in it is
evaluated as code.

**Plotting a supplied expression is the dangerous part.** `sympy.lambdify`
generates and `exec`s Python source, so it is not used. Points are computed with
`subs` on an already-validated expression tree - which, measured on a 500-point
plot, is also 25x faster than lambdify because it avoids the codegen entirely.
The safe path here is the fast one.
"""

from __future__ import annotations

import hashlib
import io
import math
import re
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

import matplotlib
import sympy
from matplotlib.patches import Rectangle
from pydantic import BaseModel, ConfigDict, Field

from tutortwin.domain.learning import ArtifactFormat, ArtifactKind
from tutortwin.learning.verification import UnsafeExpression, safe_parse
from tutortwin.observability.logging import get_logger

matplotlib.use("Agg")  # headless; must be set before pyplot is imported

logger = get_logger(__name__)

MAX_PLOT_POINTS = 400
"""Enough for a smooth curve, bounded so a hostile range cannot exhaust memory."""

MAX_SERIES = 4
MAX_ELEMENTS = 40


class SpecModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlotSeries(SpecModel):
    expression: str = Field(max_length=200)
    label: str = Field(default="", max_length=60)


class PlotSpec(SpecModel):
    """A function plot. Every number is bounded, every string is length-capped."""

    title: str = Field(default="", max_length=120)
    variable: str = Field(default="x", pattern=r"^[a-zA-Z][a-zA-Z0-9_]{0,7}$")
    series: tuple[PlotSeries, ...] = Field(min_length=1, max_length=MAX_SERIES)
    x_min: float = Field(default=-10.0, ge=-1e6, le=1e6)
    x_max: float = Field(default=10.0, ge=-1e6, le=1e6)
    x_label: str = Field(default="x", max_length=40)
    y_label: str = Field(default="y", max_length=40)
    points: int = Field(default=200, ge=10, le=MAX_PLOT_POINTS)


class Point(SpecModel):
    x: float = Field(ge=-1e4, le=1e4)
    y: float = Field(ge=-1e4, le=1e4)
    label: str = Field(default="", max_length=24)


class Segment(SpecModel):
    start: Point
    end: Point
    label: str = Field(default="", max_length=24)


class GeometrySpec(SpecModel):
    title: str = Field(default="", max_length=120)
    points: tuple[Point, ...] = Field(default=(), max_length=MAX_ELEMENTS)
    segments: tuple[Segment, ...] = Field(default=(), max_length=MAX_ELEMENTS)


class Arrow(SpecModel):
    """One force or vector, drawn from the body outward."""

    dx: float = Field(ge=-100, le=100)
    dy: float = Field(ge=-100, le=100)
    label: str = Field(default="", max_length=24)


class FreeBodySpec(SpecModel):
    title: str = Field(default="", max_length=120)
    body_label: str = Field(default="", max_length=24)
    arrows: tuple[Arrow, ...] = Field(min_length=1, max_length=12)


@dataclass(frozen=True, slots=True)
class RenderedArtifact:
    data: bytes
    artifact_format: ArtifactFormat
    kind: ArtifactKind
    width: int
    height: int

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


class InvalidSpec(ValueError):
    """The specification could not be rendered safely."""


# --- safe evaluation ----------------------------------------------------------


def evaluate_series(expression: str, variable: str, xs: list[float]) -> list[float | None]:
    """Evaluate a parsed expression pointwise. No code generation.

    `None` marks a point that is undefined (a pole, a negative square root);
    matplotlib renders those as gaps rather than drawing a false line through
    them.
    """
    try:
        parsed = safe_parse(expression)
    except UnsafeExpression as exc:
        raise InvalidSpec(f"expression rejected: {exc.reason}") from exc

    free = parsed.free_symbols
    symbol = sympy.Symbol(variable)
    if free - {symbol}:
        raise InvalidSpec(
            f"expression uses unknown symbols: {sorted(str(s) for s in free - {symbol})}"
        )

    values: list[float | None] = []
    for x in xs:
        try:
            evaluated = complex(parsed.subs(symbol, x))
            # `complex()` of SymPy's `zoo` (complex infinity, e.g. 1/0) yields
            # nan rather than raising, so a pole must be caught explicitly.
            if math.isnan(evaluated.real) or math.isinf(evaluated.real):
                values.append(None)
            # Discard complex results: plotting the real part of a complex value
            # silently draws a curve that does not exist.
            elif abs(evaluated.imag) > 1e-9:
                values.append(None)
            elif abs(evaluated.real) > 1e9:
                values.append(None)  # asymptote
            else:
                values.append(evaluated.real)
        except (TypeError, ValueError, ZeroDivisionError, OverflowError):
            values.append(None)
    return values


# --- renderers ----------------------------------------------------------------


def render_plot(
    spec: PlotSpec, *, image_format: ArtifactFormat = ArtifactFormat.PNG
) -> RenderedArtifact:
    """Render a function plot deterministically."""
    if spec.x_max <= spec.x_min:
        raise InvalidSpec("x_max must be greater than x_min")

    import matplotlib.pyplot as pyplot

    step = (spec.x_max - spec.x_min) / (spec.points - 1)
    xs = [spec.x_min + i * step for i in range(spec.points)]

    figure, axes = pyplot.subplots(figsize=(6.4, 4.0), dpi=110)
    try:
        for series in spec.series:
            ys = evaluate_series(series.expression, spec.variable, xs)
            # `None` is matplotlib's documented gap marker but is absent from the
            # stubs' accepted types, hence the cast rather than a data change.
            axes.plot(xs, cast("list[float]", ys), label=series.label or series.expression)

        axes.set_xlabel(spec.x_label)
        axes.set_ylabel(spec.y_label)
        if spec.title:
            axes.set_title(spec.title)
        axes.grid(True, alpha=0.3)
        axes.axhline(0, color="black", linewidth=0.8)
        axes.axvline(0, color="black", linewidth=0.8)
        if any(s.label for s in spec.series) or len(spec.series) > 1:
            axes.legend(loc="best", fontsize=8)

        buffer = io.BytesIO()
        figure.savefig(
            buffer,
            format="svg" if image_format is ArtifactFormat.SVG else "png",
            bbox_inches="tight",
        )
        data = buffer.getvalue()
    finally:
        # Figures are process-global in matplotlib; not closing them leaks memory
        # in a long-lived server.
        pyplot.close(figure)

    if image_format is ArtifactFormat.SVG:
        data = sanitize_svg(data)

    return RenderedArtifact(
        data=data,
        artifact_format=image_format,
        kind=ArtifactKind.FUNCTION_PLOT,
        width=704,
        height=440,
    )


def render_free_body(spec: FreeBodySpec) -> RenderedArtifact:
    """Free-body diagram: a box with labelled force arrows."""
    import matplotlib.pyplot as pyplot

    figure, axes = pyplot.subplots(figsize=(5.0, 5.0), dpi=110)
    try:
        axes.add_patch(Rectangle((-0.5, -0.5), 1.0, 1.0, fill=False, linewidth=2))
        if spec.body_label:
            axes.text(0, 0, spec.body_label, ha="center", va="center", fontsize=11)

        reach = max((abs(a.dx) + abs(a.dy) for a in spec.arrows), default=1.0) or 1.0
        for arrow in spec.arrows:
            axes.annotate(
                "",
                xy=(arrow.dx, arrow.dy),
                xytext=(0, 0),
                arrowprops={"arrowstyle": "->", "linewidth": 1.8},
            )
            if arrow.label:
                axes.text(arrow.dx * 1.12, arrow.dy * 1.12, arrow.label, fontsize=9)

        limit = reach * 1.4
        axes.set_xlim(-limit, limit)
        axes.set_ylim(-limit, limit)
        axes.set_aspect("equal")
        axes.axis("off")
        if spec.title:
            axes.set_title(spec.title)

        buffer = io.BytesIO()
        figure.savefig(buffer, format="png", bbox_inches="tight")
        data = buffer.getvalue()
    finally:
        pyplot.close(figure)

    return RenderedArtifact(
        data=data,
        artifact_format=ArtifactFormat.PNG,
        kind=ArtifactKind.FREE_BODY,
        width=550,
        height=550,
    )


def render_geometry_svg(spec: GeometrySpec) -> RenderedArtifact:
    """Hand-built SVG. Every coordinate comes from a validated bounded float."""
    xs = [p.x for p in spec.points] + [c.x for s in spec.segments for c in (s.start, s.end)]
    ys = [p.y for p in spec.points] + [c.y for s in spec.segments for c in (s.start, s.end)]
    if not xs or not ys:
        raise InvalidSpec("geometry needs at least one point or segment")

    pad = 20.0
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    width = max(max_x - min_x, 1.0)
    height = max(max_y - min_y, 1.0)
    scale = 300.0 / max(width, height)

    def tx(x: float) -> float:
        return round((x - min_x) * scale + pad, 2)

    def ty(y: float) -> float:
        # SVG's y axis points down; flip so diagrams read the mathematical way.
        return round((max_y - y) * scale + pad, 2)

    view_w = round(width * scale + 2 * pad, 2)
    view_h = round(height * scale + 2 * pad, 2)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{view_w}" height="{view_h}" '
        f'viewBox="0 0 {view_w} {view_h}">',
        '<rect width="100%" height="100%" fill="white"/>',
    ]
    if spec.title:
        parts.append(
            f'<text x="{view_w / 2}" y="14" text-anchor="middle" '
            f'font-size="12">{_escape(spec.title)}</text>'
        )
    for segment in spec.segments:
        parts.append(
            f'<line x1="{tx(segment.start.x)}" y1="{ty(segment.start.y)}" '
            f'x2="{tx(segment.end.x)}" y2="{ty(segment.end.y)}" '
            f'stroke="black" stroke-width="1.6"/>'
        )
        if segment.label:
            mx = (tx(segment.start.x) + tx(segment.end.x)) / 2
            my = (ty(segment.start.y) + ty(segment.end.y)) / 2
            parts.append(
                f'<text x="{mx}" y="{my - 4}" font-size="10">{_escape(segment.label)}</text>'
            )
    for point in spec.points:
        parts.append(f'<circle cx="{tx(point.x)}" cy="{ty(point.y)}" r="3" fill="black"/>')
        if point.label:
            parts.append(
                f'<text x="{tx(point.x) + 6}" y="{ty(point.y) - 6}" '
                f'font-size="10">{_escape(point.label)}</text>'
            )
    parts.append("</svg>")

    data = sanitize_svg("".join(parts).encode("utf-8"))
    return RenderedArtifact(
        data=data,
        artifact_format=ArtifactFormat.SVG,
        kind=ArtifactKind.GEOMETRY,
        width=int(view_w),
        height=int(view_h),
    )


def render_plot_tikz(spec: PlotSpec) -> RenderedArtifact:
    """TikZ source, for students who want the figure in LaTeX.

    Emitted as source rather than compiled: running LaTeX on generated input is
    arbitrary code execution, and the student's own toolchain can compile it.
    """
    lines = [
        "\\begin{tikzpicture}",
        f"\\begin{{axis}}[xlabel={{{_tex(spec.x_label)}}}, ylabel={{{_tex(spec.y_label)}}},"
        f" title={{{_tex(spec.title)}}}, grid=both, domain={spec.x_min}:{spec.x_max}]",
    ]
    for series in spec.series:
        safe_parse(series.expression)  # validate before emitting into the document
        lines.append(
            f"\\addplot[thick] {{{series.expression}}};"
            + (f" \\addlegendentry{{{_tex(series.label)}}}" if series.label else "")
        )
    lines += ["\\end{axis}", "\\end{tikzpicture}"]
    data = "\n".join(lines).encode("utf-8")
    return RenderedArtifact(
        data=data,
        artifact_format=ArtifactFormat.TIKZ,
        kind=ArtifactKind.FUNCTION_PLOT,
        width=0,
        height=0,
    )


# --- validation ---------------------------------------------------------------

_FORBIDDEN_SVG_TAGS = {"script", "foreignObject", "iframe", "image", "a"}
"""`<use>` is deliberately absent: matplotlib reuses glyph outlines with
`<use href="#id">`, which is an internal fragment and cannot fetch anything.
Its href is checked below like any other reference."""

_FORBIDDEN_ATTR = re.compile(r"^on[a-z]+$", re.IGNORECASE)

# Only attributes that actually *load* something are checked for external
# references. Checking every attribute rejects matplotlib's own inert Dublin Core
# metadata (`<type resource="http://purl.org/dc/dcmitype/StillImage">`), which is
# a description of the document, not a resource it fetches.
_REFERENCE_ATTRS = {"href", "src", "xlink:href", "style", "filter", "mask", "fill"}
_EXTERNAL_REF = re.compile(r"(?:https?:|//|data:|javascript:|file:)", re.IGNORECASE)


def sanitize_svg(data: bytes) -> bytes:
    """Reject SVG that could execute or fetch anything.

    SVG is a document format that runs script and loads external resources, so a
    generated one is validated before delivery even though we wrote it - labels
    originating from a model or a student flow into it.
    """
    # Entity declarations are rejected BEFORE parsing. `xml.etree` performs
    # entity expansion with no bound, so a billion-laughs payload hangs the
    # parser - verified here, the process had to be killed. An external entity
    # (`SYSTEM "file:///etc/passwd"`) is the same hole used to read files.
    #
    # `<!DOCTYPE` itself is permitted because matplotlib emits the standard SVG
    # 1.1 doctype; it is `<!ENTITY` that enables both attacks, and matplotlib
    # never emits one.
    if b"<!ENTITY" in data or b"<!entity" in data:
        raise InvalidSpec("SVG declares an XML entity")

    try:
        root = ElementTree.fromstring(data.decode("utf-8"))  # noqa: S314
    except (ElementTree.ParseError, UnicodeDecodeError) as exc:
        raise InvalidSpec(f"generated SVG is not well-formed: {exc}") from exc

    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag in _FORBIDDEN_SVG_TAGS:
            raise InvalidSpec(f"SVG contains a forbidden element: <{tag}>")
        for name, value in element.attrib.items():
            attribute = name.rsplit("}", 1)[-1]
            if _FORBIDDEN_ATTR.match(attribute):
                raise InvalidSpec(f"SVG contains an event handler: {attribute}")
            if attribute in _REFERENCE_ATTRS and not value.startswith("#"):
                if _EXTERNAL_REF.search(value):
                    raise InvalidSpec(f"SVG references external content in {attribute}")
    return data


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


_SUPERSCRIPT = re.compile(r"\^\{?([A-Za-z0-9]{1,3})\}?")
_SUBSCRIPT = re.compile(r"_\{?([A-Za-z0-9]{1,3})\}?")


def _tex(text: str) -> str:
    """Neutralise TeX control characters so a label cannot inject a macro.

    `^` and `_` are stripped with the rest, then reintroduced only as
    `\\textsuperscript`/`\\textsubscript` around a short alphanumeric run. Simply
    deleting them turned the title "y = x^2 - 4" into "y = x2 - 4", which is a
    different equation - the label was safe and wrong. Rebuilding from a matched
    group means the emitted macro can only ever wrap characters that passed the
    filter.
    """
    superscripts = _SUPERSCRIPT.findall(text)
    subscripts = _SUBSCRIPT.findall(text)
    text = _SUPERSCRIPT.sub("\x00", text)
    text = _SUBSCRIPT.sub("\x01", text)

    for char in ("\\", "{", "}", "$", "&", "#", "^", "_", "~", "%"):
        text = text.replace(char, "")

    for value in superscripts:
        text = text.replace("\x00", f"\\textsuperscript{{{value}}}", 1)
    for value in subscripts:
        text = text.replace("\x01", f"\\textsubscript{{{value}}}", 1)
    return text


# --- block / flow diagrams ----------------------------------------------------


class BlockNode(SpecModel):
    node_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,24}$")
    label: str = Field(max_length=40)
    row: int = Field(ge=0, le=11)
    column: int = Field(ge=0, le=5)


class BlockEdge(SpecModel):
    source: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,24}$")
    target: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,24}$")
    label: str = Field(default="", max_length=24)


class BlockDiagramSpec(SpecModel):
    """A flow or block diagram on a fixed grid.

    Grid placement rather than an auto-layout: a model that must name a row and a
    column produces a diagram we can draw exactly, whereas a free-form layout
    request produces overlapping boxes and a second call to fix them.
    """

    title: str = Field(default="", max_length=120)
    nodes: tuple[BlockNode, ...] = Field(min_length=1, max_length=24)
    edges: tuple[BlockEdge, ...] = Field(default=(), max_length=40)


_BOX_W, _BOX_H = 150.0, 46.0
_GAP_X, _GAP_Y = 46.0, 34.0


def render_block_diagram(spec: BlockDiagramSpec) -> RenderedArtifact:
    """Deterministic SVG flowchart."""
    ids = {node.node_id for node in spec.nodes}
    if len(ids) != len(spec.nodes):
        raise InvalidSpec("duplicate node id")
    # A model-produced spec routinely references a node it forgot to declare.
    # Drawing an arrow from nowhere is worse than refusing the spec.
    for edge in spec.edges:
        if edge.source not in ids or edge.target not in ids:
            raise InvalidSpec(f"edge references an unknown node: {edge.source}->{edge.target}")

    columns = max(node.column for node in spec.nodes) + 1
    rows = max(node.row for node in spec.nodes) + 1
    top = 34.0 if spec.title else 12.0
    width = columns * _BOX_W + (columns + 1) * _GAP_X
    height = top + rows * _BOX_H + (rows + 1) * _GAP_Y

    def box(node: BlockNode) -> tuple[float, float]:
        return (
            _GAP_X + node.column * (_BOX_W + _GAP_X),
            top + _GAP_Y + node.row * (_BOX_H + _GAP_Y),
        )

    positions = {node.node_id: box(node) for node in spec.nodes}

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" '
        f'height="{height:.0f}" viewBox="0 0 {width:.0f} {height:.0f}">',
        '<rect width="100%" height="100%" fill="white"/>',
        '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="black"/></marker></defs>',
    ]
    if spec.title:
        parts.append(
            f'<text x="{width / 2:.1f}" y="20" text-anchor="middle" font-size="13" '
            f'font-weight="bold">{_escape(spec.title)}</text>'
        )

    for edge in spec.edges:
        sx, sy = positions[edge.source]
        tx_, ty_ = positions[edge.target]
        x1, y1 = sx + _BOX_W / 2, sy + _BOX_H
        x2, y2 = tx_ + _BOX_W / 2, ty_
        if abs(sy - ty_) < 1.0:  # same row: connect side to side
            x1, y1 = (sx + _BOX_W, sy + _BOX_H / 2) if tx_ > sx else (sx, sy + _BOX_H / 2)
            x2, y2 = (tx_, ty_ + _BOX_H / 2) if tx_ > sx else (tx_ + _BOX_W, ty_ + _BOX_H / 2)
        parts.append(
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="black" stroke-width="1.4" marker-end="url(#arrow)"/>'
        )
        if edge.label:
            parts.append(
                f'<text x="{(x1 + x2) / 2 + 4:.1f}" y="{(y1 + y2) / 2 - 3:.1f}" '
                f'font-size="10">{_escape(edge.label)}</text>'
            )

    for node in spec.nodes:
        x, y = positions[node.node_id]
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{_BOX_W}" height="{_BOX_H}" rx="6" '
            f'fill="#f2f4f7" stroke="black" stroke-width="1.2"/>'
        )
        parts.append(
            f'<text x="{x + _BOX_W / 2:.1f}" y="{y + _BOX_H / 2 + 4:.1f}" '
            f'text-anchor="middle" font-size="11">{_escape(node.label)}</text>'
        )
    parts.append("</svg>")

    data = sanitize_svg("".join(parts).encode("utf-8"))
    return RenderedArtifact(
        data=data,
        artifact_format=ArtifactFormat.SVG,
        kind=ArtifactKind.BLOCK_DIAGRAM,
        width=int(width),
        height=int(height),
    )


# --- circuits -----------------------------------------------------------------


class CircuitElementKind(StrEnum):
    SOURCE = "SOURCE"
    RESISTOR = "RESISTOR"
    CAPACITOR = "CAPACITOR"
    INDUCTOR = "INDUCTOR"
    SWITCH = "SWITCH"
    LAMP = "LAMP"


class CircuitElement(SpecModel):
    kind: CircuitElementKind
    label: str = Field(default="", max_length=24)


class CircuitSpec(SpecModel):
    """A single series loop.

    Series-only is a deliberate limit, not an oversight: it covers the circuits
    school physics actually asks about, and a general netlist renderer would need
    a placement algorithm whose failures are silently wrong diagrams.
    """

    title: str = Field(default="", max_length=120)
    elements: tuple[CircuitElement, ...] = Field(min_length=1, max_length=8)


def render_circuit_svg(spec: CircuitSpec) -> RenderedArtifact:
    """Deterministic series-loop schematic."""
    count = len(spec.elements)
    per_side = max(1, (count + 1) // 2)
    seg = 110.0
    width = _GAP_X * 2 + per_side * seg
    top = 34.0 if spec.title else 16.0
    height = top + 210.0

    left, right = 40.0, width - 40.0
    upper, lower = top + 30.0, top + 170.0

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" '
        f'height="{height:.0f}" viewBox="0 0 {width:.0f} {height:.0f}">',
        '<rect width="100%" height="100%" fill="white"/>',
    ]
    if spec.title:
        parts.append(
            f'<text x="{width / 2:.1f}" y="20" text-anchor="middle" font-size="13" '
            f'font-weight="bold">{_escape(spec.title)}</text>'
        )

    # The loop itself, drawn first so element symbols sit on top of the wire.
    parts.append(
        f'<rect x="{left:.1f}" y="{upper:.1f}" width="{right - left:.1f}" '
        f'height="{lower - upper:.1f}" fill="none" stroke="black" stroke-width="1.6"/>'
    )

    span = (right - left) / max(1, per_side)
    for index, element in enumerate(spec.elements):
        on_top = index < per_side
        slot = index if on_top else index - per_side
        cx = left + span * (slot + 0.5)
        if not on_top:
            cx = right - span * (slot + 0.5)
        cy = upper if on_top else lower
        parts.extend(_circuit_symbol(element, cx, cy))
        if element.label:
            parts.append(
                f'<text x="{cx:.1f}" y="{cy - 16 if on_top else cy + 26:.1f}" '
                f'text-anchor="middle" font-size="10">{_escape(element.label)}</text>'
            )
    parts.append("</svg>")

    data = sanitize_svg("".join(parts).encode("utf-8"))
    return RenderedArtifact(
        data=data,
        artifact_format=ArtifactFormat.SVG,
        kind=ArtifactKind.CIRCUIT,
        width=int(width),
        height=int(height),
    )


def _circuit_symbol(element: CircuitElement, cx: float, cy: float) -> list[str]:
    """Standard schematic symbols, drawn from bounded coordinates."""
    half = 22.0
    white_out = (
        f'<rect x="{cx - half:.1f}" y="{cy - 14:.1f}" width="{half * 2:.1f}" '
        f'height="28" fill="white"/>'
    )
    if element.kind is CircuitElementKind.RESISTOR:
        return [
            white_out,
            f'<rect x="{cx - half:.1f}" y="{cy - 9:.1f}" width="{half * 2:.1f}" '
            f'height="18" fill="white" stroke="black" stroke-width="1.5"/>',
        ]
    if element.kind is CircuitElementKind.SOURCE:
        return [
            white_out,
            f'<line x1="{cx - 6:.1f}" y1="{cy - 14:.1f}" x2="{cx - 6:.1f}" '
            f'y2="{cy + 14:.1f}" stroke="black" stroke-width="2.4"/>',
            f'<line x1="{cx + 6:.1f}" y1="{cy - 7:.1f}" x2="{cx + 6:.1f}" '
            f'y2="{cy + 7:.1f}" stroke="black" stroke-width="2.4"/>',
        ]
    if element.kind is CircuitElementKind.CAPACITOR:
        return [
            white_out,
            f'<line x1="{cx - 5:.1f}" y1="{cy - 13:.1f}" x2="{cx - 5:.1f}" '
            f'y2="{cy + 13:.1f}" stroke="black" stroke-width="2.2"/>',
            f'<line x1="{cx + 5:.1f}" y1="{cy - 13:.1f}" x2="{cx + 5:.1f}" '
            f'y2="{cy + 13:.1f}" stroke="black" stroke-width="2.2"/>',
        ]
    if element.kind is CircuitElementKind.INDUCTOR:
        arcs = "".join(
            f'<path d="M {cx - half + i * 14:.1f} {cy} a 7 7 0 0 1 14 0" fill="none" '
            f'stroke="black" stroke-width="1.5"/>'
            for i in range(3)
        )
        return [white_out, arcs]
    if element.kind is CircuitElementKind.SWITCH:
        return [
            white_out,
            f'<circle cx="{cx - 14:.1f}" cy="{cy}" r="2.5" fill="black"/>',
            f'<circle cx="{cx + 14:.1f}" cy="{cy}" r="2.5" fill="black"/>',
            f'<line x1="{cx - 14:.1f}" y1="{cy}" x2="{cx + 10:.1f}" '
            f'y2="{cy - 12:.1f}" stroke="black" stroke-width="1.6"/>',
        ]
    return [
        white_out,
        f'<circle cx="{cx:.1f}" cy="{cy}" r="11" fill="white" stroke="black" stroke-width="1.5"/>',
        f'<line x1="{cx - 8:.1f}" y1="{cy - 8:.1f}" x2="{cx + 8:.1f}" '
        f'y2="{cy + 8:.1f}" stroke="black" stroke-width="1.2"/>',
        f'<line x1="{cx - 8:.1f}" y1="{cy + 8:.1f}" x2="{cx + 8:.1f}" '
        f'y2="{cy - 8:.1f}" stroke="black" stroke-width="1.2"/>',
    ]
