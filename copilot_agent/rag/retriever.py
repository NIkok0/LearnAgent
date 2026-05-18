from __future__ import annotations

import logging
from typing import Any

from copilot_agent.rag.index import build_vector_index
from copilot_agent.rag.ingest import load_chunks
from copilot_agent.rag.keyword import keyword_search, keyword_scores
from copilot_agent.rag.schema import DocChunk
from copilot_agent.settings import settings

log = logging.getLogger(__name__)


class RagStore:
    """Hybrid RAG: keyword (always) + LlamaIndex vector retrieval (when available)."""

    def __init__(
        self,
        chunks: list[DocChunk],
        *,
        vector_index: Any | None = None,
    ) -> None:
        self.chunks = chunks
        self._by_key = {c.key: c for c in chunks}
        self._vector_index = vector_index
        self._retriever = None
        if vector_index is not None:
            try:
                self._retriever = vector_index.as_retriever(
                    similarity_top_k=max(settings.rag_vector_top_k, 12)
                )
            except Exception:
                log.exception("Failed to create vector retriever")
                self._retriever = None

    @property
    def vector_enabled(self) -> bool:
        return self._retriever is not None

    def search(self, query: str, top_k: int = 6) -> list[DocChunk]:
        if not self.chunks:
            return []
        if not query.strip():
            return self.chunks[:top_k]

        kw = keyword_scores(self.chunks, query)
        vec = self._vector_scores(query)

        if not kw and not vec:
            return keyword_search(self.chunks, query, top_k=top_k)

        keys = set(kw.keys()) | set(vec.keys())
        fused: list[tuple[float, DocChunk]] = []
        kw_w = settings.rag_keyword_weight
        vec_w = settings.rag_vector_weight
        for key in keys:
            chunk = self._by_key.get(key)
            if chunk is None:
                continue
            score = kw_w * kw.get(key, 0.0) + vec_w * vec.get(key, 0.0)
            fused.append((score, chunk))
        fused.sort(key=lambda x: (-x[0], x[1].source, x[1].start_line))
        out = [c for s, c in fused if s > 0][:top_k]
        if out:
            return out
        return keyword_search(self.chunks, query, top_k=top_k)

    def _vector_scores(self, query: str) -> dict[tuple[str, int], float]:
        if self._retriever is None:
            return {}
        try:
            nodes = self._retriever.retrieve(query)
        except Exception:
            log.exception("Vector retrieval failed")
            return {}
        raw: dict[tuple[str, int], float] = {}
        for n in nodes:
            meta = getattr(n.node, "metadata", None) or {}
            source = str(meta.get("source", ""))
            start_line = int(meta.get("start_line", 0) or 0)
            if not source:
                continue
            key = (source, start_line)
            score = float(n.score or 0.0)
            raw[key] = max(raw.get(key, 0.0), score)
        if not raw:
            return {}
        max_s = max(raw.values()) or 1.0
        return {k: v / max_s for k, v in raw.items()}


def build_rag_store() -> RagStore:
    chunks = load_chunks()
    vector_index = build_vector_index(chunks) if chunks else None
    store = RagStore(chunks, vector_index=vector_index)
    log.info(
        "RAG store ready: chunks=%d vector=%s model=%s",
        len(chunks),
        store.vector_enabled,
        settings.rag_embedding_model if store.vector_enabled else "n/a",
    )
    return store
