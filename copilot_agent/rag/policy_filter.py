from __future__ import annotations

from copilot_agent.contracts.retrieval import (
    RetrievalPolicyDecision,
    RetrievalRequest,
    RetrievalResult,
    classification_allowed,
)
from copilot_agent.rag.schema import DocChunk


class RagPolicyFilter:
    """Deterministic metadata policy for Policy-aware RAG v1."""

    def pre_filter(self, chunks: list[DocChunk], request: RetrievalRequest) -> list[DocChunk]:
        return [chunk for chunk in chunks if self.evaluate(chunk, request).allowed]

    def pre_filter_with_decisions(
        self,
        chunks: list[DocChunk],
        request: RetrievalRequest,
    ) -> tuple[list[DocChunk], list[RetrievalPolicyDecision]]:
        allowed: list[DocChunk] = []
        decisions: list[RetrievalPolicyDecision] = []
        for chunk in chunks:
            decision = self.evaluate(chunk, request)
            decisions.append(decision)
            if decision.allowed:
                allowed.append(chunk)
        return allowed, decisions

    def post_filter(self, chunks: list[DocChunk], request: RetrievalRequest) -> RetrievalResult:
        decisions: list[RetrievalPolicyDecision] = []
        allowed: list[DocChunk] = []
        blocked_ids: list[str] = []
        retrieved_ids = [chunk.chunk_id for chunk in chunks]
        for chunk in chunks:
            decision = self.evaluate(chunk, request)
            decisions.append(decision)
            if decision.allowed:
                allowed.append(chunk)
            else:
                blocked_ids.append(chunk.chunk_id)
        return RetrievalResult(
            request=request,
            allowed_chunks=allowed,
            blocked_count=len(blocked_ids),
            retrieved_chunk_ids=retrieved_ids,
            allowed_chunk_ids=[chunk.chunk_id for chunk in allowed],
            blocked_chunk_ids=blocked_ids,
            policy_decisions=decisions,
        )

    def evaluate(self, chunk: DocChunk, request: RetrievalRequest) -> RetrievalPolicyDecision:
        scopes = set(request.allowed_scopes or [])
        if request.user_id:
            scopes.add(f"user:{request.user_id}")
        if request.tenant_id:
            scopes.add(f"tenant:{request.tenant_id}")

        reason = "allowed"
        allowed = True
        if chunk.tenant_id != request.tenant_id:
            allowed = False
            reason = "tenant_mismatch"
        elif not classification_allowed(chunk.classification, request.max_classification):
            allowed = False
            reason = "classification_exceeds_request"
        elif chunk.pii_level == "high" and not request.allow_high_pii:
            allowed = False
            reason = "high_pii_blocked"
        elif chunk.acl and not scopes.intersection(chunk.acl):
            allowed = False
            reason = "acl_denied"

        return RetrievalPolicyDecision(
            allowed=allowed,
            reason=reason,
            tenant_id=chunk.tenant_id,
            doc_id=chunk.doc_id,
            chunk_id=chunk.chunk_id,
            classification=chunk.classification,
            pii_level=chunk.pii_level,
            acl=list(chunk.acl),
        )
