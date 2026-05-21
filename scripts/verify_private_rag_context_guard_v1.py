#!/usr/bin/env python
"""Verify Private RAG Context Guard v1."""

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
from copilot_agent.rag.context_guard import (  # noqa: E402
    UNTRUSTED_RAG_CONTEXT_HEADER,
    build_guarded_context,
    detect_sensitive_output,
)
from copilot_agent.rag.retriever import RagStore  # noqa: E402
from copilot_agent.rag.schema import DocChunk  # noqa: E402
from copilot_agent.runtime.event_schema import EVENT_RETRIEVAL_COMPLETED  # noqa: E402


def main() -> int:
    chunks = [
        DocChunk(
            source="allowed.md",
            start_line=1,
            text="official policy: cite this source. " + ("a" * 500),
            tenant_id="default",
            doc_id="allowed.md",
            acl=["user:alice"],
            classification="internal",
        ),
        DocChunk(
            source="large.md",
            start_line=2,
            text="large allowed document. " + ("b" * 5000),
            tenant_id="default",
            doc_id="large.md",
            acl=["user:alice"],
            classification="internal",
        ),
        DocChunk(
            source="secret.md",
            start_line=3,
            text="secret document with TOKEN_SHOULD_NOT_ENTER_CONTEXT",
            tenant_id="default",
            doc_id="secret.md",
            acl=["user:alice"],
            classification="secret",
        ),
    ]
    request = RetrievalRequest(
        tenant_id="default",
        user_id="alice",
        query="official policy source",
        allowed_scopes=["user:alice"],
        max_classification="internal",
    )
    detailed, policy_result = RagStore(chunks).policy_aware_search(request, top_k=5)
    guarded = build_guarded_context(detailed.chunks, max_chars=900, require_citations=True)
    payload = RagSearchAdapter.to_retrieval_completed_payload(
        request.query,
        guarded.chunks,
        excerpt_chars=len(guarded.markdown),
        call_id="guard-call-1",
        retrieval_mode=detailed.route.mode,
        retrieval_route=detailed.route.as_dict(),
        policy_result=policy_result,
        context_guard=guarded.audit_payload(),
    )
    validated = validate_payload_for_kind(EVENT_RETRIEVAL_COMPLETED, payload)
    safe_output = detect_sensitive_output("Answer based on allowed.md.")
    unsafe_output = detect_sensitive_output("Leaked sk-1234567890abcdef and set-cookie: a=b")
    checks = {
        "header_marks_untrusted": guarded.markdown.startswith(UNTRUSTED_RAG_CONTEXT_HEADER),
        "budget_respected": guarded.used_chars <= guarded.budget_chars <= 900,
        "citations_required": guarded.require_citations is True,
        "secret_chunk_blocked": "TOKEN_SHOULD_NOT_ENTER_CONTEXT" not in guarded.markdown,
        "context_guard_in_audit": validated.get("context_guard", {}).get("context_guard") == "private_rag_v1",
        "audit_source_ids_match_context": validated.get("context_guard", {}).get("source_ids") == guarded.source_ids,
        "safe_output_passes": safe_output.get("safe") is True,
        "unsafe_output_detected": unsafe_output.get("safe") is False and int(unsafe_output.get("finding_count") or 0) >= 1,
    }
    passed = all(checks.values())
    print(f"checks={json.dumps(checks, ensure_ascii=False, sort_keys=True)}")
    print(f"context_guard={json.dumps(guarded.audit_payload(), ensure_ascii=False, sort_keys=True)}")
    print(f"private_rag_context_guard_v1={'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
