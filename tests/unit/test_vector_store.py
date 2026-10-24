"""Unit tests for in-memory vector search and record building."""

from __future__ import annotations

import pytest

from app.retrieval.vector import (
    InMemoryVectorStore,
    VectorRecord,
    VectorRetriever,
    build_records,
    stable_point_id,
)
from app.schemas.retrieval import RetrievedChunk


def _chunk(chunk_id: str, content: str = "正文") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        document_id="policy_0",
        title="政策",
        content=content,
        score=0.0,
        source="bm25",
    )


class _FixedEmbedder:
    """Always returns the same vector so cosine scores stay deterministic."""

    def __init__(self, vector: list[float]) -> None:
        self._vector = vector

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(self._vector) for _ in texts]


class _EmptyEmbedder:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return []


# 持久化点标识。


def test_stable_point_id_is_deterministic_uuid() -> None:
    first = stable_point_id("return_policy_0")
    assert first == stable_point_id("return_policy_0")
    assert first != stable_point_id("return_policy_1")
    assert len(first) == 36  # canonical UUID string


# --- InMemoryVectorStore ----------------------------------------------------


def test_in_memory_upsert_replaces_same_chunk_id() -> None:
    store = InMemoryVectorStore()
    store.upsert([VectorRecord(chunk=_chunk("c1", "旧"), embedding=[1.0])])
    store.upsert([VectorRecord(chunk=_chunk("c1", "新"), embedding=[1.0])])

    hits = store.search([1.0], top_k=5)

    assert len(hits) == 1
    assert hits[0].content == "新"


# --- build_records ----------------------------------------------------------


def test_build_records_rejects_length_mismatch() -> None:
    with pytest.raises(ValueError):
        build_records([_chunk("c1")], [])


# --- VectorRetriever --------------------------------------------------------


@pytest.mark.asyncio
async def test_vector_retriever_returns_empty_without_embedding() -> None:
    retriever = VectorRetriever(InMemoryVectorStore(), _EmptyEmbedder())
    assert await retriever.search("退货", top_k=1) == []


@pytest.mark.asyncio
async def test_vector_retriever_ping_delegates_to_store() -> None:
    retriever = VectorRetriever(InMemoryVectorStore(), _FixedEmbedder([1.0]))
    assert await retriever.ping() is True
