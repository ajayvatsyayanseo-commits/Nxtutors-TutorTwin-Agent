"""Retention sweeper.

R2 has a lifecycle rule that reclaims the bytes, and that rule is what actually
frees storage. This sweeps the *rows*: an expired `media_objects` row whose blob
the bucket already deleted is a dangling pointer that makes the control plane
show a document nobody can open, and an expired `media_extractions` row is
student content kept past the retention promise.

Three properties:

**Idempotent.** Running it twice deletes nothing the second time. Cloud Scheduler
retries, and a sweeper that is not idempotent turns a retry into a wider delete.

**Bounded.** One call deletes at most `batch_size` objects. A sweep that runs for
an hour holds a Postgres connection for an hour, and Cloud Run will kill it
somewhere in the middle with no record of how far it got.

**Blob-first.** The blob is deleted before the row that points at it. The other
order leaves an orphaned object nothing references, which no later sweep can
find - it would have to list the bucket, and the bucket is not the index.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tutortwin.db.models import MediaExtraction, MediaObject
from tutortwin.media.blobstore import BlobStore
from tutortwin.observability.logging import get_logger

logger = get_logger(__name__)

DEFAULT_BATCH = 200


@dataclass(frozen=True, slots=True)
class SweepResult:
    blobs_deleted: int = 0
    media_rows_deleted: int = 0
    extractions_deleted: int = 0
    blob_errors: int = 0

    @property
    def total(self) -> int:
        return self.blobs_deleted + self.media_rows_deleted + self.extractions_deleted


async def sweep(
    session: AsyncSession,
    *,
    blobstore: BlobStore,
    now: datetime | None = None,
    batch_size: int = DEFAULT_BATCH,
) -> SweepResult:
    """Delete expired media and its extracted text.

    Extractions go with their media object rather than on their own clock: text
    extracted from a document *is* the document, and keeping the words after
    deleting the file would make the retention promise a technicality.
    """
    moment = (now or datetime.now(UTC)).astimezone(UTC)

    expired = list(
        (
            await session.execute(
                select(MediaObject)
                .where(MediaObject.expires_at.is_not(None), MediaObject.expires_at <= moment)
                .order_by(MediaObject.expires_at)
                .limit(batch_size)
            )
        ).scalars()
    )
    if not expired:
        return SweepResult()

    blobs_deleted = 0
    blob_errors = 0
    extractions = 0

    for media in expired:
        if media.blob_key:
            try:
                await blobstore.delete(media.blob_key, subject_id=media.subject_id)
                blobs_deleted += 1
            except FileNotFoundError:
                # The bucket lifecycle rule got there first. That is the normal
                # case, not an error: the row is still ours to remove.
                blobs_deleted += 1
            except Exception as exc:
                # Leave the row. A row whose blob is still present is retryable;
                # deleting it anyway would strand the object permanently.
                blob_errors += 1
                logger.warning(
                    "retention_blob_delete_failed",
                    media_id=str(media.id),
                    error_type=type(exc).__name__,
                )
                continue

        if media.sha256:
            # Counted with a SELECT rather than read off the DELETE: `rowcount`
            # is driver-dependent on an async result, and a retention report that
            # lies about how much it removed is worse than one extra query in a
            # job nobody is waiting on.
            doomed = (
                await session.execute(
                    select(func.count())
                    .select_from(MediaExtraction)
                    .where(
                        MediaExtraction.subject_id == media.subject_id,
                        MediaExtraction.sha256 == media.sha256,
                    )
                )
            ).scalar_one()
            await session.execute(
                delete(MediaExtraction).where(
                    MediaExtraction.subject_id == media.subject_id,
                    MediaExtraction.sha256 == media.sha256,
                )
            )
            extractions += int(doomed or 0)

        await session.delete(media)

    await session.commit()

    outcome = SweepResult(
        blobs_deleted=blobs_deleted,
        media_rows_deleted=len(expired) - blob_errors,
        extractions_deleted=extractions,
        blob_errors=blob_errors,
    )
    logger.info(
        "retention_swept",
        blobs=outcome.blobs_deleted,
        media_rows=outcome.media_rows_deleted,
        extractions=outcome.extractions_deleted,
        errors=outcome.blob_errors,
    )
    return outcome


__all__ = ["DEFAULT_BATCH", "SweepResult", "sweep"]
