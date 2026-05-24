#!/usr/bin/env python
"""Verify L7 FinalAnswerModel assembly from checkpoint ToolMessages."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402

from copilot_agent.agent.final_answer import build_final_answer  # noqa: E402
from copilot_agent.contracts.final_answer import FinalAnswerModel  # noqa: E402
from copilot_agent.eval.citation import evaluate_structured_citations  # noqa: E402


def main() -> int:
    search_payload = {
        "success": True,
        "data": {
            "excerpts_markdown": "See DEPLOY-SERVER.md",
            "sources": ["DEPLOY-SERVER.md"],
            "citations": [
                {
                    "source_file": "DEPLOY-SERVER.md",
                    "heading_path": "Deploy",
                    "start_line": 10,
                    "chunk_id": "DEPLOY-SERVER.md:10:abc",
                    "authority": 90,
                }
            ],
        },
    }
    messages = [
        ToolMessage(content=json.dumps(search_payload, ensure_ascii=False), name="search_docs", tool_call_id="c1"),
        ToolMessage(content='{"success": true, "data": {"path": "/actuator/health", "status_code": 200}}', name="http_get", tool_call_id="c2"),
        AIMessage(content="Worker may be down; see DEPLOY-SERVER.md."),
    ]
    model = build_final_answer(
        answer="Worker may be down; see DEPLOY-SERVER.md.",
        messages=messages,
        route_kind="troubleshooting",
        metadata={"safety_status": "safe", "output_guard_action": "allow", "citation_required": True},
    )
    missing_citation = build_final_answer(
        answer="This answer needs evidence but has none.",
        messages=[],
        route_kind="knowledge",
        metadata={"citation_required": True},
    )
    structured = evaluate_structured_citations(
        citations=[item.model_dump() for item in model.citations],
        required_sources=["DEPLOY-SERVER.md"],
    )
    checks = {
        "model_type_ok": isinstance(model, FinalAnswerModel),
        "contract_version_v2": model.contract_version == 2,
        "answer_preserved": model.answer.startswith("Worker may be down"),
        "citation_count": len(model.citations) == 1,
        "tools_used": model.tools_used == ["search_docs", "http_get"],
        "tool_evidence": len(model.tool_evidence) == 2
        and model.tool_evidence[0].get("tool") == "search_docs"
        and model.tool_evidence[0].get("citation_count") == 1,
        "route_kind": model.route_kind == "troubleshooting",
        "evidence_count": model.evidence_count == 1
        and model.source_count == 1
        and model.metadata.get("evidence_count") == 1,
        "citation_status": model.citation_required is True and model.citation_status == "satisfied",
        "missing_citation_warns": missing_citation.citation_status == "missing"
        and "citation_required_but_missing" in missing_citation.contract_warnings,
        "safety_status": model.safety_status == "safe",
        "output_guard_action": model.output_guard_action == "allow",
        "structured_citations_pass": structured.passed,
    }
    passed = all(checks.values())
    summary = {
        "suite_name": "final_answer_l7",
        "status": "PASS" if passed else "FAIL",
        "checks": checks,
        "final_answer": model.model_dump(),
        "missing_citation": missing_citation.model_dump(),
    }
    summary_path = ROOT / "artifacts" / "phase4" / "final-answer-l7-summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"final_answer_l7={'PASS' if passed else 'FAIL'}")
    print(f"summary_json={summary_path}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
