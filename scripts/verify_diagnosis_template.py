#!/usr/bin/env python
"""Verify structured troubleshooting diagnosis outline generation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from langchain_core.messages import ToolMessage  # noqa: E402

from copilot_agent.agent.diagnosis import build_diagnosis_outline, should_inject_diagnosis  # noqa: E402


def main() -> int:
    messages = [
        ToolMessage(
            content='{"success": true, "data": {"sources": ["RUNBOOK.md"], "suggested_api_paths": []}}',
            name="search_docs",
            tool_call_id="c1",
        ),
        ToolMessage(
            content='{"success": true, "data": {"body": {"status": "QUEUED", "id": "job-1"}}}',
            name="http_get",
            tool_call_id="c2",
        ),
    ]
    should = should_inject_diagnosis(route_kind="troubleshooting", messages=messages)
    outline = build_diagnosis_outline(
        route_kind="troubleshooting",
        messages=messages,
        question="为什么任务一直 QUEUED？",
    )
    text = outline.to_system_message() if outline else ""

    checks = {
        "should_inject": should,
        "outline_created": outline is not None,
        "status_queued": outline is not None and outline.status == "QUEUED",
        "has_sections": all(section in text for section in ["## 文档依据", "## 建议排查步骤"]),
        "mentions_runbook": "RUNBOOK.md" in text,
    }
    passed = all(checks.values())
    summary = {"suite_name": "diagnosis_template", "status": "PASS" if passed else "FAIL", "checks": checks}
    summary_path = ROOT / "artifacts/phase4/diagnosis-template-summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"diagnosis_template={'PASS' if passed else 'FAIL'}")
    print(f"summary_json={summary_path}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
