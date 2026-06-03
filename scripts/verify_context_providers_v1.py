#!/usr/bin/env python
"""Verify v2 ContextBlock provider trace construction."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.context.providers import build_context_blocks, estimate_tokens  # noqa: E402
from copilot_agent.scenario.router.types import ToolRoute  # noqa: E402


def verify() -> dict[str, bool | int | list[str]]:
    route = ToolRoute(
        kind="troubleshooting",
        recommended_tools=("search_docs", "http_get"),
        forbidden_tools=("http_post",),
        suggested_paths=("/api/jobs/{id}",),
        rationale="diagnose job status",
    )
    blocks = build_context_blocks(
        scenario_name="watermark",
        system_prompt="You are LearnAgent.",
        route=route,
        memory_injections=[
            {"kind": "episodic", "preview_chars": 80, "recalled_runs": 1},
            {"kind": "long_term", "items": 2, "sources": [{"id": "m1"}]},
        ],
        skill_injections=[
            {
                "name": "watermark_diagnosis",
                "description": "Diagnose watermark task failures.",
                "injected": True,
            },
            {
                "name": "missing_capability_skill",
                "description": "Should be visible but not injected.",
                "injected": False,
            },
        ],
        retrieved_context=[
            {"source": "RUNBOOK.md", "excerpt": "queued task troubleshooting"},
            {"source": "API.md", "text": "GET /api/jobs/{id}"},
        ],
        policy_hints=[{"tool_allowlist": ["search_docs", "http_get"]}],
        tool_schema_count=3,
        budget={"max_context_chars": 4096},
        truncation_report={
            "retrieval_decision": {"action": "retrieve", "reason": "troubleshooting_intent"},
            "truncated": False,
        },
    )
    sources = [block.source for block in blocks]
    priorities = [block.priority for block in blocks]
    skill_tags = {
        block.trace_id: list(block.policy_tags)
        for block in blocks
        if block.source == "skill"
    }
    retrieval = next(block for block in blocks if block.source == "retrieval")
    return {
        "provider_block_count": len(blocks),
        "provider_sources": sources,
        "provider_sources_cover_v2_core": {
            "system_prompt",
            "route",
            "memory:episodic",
            "memory:long_term",
            "skill",
            "retrieval",
            "tool_schemas",
            "policy_hints",
            "budget_packer",
        }.issubset(set(sources)),
        "provider_priorities_sorted": priorities == sorted(priorities),
        "missing_capability_skill_marked_not_injected": "not_injected"
        in skill_tags.get("skill:missing_capability_skill", []),
        "retrieval_block_has_decision_metadata": retrieval.metadata.get("decision", {}).get("action") == "retrieve",
        "retrieval_block_content_not_raw_text": not retrieval.content,
        "token_estimate_zero_for_empty": estimate_tokens("") == 0,
        "token_estimate_positive_for_text": estimate_tokens("abcd") == 1,
    }


def main() -> int:
    checks = verify()
    passed = all(value is True for value in checks.values() if isinstance(value, bool))
    summary = {
        "suite_name": "context_providers_v1",
        "status": "PASS" if passed else "FAIL",
        "checks": checks,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"verify_context_providers_v1={'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
