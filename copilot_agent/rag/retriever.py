from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from copilot_agent.contracts.retrieval import RetrievalRequest, RetrievalResult
from copilot_agent.rag.bm25 import BM25Index
from copilot_agent.rag.fusion import apply_doc_type_boost, dedup_chunks, rank_from_scores, rrf_fuse
from copilot_agent.rag.index import build_vector_index, sync_vector_index
from copilot_agent.rag.ingest import load_chunks
from copilot_agent.rag.keyword import keyword_search, keyword_scores
from copilot_agent.rag.policy_filter import RagPolicyFilter
from copilot_agent.rag.rerank import rerank_chunks
from copilot_agent.rag.query_rewrite import query_doc_type_hints, rewrite_query
from copilot_agent.rag.query_router import RetrievalRoute, route_query
from copilot_agent.rag.schema import DocChunk
from copilot_agent.settings import settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RagSearchResult:
    chunks: list[DocChunk]
    route: RetrievalRoute
    search_query: str


class RagStore:
    """Hybrid RAG: query-routed sparse (keyword + BM25) + optional vector, fused with RRF."""

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
        self._bm25: BM25Index | None = BM25Index(chunks) if chunks else None
        self._policy_filter = RagPolicyFilter()
        self._bind_retriever(vector_index)

    def _bind_retriever(self, vector_index: Any | None) -> None:
        self._retriever = None
        if vector_index is not None:
            try:
                self._retriever = vector_index.as_retriever(
                    similarity_top_k=max(settings.rag_vector_top_k, 12)
                )
            except Exception:
                log.exception("Failed to create vector retriever")

    @property
    def vector_enabled(self) -> bool:
        return self._retriever is not None

    def replace_chunks(self, chunks: list[DocChunk]) -> None:
        """Hot reload keyword corpus without replacing the store object."""
        self.chunks = chunks
        self._by_key = {c.key: c for c in chunks}
        self._bm25 = BM25Index(chunks) if chunks else None

    def update_vector_index(self, vector_index: Any | None) -> None:
        """Swap vector retriever after async or incremental rebuild."""
        self._vector_index = vector_index
        self._bind_retriever(vector_index)

    def search(self, query: str, top_k: int = 6) -> list[DocChunk]:
        return self.search_detailed(query, top_k=top_k).chunks

    def search_detailed(self, query: str, top_k: int = 6) -> RagSearchResult:
        if not self.chunks:
            route = route_query(query, vector_available=self.vector_enabled)
            return RagSearchResult(chunks=[], route=route, search_query=query)

        if not query.strip():
            route = route_query(query, vector_available=self.vector_enabled)
            return RagSearchResult(chunks=self.chunks[:top_k], route=route, search_query=query)

        search_query = rewrite_query(query) if settings.rag_query_rewrite_enabled else query
        if settings.rag_query_route_enabled:
            route = route_query(query, vector_available=self.vector_enabled)
        else:
            vec_w = settings.rag_vector_weight if self.vector_enabled else 0.0
            route = RetrievalRoute(
                mode="hybrid",
                keyword_weight=settings.rag_keyword_weight,
                bm25_weight=settings.rag_bm25_weight,
                vector_weight=vec_w,
                sparse_signals=(),
                dense_signals=(),
            )

        candidate_k = max(top_k, top_k * settings.rag_fusion_candidate_multiplier)
        pool_k = (
            max(candidate_k, settings.rag_rerank_candidates)
            if settings.rag_rerank_enabled
            else candidate_k
        )
        kw = keyword_scores(self.chunks, search_query)
        bm25 = self._bm25_scores(search_query) if settings.rag_use_bm25 else {}
        vec = self._vector_scores(search_query) if route.vector_weight > 0 else {}

        if not kw and not bm25 and not vec:
            fallback = keyword_search(self.chunks, search_query, top_k=pool_k)
            chunks = self._finalize(fallback, top_k, query=search_query)
            return RagSearchResult(chunks=chunks, route=route, search_query=search_query)

        if settings.rag_use_rrf:
            rankings: list[dict[tuple[str, int], int]] = []
            weights: list[float] = []
            if kw:
                rankings.append(rank_from_scores(kw))
                weights.append(route.keyword_weight)
            if bm25:
                rankings.append(rank_from_scores(bm25))
                weights.append(route.bm25_weight)
            if vec:
                rankings.append(rank_from_scores(vec))
                weights.append(route.vector_weight)
            fused = rrf_fuse(rankings, weights=weights, k=settings.rag_rrf_k)
        else:
            keys = set(kw.keys()) | set(bm25.keys()) | set(vec.keys())
            fused = {
                key: (
                    route.keyword_weight * kw.get(key, 0.0)
                    + route.bm25_weight * bm25.get(key, 0.0)
                    + route.vector_weight * vec.get(key, 0.0)
                )
                for key in keys
            }

        hints = query_doc_type_hints(query) if settings.rag_doc_type_boost_enabled else {}
        fused = apply_doc_type_boost(
            fused,
            self._by_key,
            query_hints=hints,
            enabled=settings.rag_doc_type_boost_enabled,
        )

        ordered = sorted(fused.items(), key=lambda x: (-x[1], x[0][0], x[0][1]))
        out: list[DocChunk] = []
        for key, _ in ordered:
            chunk = self._by_key.get(key)
            if chunk is not None:
                out.append(chunk)
            if len(out) >= pool_k:
                break

        if not out:
            fallback = keyword_search(self.chunks, search_query, top_k=pool_k)
            chunks = self._finalize(fallback, top_k, query=search_query)
            return RagSearchResult(chunks=chunks, route=route, search_query=search_query)

        chunks = self._finalize(out, top_k, query=search_query)
        log.debug(
            "RAG search route mode=%s kw=%.2f bm25=%.2f vec=%.2f signals=%s",
            route.mode,
            route.keyword_weight,
            route.bm25_weight,
            route.vector_weight,
            route.sparse_signals + route.dense_signals,
        )
        return RagSearchResult(chunks=chunks, route=route, search_query=search_query)

    def _finalize(self, chunks: list[DocChunk], top_k: int, *, query: str = "") -> list[DocChunk]:
        if settings.rag_dedup_results:
            chunks = dedup_chunks(chunks)
        if settings.rag_rerank_enabled and query.strip():
            chunks = rerank_chunks(query, chunks, top_k=top_k)
        else:
            chunks = chunks[:top_k]
        return chunks

    def policy_aware_search(self, request: RetrievalRequest, top_k: int = 6) -> tuple[RagSearchResult, RetrievalResult]:
        """Search with metadata pre-filter and post-filter.

        The base ranking pipeline is reused on a temporary filtered store so legacy
        search behavior remains unchanged for callers that do not pass a policy.
        """
        prefiltered, pre_decisions = self._policy_filter.pre_filter_with_decisions(self.chunks, request)
        if len(prefiltered) == len(self.chunks):
            detailed = self.search_detailed(request.query, top_k=top_k)
        else:
            scoped = RagStore(prefiltered, vector_index=None)
            detailed = scoped.search_detailed(request.query, top_k=top_k)
        result = self._policy_filter.post_filter(detailed.chunks, request)
        pre_blocked = [decision for decision in pre_decisions if not decision.allowed]
        result = result.model_copy(
            update={
                "prefilter_blocked_chunk_ids": [decision.chunk_id for decision in pre_blocked],
                "prefilter_blocked_count": len(pre_blocked),
                "blocked_count": result.blocked_count + len(pre_blocked),
                "policy_decisions": [*pre_blocked, *result.policy_decisions],
            }
        )
        return (
            RagSearchResult(chunks=result.allowed_chunks, route=detailed.route, search_query=detailed.search_query),
            result,
        )

    def _bm25_scores(self, query: str) -> dict[tuple[str, int], float]:
        if self._bm25 is None:
            return {}
        return self._bm25.scores(query)

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


