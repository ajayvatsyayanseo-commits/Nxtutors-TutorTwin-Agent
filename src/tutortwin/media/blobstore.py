"""Object storage behind one abstraction.

Two implementations: a filesystem fake for tests and local development, and a
Cloudflare R2 adapter for production. R2 speaks an S3-compatible protocol, but
that is an implementation detail confined to this module - nothing outside it
imports boto3 or knows the bucket exists.

Objects are **content-addressed**: the key is derived from the SHA-256 of the
bytes plus the owning subject. Two consequences worth having:

* re-uploading identical content is free and idempotent
* the same file sent by two students is stored twice, deliberately - a shared
  key would let one student's deletion affect another's, and would make an
  ownership check depend on knowing every uploader
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from tutortwin.observability.logging import get_logger

logger = get_logger(__name__)

DEFAULT_RETENTION_DAYS = 7
"""Raw student uploads are transient. The extraction is what has lasting value;
the original bytes are a liability that ages badly."""


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_key(subject_id: UUID, digest: str, extension: str = "") -> str:
    """`media/<subject>/<aa>/<full-digest><ext>`.

    The two-character shard keeps any single prefix from accumulating millions
    of objects, which some stores handle poorly.
    """
    suffix = f".{extension.lstrip('.')}" if extension else ""
    return f"media/{subject_id}/{digest[:2]}/{digest}{suffix}"


@dataclass(frozen=True, slots=True)
class BlobMetadata:
    key: str
    size: int
    content_type: str
    sha256: str
    subject_id: UUID
    stored_at: datetime
    expires_at: datetime | None = None

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and datetime.now(UTC) >= self.expires_at


class BlobStore(Protocol):
    """Every object is private. Access is by signed URL or backend streaming."""

    async def put(
        self,
        *,
        subject_id: UUID,
        data: bytes,
        content_type: str,
        extension: str = "",
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> BlobMetadata: ...

    async def get(self, key: str, *, subject_id: UUID) -> bytes: ...

    async def signed_url(self, key: str, *, subject_id: UUID, expires_in_seconds: int) -> str: ...

    async def delete(self, key: str, *, subject_id: UUID) -> None: ...

    async def exists(self, key: str) -> bool: ...


class BlobNotFound(KeyError):
    pass


class BlobAccessDenied(PermissionError):
    """A subject asked for an object that is not theirs."""


def _assert_owner(key: str, subject_id: UUID) -> None:
    """Ownership is encoded in the key, so it is checkable without a lookup.

    This is the last line of defence, not the only one - callers check ownership
    at the repository layer too - but it means a bug upstream cannot turn into
    cross-student data access at the storage layer.
    """
    if not key.startswith(f"media/{subject_id}/"):
        raise BlobAccessDenied("Object does not belong to this subject.")


@dataclass(slots=True)
class FilesystemBlobStore:
    """Local fake. Same semantics as R2, including ownership and expiry."""

    root: Path
    _meta: dict[str, BlobMetadata] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # The key is server-generated, but treat it as untrusted anyway: a
        # traversal here would write outside the store.
        resolved = (self.root / key).resolve()
        if not str(resolved).startswith(str(self.root.resolve())):
            raise BlobAccessDenied("Key escapes the store root.")
        return resolved

    async def put(
        self,
        *,
        subject_id: UUID,
        data: bytes,
        content_type: str,
        extension: str = "",
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> BlobMetadata:
        digest = content_hash(data)
        key = build_key(subject_id, digest, extension)
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

        now = datetime.now(UTC)
        meta = BlobMetadata(
            key=key,
            size=len(data),
            content_type=content_type,
            sha256=digest,
            subject_id=subject_id,
            stored_at=now,
            expires_at=now + timedelta(days=retention_days) if retention_days else None,
        )
        self._meta[key] = meta
        return meta

    async def get(self, key: str, *, subject_id: UUID) -> bytes:
        _assert_owner(key, subject_id)
        path = self._path(key)
        if not path.exists():
            raise BlobNotFound(key)
        return path.read_bytes()

    async def signed_url(self, key: str, *, subject_id: UUID, expires_in_seconds: int) -> str:
        _assert_owner(key, subject_id)
        if not self._path(key).exists():
            raise BlobNotFound(key)
        return f"file://{self._path(key)}?expires_in={expires_in_seconds}"

    async def delete(self, key: str, *, subject_id: UUID) -> None:
        _assert_owner(key, subject_id)
        path = self._path(key)
        if path.exists():
            path.unlink()
        self._meta.pop(key, None)

    async def exists(self, key: str) -> bool:
        return self._path(key).exists()


@dataclass(slots=True)
class R2BlobStore:
    """Cloudflare R2. Private bucket, signed short-lived access, no public read.

    boto3 is imported lazily so a deployment without R2 configured never loads
    it, and so the test suite does not depend on it.
    """

    account_id: str
    access_key_id: str
    secret_access_key: str
    bucket: str
    _client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
                "s3",
                endpoint_url=f"https://{self.account_id}.r2.cloudflarestorage.com",
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
                # R2 ignores regions but the SDK requires one.
                region_name="auto",
                config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
            )
        return self._client

    async def put(
        self,
        *,
        subject_id: UUID,
        data: bytes,
        content_type: str,
        extension: str = "",
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> BlobMetadata:
        import asyncio

        digest = content_hash(data)
        key = build_key(subject_id, digest, extension)
        now = datetime.now(UTC)
        expires_at = now + timedelta(days=retention_days) if retention_days else None

        client = self._get_client()
        # boto3 is synchronous; keep it off the event loop.
        await asyncio.to_thread(
            client.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            # Retention is also enforced by a bucket lifecycle rule; this
            # metadata makes an object's intended expiry self-describing.
            Metadata={
                "subject-id": str(subject_id),
                "sha256": digest,
                "expires-at": expires_at.isoformat() if expires_at else "",
            },
        )
        return BlobMetadata(
            key=key,
            size=len(data),
            content_type=content_type,
            sha256=digest,
            subject_id=subject_id,
            stored_at=now,
            expires_at=expires_at,
        )

    async def get(self, key: str, *, subject_id: UUID) -> bytes:
        import asyncio

        _assert_owner(key, subject_id)
        client = self._get_client()
        try:
            response = await asyncio.to_thread(
                client.get_object,
                Bucket=self.bucket,
                Key=key,
            )
        except Exception as exc:  # noqa: BLE001 - vendor exception tree
            raise BlobNotFound(key) from exc
        body: bytes = await asyncio.to_thread(response["Body"].read)
        return body

    async def signed_url(self, key: str, *, subject_id: UUID, expires_in_seconds: int) -> str:
        import asyncio

        _assert_owner(key, subject_id)
        client = self._get_client()
        url: str = await asyncio.to_thread(
            client.generate_presigned_url,
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires_in_seconds,
        )
        return url

    async def delete(self, key: str, *, subject_id: UUID) -> None:
        import asyncio

        _assert_owner(key, subject_id)
        client = self._get_client()
        await asyncio.to_thread(
            client.delete_object,
            Bucket=self.bucket,
            Key=key,
        )

    async def exists(self, key: str) -> bool:
        import asyncio

        client = self._get_client()
        try:
            await asyncio.to_thread(
                client.head_object,
                Bucket=self.bucket,
                Key=key,
            )
        except Exception:  # noqa: BLE001
            return False
        return True
