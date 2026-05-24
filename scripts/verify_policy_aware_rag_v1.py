#!/usr/bin/env python
"""Verify Policy-aware RAG v1: ACL, tenant, classification, and safe audit payload."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.contracts.adapters.tool_rag import RagSearchAdapter  # noqa: E402
from copilot_agent.contracts.events.registry import validate_payload_for_kind  # noqa: E402
from copilot_agent.contracts.retrieval import RetrievalRequest  # noqa: E402
from copilot_agent.rag.retriever import RagStore  # noqa: E402
from copilot_agent.rag.schema import DocChunk, format_chunks_for_prompt  # noqa: E402
from copilot_agent.runtime.event_schema import EVENT_RETRIEVAL_COMPLETED  # noqa: E402
from copilot_agent.tools.audit import audit_payload_has_secret  # noqa: E402


def _chunk(
    source: str,
    text: str,
    *,
    tenant_id: str = "tenant-a",
    acl: list[str] | None = None,
    classification: str = "internal",
    pii_level: str = "none",
    start_line: int = 1,
) -> DocChunk:
    return DocChunk(
        source=source,
        start_line=start_line,
        text=text,
        section_title=source,
        tenant_id=tenant_id,
        doc_id=source,
        acl=acl or [],
        classification=classification,
        pii_level=pii_level,
        source_hash=f"hash-{source}",
        retention_policy="mvp",
    )


def _mock_vector_index(chunks: list[DocChunk]):
    class _Node:
        def __init__(self, chunk: DocChunk, score: float) -> None:
            self.node = type("MetaNode", (), {"metadata": {
                "source": chunk.source,
                "start_line": chunk.start_line,
                "chunk_id": chunk.chunk_id,
                "tenant_id": chunk.tenant_id,
            }})()
            self.score = score

    class _Retriever:
        def __init__(self) -> None:
            self._nodes = [_Node(chunk, 0.9 - index * 0.1) for index, chunk in enumerate(chunks)]

        def retrieve(self, query: str):
            return self._nodes

    class _Index:
        def as_retriever(self, similarity_top_k: int = 12):
            return _Retriever()

    return _Index()


def _verify_policy_vector_coexistence() -> dict[str, bool]:
    allowed = _chunk(
        "vector-allowed.md",
        "redis stream vector allowed runbook",
        tenant_id="tenant-a",
        acl=["user:alice"],
        start_line=1,
    )
    blocked = _chunk(
        "vector-blocked.md",
        "redis stream vector blocked tenant b",
        tenant_id="tenant-b",
        acl=["user:alice"],
        start_line=2,
    )
    store = RagStore([allowed, blocked], vector_index=_mock_vector_index([allowed, blocked]))
    request = RetrievalRequest(
        tenant_id="tenant-a",
        user_id="alice",
        query="redis stream vector runbook",
        allowed_scopes=["user:alice"],
        max_classification="internal",
        purpose="agent_context",
    )
    detailed, _ = store.policy_aware_search(request, top_k=4)
    vector_scores = store._vector_scores("redis stream vector runbook")
    scoped = RagStore(
        [allowed],
        vector_index=store._vector_index,
        vector_chunk_allowlist={allowed.chunk_id},
        vector_metadata_filter={"tenant_id": "tenant-a"},
    )
    scoped_scores = scoped._vector_scores("redis stream vector runbook")
    return {
        "policy_prefilter_keeps_vector_path": len(detailed.chunks) == 1 and detailed.chunks[0].source == "vector-allowed.md",
        "vector_allowlist_filters_blocked": allowed.key in scoped_scores and blocked.key not in scoped_scores,
        "parent_vector_index_present": store._vector_index is not None and vector_scores.get(allowed.key, 0) > 0,
    }


def main() -> int:
    secret_text = "SECRET_TOKEN_SHOULD_NOT_APPEAR"
    store = RagStore(
        [
            _chunk(
                "allowed.md",
                "redis stream official runbook queue retry policy",
                acl=["user:alice", "group:ops"],
                classification="internal",
                start_line=1,
            ),
            _chunk(
                "other-tenant.md",
                "redis stream tenant b private runbook",
                tenant_id="tenant-b",
                acl=["user:alice"],
                classification="internal",
                start_line=2,
            ),
            _chunk(
                "secret.md",
                f"redis stream confidential secret {secret_text}",
                acl=["user:alice"],
                classification="secret",
                start_line=3,
            ),
            _chunk(
                "acl-denied.md",
                "redis stream finance-only policy",
                acl=["group:finance"],
                classification="internal",
                start_line=4,
            ),
            _chunk(
                "high-pii.md",
                "redis stream user phone number private data",
                acl=["user:alice"],
                classification="internal",
                pii_level="high",
                start_line=5,
            ),
        ]
    )
    request = RetrievalRequest(
        tenant_id="tenant-a",
        user_id="alice",
        query="redis stream runbook policy",
        allowed_scopes=["group:ops"],
        max_classification="internal",
        purpose="agent_context",
    )
    detailed, policy_result = store.policy_aware_search(request, top_k=8)
    context = format_chunks_for_prompt(detailed.chunks)
    payload = RagSearchAdapter.to_retrieval_completed_payload(
        request.query,
        detailed.chunks,
        excerpt_chars=len(context),
        call_id="rag-call-1",
        retrieval_mode=detailed.route.mode,
        retrieval_route=detailed.route.as_dict(),
        policy_result=policy_result,
    )
    validated = validate_payload_for_kind(EVENT_RETRIEVAL_COMPLETED, payload)
    encoded_payload = json.dumps(validated, ensure_ascii=False)

    checks = {
        "allowed_chunk_returned": [chunk.source for chunk in detailed.chunks] == ["allowed.md"],
        "cross_tenant_blocked": any(
            decision.reason == "tenant_mismatch" and decision.doc_id == "other-tenant.md"
            for decision in policy_result.policy_decisions
        ),
        "classification_blocked": any(
            decision.reason == "classification_exceeds_request" and decision.doc_id == "secret.md"
            for decision in policy_result.policy_decisions
        ),
        "acl_blocked": any(
            decision.reason == "acl_denied" and decision.doc_id == "acl-denied.md"
            for decision in policy_result.policy_decisions
        ),
        "high_pii_blocked": any(
            decision.reason == "high_pii_blocked" and decision.doc_id == "high-pii.md"
            for decision in policy_result.policy_decisions
        ),
        "blocked_not_in_context": secret_text not in context and "finance-only" not in context,
        "audit_has_policy_trace": bool(validated.get("policy_trace_id")),
        "audit_has_no_raw_secret": secret_text not in encoded_payload and not audit_payload_has_secret(validated),
        "query_hash_present": bool(validated.get("query_hash")) and request.query not in str(validated.get("query_hash")),
        "audit_has_tenant_id": validated.get("tenant_id") == "tenant-a",
    }

    vector_checks = _verify_policy_vector_coexistence()
    checks.update(vector_checks)
    overall = all(checks.values())
    print(f"checks={json.dumps(checks, ensure_ascii=False, sort_keys=True)}")
    print(f"blocked_count={policy_result.blocked_count}")
    print(f"prefilter_blocked_count={policy_result.prefilter_blocked_count}")
    print(f"allowed_chunk_ids={','.join(policy_result.allowed_chunk_ids)}")
    print(f"policy_aware_rag_v1={'PASS' if overall else 'FAIL'}")
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
