#!/usr/bin/env python
"""Verify credential + scenario rag scopes flow into policy-aware retrieval."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("SCENARIO", "watermark")

from copilot_agent.contracts.retrieval import RetrievalRequest  # noqa: E402
from copilot_agent.credentials import CredentialManager  # noqa: E402
from copilot_agent.rag.request_context import merge_retrieval_scopes, retrieval_defaults_from_scenario  # noqa: E402
from copilot_agent.rag.retriever import RagStore  # noqa: E402
from copilot_agent.rag.schema import DocChunk  # noqa: E402
from copilot_agent.scenario import load_scenario  # noqa: E402
from copilot_agent.scenario.bootstrap import apply_scenario_environment  # noqa: E402


def main() -> int:
    scenario = load_scenario("watermark")
    apply_scenario_environment(scenario)
    creds = CredentialManager.from_scenario_resources(
        scenario.resources,
        ttl_seconds=3600,
    )
    defaults = retrieval_defaults_from_scenario(
        scenario,
        credential_manager=creds,
        user_id="alice",
    )
    merged = merge_retrieval_scopes(
        credential_manager=creds,
        scenario=scenario,
        user_id="alice",
    )

    security_chunk = DocChunk(
        source="SECURITY-BASELINE.md",
        start_line=1,
        text="HTTPS whitelist cookie baseline policy",
        tenant_id="default",
        classification="confidential",
        acl=["group:ops", "group:security"],
        authority=85,
    )
    store = RagStore([security_chunk])
    request = RetrievalRequest(
        tenant_id=str(defaults["tenant_id"]),
        user_id="alice",
        query="HTTPS cookie baseline",
        max_classification="confidential",
        allowed_scopes=list(defaults["allowed_scopes"]),
        purpose="verify_scopes",
    )
    detailed, policy = store.policy_aware_search(request, top_k=4)

    checks = {
        "http_scopes_present": "http:read" in merged and "http:write" in merged,
        "rag_group_scopes_present": "group:ops" in merged and "group:security" in merged,
        "user_scope_present": "user:alice" in merged,
        "security_baseline_allowed": any(chunk.source == "SECURITY-BASELINE.md" for chunk in detailed.chunks),
        "embedding_model_from_scenario": os.environ.get("RAG_EMBEDDING_MODEL") == "BAAI/bge-small-zh-v1.5",
    }
    passed = all(checks.values())
    print(f"checks={json.dumps(checks, ensure_ascii=False, sort_keys=True)}")
    print(f"allowed_scopes={','.join(request.allowed_scopes)}")
    print(f"rag_retrieval_scopes={'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
