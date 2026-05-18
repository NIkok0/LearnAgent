"""Backward-compatible facade; prefer `build_rag_store()` and `RagStore.search()`."""

from __future__ import annotations

from copilot_agent.rag.ingest import load_chunks
from copilot_agent.rag.keyword import keyword_search
from copilot_agent.rag.retriever import RagStore, build_rag_store
from copilot_agent.rag.schema import DocChunk, format_chunks_for_prompt

__all__ = [
    "DocChunk",
    "RagStore",
    "build_rag_store",
    "format_chunks_for_prompt",
    "load_and_chunk",
    "search_docs",
]


def load_and_chunk(*args, **kwargs) -> list[DocChunk]:
    return load_chunks(*args, **kwargs)


def search_docs(chunks: list[DocChunk], query: str, top_k: int = 6) -> list[DocChunk]:
    """Keyword-only search (legacy). Use `RagStore.search` for hybrid retrieval."""
    return keyword_search(chunks, query, top_k=top_k)