def build_rag_store(*, sync_vector: bool = True) -> RagStore:
    chunks = load_chunks()
    vector_index = None
    if chunks and settings.rag_use_vector and sync_vector:
        vector_index = build_vector_index(chunks)
    store = RagStore(chunks, vector_index=vector_index)
    log.info(
        "RAG store ready: chunks=%d vector=%s bm25=%s rrf=%s route=%s rewrite=%s rerank=%s model=%s",
        len(chunks),
        store.vector_enabled,
        settings.rag_use_bm25,
        settings.rag_use_rrf,
        settings.rag_query_route_enabled,
        settings.rag_query_rewrite_enabled,
        settings.rag_rerank_enabled,
        settings.rag_embedding_model if settings.rag_use_vector else "n/a",
    )
    return store


def sync_rag_store_vectors(store: RagStore) -> dict[str, Any]:
    """Run incremental vector sync for an existing store's keyword chunks."""
    result = sync_vector_index(store.chunks)
    store.update_vector_index(result.index)
    return {
        "skipped": result.skipped,
        "changed_files": list(result.delta.changed),
        "removed_files": list(result.delta.removed),
        "upserted_files": result.upserted_files,
        "upserted_chunks": result.upserted_chunks,
        "vector_enabled": store.vector_enabled,
    }
