#!/usr/bin/env python
"""Verify deterministic RAG retrieval gate and unchecked retrieval guardrails."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.context.preretrieval_dedupe import build_preretrieval_cache  # noqa: E402
from copilot_agent.context.retrieval_gate import build_policy_context_hash, decide_retrieval  # noqa: E402
from copilot_agent.contracts.retrieval import RetrievalRequest  # noqa: E402
from copilot_agent.rag.schema import DocChunk  # noqa: E402
from copilot_agent.scenario.router.types import ToolRoute  # noqa: E402


def _route(kind: str = "knowledge", tools: tuple[str, ...] = ("search_docs",)) -> ToolRoute:
    return ToolRoute(
        kind=kind,  # type: ignore[arg-type]
        recommended_tools=tools,
        forbidden_tools=(),
        suggested_paths=(),
        rationale="verify",
    )


def _request(**updates: object) -> RetrievalRequest:
    payload = {
        "tenant_id": "tenant-a",
        "user_id": "user-a",
        "query": "水印任务一直 QUEUED 怎么排查？",
        "purpose": "preretrieval_context",
        "max_classification": "internal",
        "allowed_scopes": ["tenant:tenant-a", "user:user-a"],
        "allow_high_pii": False,
    }
    payload.update(updates)
    return RetrievalRequest(**payload)  # type: ignore[arg-type]


def _memory(high_confidence: bool = False) -> dict[str, object]:
    if not high_confidence:
        return {"episodic": {"recalled_long_term": [], "inject_preview": ""}}
    return {
        "episodic": {
            "recalled_long_term": [
                {
                    "memory_type": "fact",
                    "confidence": 0.94,
                    "score": 0.72,
                    "content": "用户的 Agent UI 部署在 /agent/ui/。",
                }
            ],
            "inject_preview": "Long-term answer seed: 用户的 Agent UI 部署在 /agent/ui/。",
        }
    }


def _cache(request: RetrievalRequest) -> dict[str, object]:
    hits = [
        DocChunk(
            source="RUNBOOK.md",
            start_line=1,
            text="QUEUED tasks may indicate worker or Redis Stream issues.",
            doc_type="runbook",
            tenant_id=request.tenant_id,
            acl=list(request.allowed_scopes),
        )
    ]
    return build_preretrieval_cache(
        query="水印任务一直 QUEUED 怎么排查？",
        hits=hits,
        request=request,
        policy_context_hash=build_policy_context_hash(request),
        policy_trace_id="trace-a",
        retrieval_mode="preretrieval",
    )


def _production_files_without_unchecked_rag() -> bool:
    targets = [
        ROOT / "copilot_agent/context",
        ROOT / "copilot_agent/agent",
        ROOT / "copilot_agent/runtime",
    ]
    needles = ("search_docs_unchecked(", "search_docs_detailed_unchecked(", "._rag.search(")
    for base in targets:
        for path in base.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if any(needle in text for needle in needles):
                return False
    return True


def main() -> int:
    request = _request()
    checks: dict[str, bool] = {}

    checks["route_search_docs_retrieves"] = (
        decide_retrieval(
            query="普通知识问题",
            route=_route(),
            memory_dict=_memory(),
            request=request,
        ).action
        == "retrieve"
    )
    checks["doc_api_deploy_error_intent_retrieves"] = (
        decide_retrieval(
            query="部署接口返回错误码 E401 应该看哪个文档？",
            route=_route(tools=()),
            memory_dict=_memory(),
            request=request,
        ).action
        == "retrieve"
    )
    checks["similar_query_same_policy_reuses_cache"] = (
        decide_retrieval(
            query="水印任务一直 QUEUED 怎么排查",
            route=_route(),
            memory_dict=_memory(),
            request=request,
            previous_cache=_cache(request),
        ).action
        == "reuse_cache"
    )
    checks["similar_query_policy_mismatch_no_cache_reuse"] = (
        decide_retrieval(
            query="水印任务一直 QUEUED 怎么排查",
            route=_route(),
            memory_dict=_memory(),
            request=_request(user_id="user-b", allowed_scopes=["tenant:tenant-a", "user:user-b"]),
            previous_cache=_cache(request),
        ).action
        != "reuse_cache"
    )
    checks["memory_high_confidence_skips_rag"] = (
        decide_retrieval(
            query="我的 Agent UI 在哪里？",
            route=_route(),
            memory_dict=_memory(high_confidence=True),
            request=request,
        ).action
        == "skip_rag"
    )
    checks["chitchat_formatting_skips_rag"] = (
        decide_retrieval(
            query="好的",
            route=_route(),
            memory_dict=_memory(),
            request=request,
        ).action
        == "skip_rag"
        and decide_retrieval(
            query="帮我把这段话润色一下",
            route=_route(),
            memory_dict=_memory(),
            request=request,
        ).action
        == "skip_rag"
    )
    live_decision = decide_retrieval(
        query="现在公网 DNS 生效了吗？",
        route=_route(kind="live_status", tools=("http_get",)),
        memory_dict=_memory(),
        request=request,
    )
    checks["live_current_routes_to_tool_api"] = (
        live_decision.action == "route_to_tool_api" and live_decision.recommended_next == "prefer_live_tool_or_api"
    )
    checks["decision_payload_sanitized"] = "query" not in live_decision.as_dict()
    checks["production_no_unchecked_rag_calls"] = _production_files_without_unchecked_rag()

    passed = all(checks.values())
    summary = {
        "suite_name": "retrieval_gate_v1",
        "status": "PASS" if passed else "FAIL",
        "checks": checks,
    }
    summary_path = ROOT / "artifacts/runtime/retrieval-gate-v1-summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"checks={json.dumps(checks, ensure_ascii=False)}")
    print(f"summary_json={summary_path}")
    print(f"verify_retrieval_gate_v1={'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
