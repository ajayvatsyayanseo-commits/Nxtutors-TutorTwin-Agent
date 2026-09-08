"""`VisualArtifactService`: render a diagram, store it, record what it is.

The service exists to keep one rule enforceable in one place: **a model may
produce a specification, never an image.** Every entry point here takes a
validated spec object and calls a deterministic renderer. There is no method that
accepts an image, a prompt for an image, or a URL to one, so "just this once, ask
the model to draw it" is not reachable from this API.

Artifacts are content-addressed. Asking for the same graph twice stores one blob
and one row, which also means a retry after a dropped connection is free.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from tutortwin.db.learning_models import LearningArtifact
from tutortwin.domain.learning import ArtifactFormat, ArtifactKind, ArtifactRef
from tutortwin.learning.visuals import (
    BlockDiagramSpec,
    CircuitSpec,
    FreeBodySpec,
    GeometrySpec,
    InvalidSpec,
    PlotSpec,
    RenderedArtifact,
    render_block_diagram,
    render_circuit_svg,
    render_free_body,
    render_geometry_svg,
    render_plot,
    render_plot_tikz,
)
from tutortwin.media.blobstore import BlobStore
from tutortwin.observability.logging import get_logger
from tutortwin.repositories.learning import record_artifact

logger = get_logger(__name__)

ARTIFACT_RETENTION_DAYS = 90
"""Diagrams outlive the upload they came from: a student revisits a graph weeks
later, and regenerating it would be free but the link would already be dead."""

_CONTENT_TYPES: dict[ArtifactFormat, tuple[str, str]] = {
    ArtifactFormat.PNG: ("image/png", "png"),
    ArtifactFormat.SVG: ("image/svg+xml", "svg"),
    ArtifactFormat.TIKZ: ("text/plain", "tex"),
    ArtifactFormat.TEXT: ("text/plain", "txt"),
}

AnySpec = PlotSpec | GeometrySpec | FreeBodySpec | BlockDiagramSpec | CircuitSpec


@dataclass(frozen=True, slots=True)
class ArtifactOutcome:
    """What was produced, and what it cost. `model_calls` is always zero."""

    ref: ArtifactRef
    model_calls: int = 0
    reused: bool = False


class VisualArtifactService:
    def __init__(self, blobs: BlobStore) -> None:
        self._blobs = blobs

    async def render_and_store(
        self,
        session: AsyncSession,
        *,
        subject_id: UUID,
        conversation_id: UUID | None,
        spec: AnySpec,
        image_format: ArtifactFormat = ArtifactFormat.PNG,
        generated_by: str = "deterministic",
    ) -> ArtifactOutcome:
        """Render, store the bytes, record the metadata. No model call, ever."""
        rendered = self._render(spec, image_format)
        content_type, extension = _CONTENT_TYPES[rendered.artifact_format]

        existing = await self._find(session, subject_id=subject_id, sha256=rendered.sha256)
        if existing is not None:
            return ArtifactOutcome(ref=existing, model_calls=0, reused=True)

        blob = await self._blobs.put(
            subject_id=subject_id,
            data=rendered.data,
            content_type=content_type,
            extension=extension,
            retention_days=ARTIFACT_RETENTION_DAYS,
        )
        artifact_id = await record_artifact(
            session,
            subject_id=subject_id,
            conversation_id=conversation_id,
            kind=str(rendered.kind),
            artifact_format=str(rendered.artifact_format),
            blob_key=blob.key,
            sha256=rendered.sha256,
            width=rendered.width,
            height=rendered.height,
            generated_by=generated_by,
            spec=_spec_payload(spec),
        )
        logger.info(
            "artifact_stored",
            kind=str(rendered.kind),
            artifact_format=str(rendered.artifact_format),
            bytes=len(rendered.data),
            model_calls=0,
        )
        return ArtifactOutcome(
            ref=ArtifactRef(
                id=artifact_id,
                kind=rendered.kind,
                artifact_format=rendered.artifact_format,
                blob_key=blob.key,
                sha256=rendered.sha256,
                width=rendered.width,
                height=rendered.height,
                generated_by=generated_by,
            ),
            model_calls=0,
        )

    @staticmethod
    def _render(spec: AnySpec, image_format: ArtifactFormat) -> RenderedArtifact:
        if isinstance(spec, PlotSpec):
            if image_format is ArtifactFormat.TIKZ:
                return render_plot_tikz(spec)
            return render_plot(spec, image_format=image_format)
        if isinstance(spec, GeometrySpec):
            return render_geometry_svg(spec)
        if isinstance(spec, FreeBodySpec):
            return render_free_body(spec)
        if isinstance(spec, BlockDiagramSpec):
            return render_block_diagram(spec)
        if isinstance(spec, CircuitSpec):
            return render_circuit_svg(spec)
        raise InvalidSpec(f"no deterministic renderer for {type(spec).__name__}")

    async def _find(
        self, session: AsyncSession, *, subject_id: UUID, sha256: str
    ) -> ArtifactRef | None:
        from sqlalchemy import select

        row = (
            await session.execute(
                select(LearningArtifact).where(
                    LearningArtifact.subject_id == subject_id,
                    LearningArtifact.sha256 == sha256,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return ArtifactRef(
            id=row.id,
            kind=ArtifactKind(row.kind),
            artifact_format=ArtifactFormat(row.artifact_format),
            blob_key=row.blob_key,
            sha256=row.sha256,
            width=row.width,
            height=row.height,
            generated_by=row.generated_by,
        )

    async def fetch(self, *, blob_key: str, subject_id: UUID) -> bytes:
        """Ownership is checked by the store as well as by the caller."""
        return await self._blobs.get(blob_key, subject_id=subject_id)


def _spec_payload(spec: AnySpec) -> dict[str, Any]:
    """Store the spec so the same picture is reproducible, not re-derived."""
    return {"type": type(spec).__name__, "spec": spec.model_dump(mode="json")}


__all__ = ["ARTIFACT_RETENTION_DAYS", "ArtifactOutcome", "VisualArtifactService"]
