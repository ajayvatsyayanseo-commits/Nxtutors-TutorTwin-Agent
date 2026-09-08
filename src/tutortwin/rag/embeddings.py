"""Embedding providers and the content-addressed embedding cache.

Embeddings are the one paid call RAG makes on the hot path, so the cache is not
an optimisation - it is the difference between paying once per distinct passage
and paying once per upload. Keyed by normalized-text hash + model, identical
text is never embedded twice.

Batching matters too: a hundred chunks in one request costs one round trip, not
a hundred.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tutortwin.db.knowledge_models import EmbeddingCache
from tutortwin.observability.logging import get_logger
from tutortwin.rag.chunking import normalize_for_hash

logger = get_logger(__name__)

DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_DIMENSIONS = 1536

MAX_BATCH = 96
"""Vendors cap batch size and request bytes; 96 stays comfortably inside both."""


def content_key(text: str) -> str:
    return hashlib.sha256(normalize_for_hash(text).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    vectors: tuple[tuple[float, ...], ...]
    model: str
    api_calls: int
    cache_hits: int
    texts_embedded: int


class EmbeddingProvider(Protocol):
    @property
    def model(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]: ...


@dataclass(slots=True)
class DeterministicEmbeddingProvider:
    """Hash-based pseudo-embeddings for tests and offline development.

    Not semantically meaningful, but *stable and self-consistent*: the same text
    always yields the same vector, and near-identical texts share leading
    components, so ranking behaviour is exercised without a vendor key. Every
    call is counted so cost tests can assert on it.
    """

    model: str = "deterministic-v1"
    dimensions: int = 64
    calls: int = 0
    texts_seen: list[str] = field(default_factory=list)

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        self.calls += 1
        self.texts_seen.extend(texts)
        return tuple(self._vector(t) for t in texts)

    def _vector(self, text: str) -> tuple[float, ...]:
        # Token-level hashing so texts sharing words share direction, which is
        # what makes the ranking assertions meaningful rather than arbitrary.
        vector = [0.0] * self.dimensions
        for token in normalize_for_hash(text).split():
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            vector[index] += 1.0
        norm = sum(v * v for v in vector) ** 0.5
        if norm == 0:
            vector[0] = 1.0
            norm = 1.0
        return tuple(v / norm for v in vector)


class OpenAIEmbeddingProvider:
    """The configured default. OpenAI is the only embedding vendor in use."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_EMBEDDING_MODEL,
        dimensions: int = DEFAULT_DIMENSIONS,
    ) -> None:
        import openai

        self._client = openai.AsyncOpenAI(api_key=api_key)
        self._model = model
        self._dimensions = dimensions
        self.calls = 0

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        if not texts:
            return ()
        vectors: list[tuple[float, ...]] = []
        for start in range(0, len(texts), MAX_BATCH):
            batch = texts[start : start + MAX_BATCH]
            self.calls += 1
            response = await self._client.embeddings.create(model=self._model, input=list(batch))
            vectors.extend(tuple(item.embedding) for item in response.data)
        return tuple(vectors)


async def embed_with_cache(
    session: AsyncSession,
    provider: EmbeddingProvider,
    texts: tuple[str, ...],
) -> EmbeddingResult:
    """Embed only what is not already cached.

    Order is preserved, so callers can zip results back against their input.
    Duplicate texts within one call are embedded once.
    """
    if not texts:
        return EmbeddingResult((), provider.model, 0, 0, 0)

    keys = [content_key(t) for t in texts]
    unique_keys = list(dict.fromkeys(keys))

    cached_rows = (
        await session.execute(
            select(EmbeddingCache).where(
                EmbeddingCache.normalized_sha256.in_(unique_keys),
                EmbeddingCache.embedding_model == provider.model,
            )
        )
    ).scalars()
    cache: dict[str, list[float]] = {
        row.normalized_sha256: list(row.embedding_json) for row in cached_rows
    }

    missing_keys = [k for k in unique_keys if k not in cache]
    key_to_text = dict(zip(keys, texts, strict=True))
    to_embed = tuple(key_to_text[k] for k in missing_keys)

    api_calls = 0
    if to_embed:
        before = getattr(provider, "calls", 0)
        fresh = await provider.embed(to_embed)
        api_calls = getattr(provider, "calls", 0) - before or 1

        for key, vector in zip(missing_keys, fresh, strict=True):
            cache[key] = list(vector)
            await session.execute(
                pg_insert(EmbeddingCache)
                .values(
                    normalized_sha256=key,
                    embedding_model=provider.model,
                    embedding_json=list(vector),
                    dimensions=len(vector),
                )
                # A concurrent request may have inserted the same key first;
                # either vector is correct, so the race is benign.
                .on_conflict_do_nothing(
                    index_elements=[
                        EmbeddingCache.normalized_sha256,
                        EmbeddingCache.embedding_model,
                    ]
                )
            )

    logger.info(
        "embeddings_resolved",
        requested=len(texts),
        cache_hits=len(unique_keys) - len(missing_keys),
        embedded=len(missing_keys),
        api_calls=api_calls,
    )
    return EmbeddingResult(
        vectors=tuple(tuple(cache[k]) for k in keys),
        model=provider.model,
        api_calls=api_calls,
        cache_hits=len(unique_keys) - len(missing_keys),
        texts_embedded=len(missing_keys),
    )
