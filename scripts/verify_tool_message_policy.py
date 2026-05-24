#!/usr/bin/env python
"""Verify LLM-facing ToolMessage summary policy."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.agent.tool_message_policy import summarize_tool_llm_payload  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402


def main() -> int:
    long_excerpt = "x" * 5000
    full_payload = {
        "success": True,
        "data": {
            "excerpts_markdown": long_excerpt,
            "sources": ["DEPLOY-SERVER.md"],
            "citations": [{"source_file": "DEPLOY-SERVER.md", "chunk_id": "DEPLOY-SERVER.md:1:a"}],
            "suggested_api_paths": ["/actuator/health"],
        },
    }

    original_mode = settings.agent_tool_message_mode
    original_max = settings.agent_tool_message_max_chars
    try:
        settings.agent_tool_message_mode = "full"
        unchanged = summarize_tool_llm_payload("search_docs", full_payload)
        settings.agent_tool_message_mode = "summary"
        settings.agent_tool_message_max_chars = 800
        summarized = summarize_tool_llm_payload("search_docs", full_payload)
        http_full = {
            "success": True,
            "data": {"path": "/actuator/health", "status_code": 200, "body": {"status": "UP", "details": "z" * 3000}},
        }
        http_summary = summarize_tool_llm_payload("http_get", http_full)
    finally:
        settings.agent_tool_message_mode = original_mode
        settings.agent_tool_message_max_chars = original_max

    summary_data = summarized.get("data") if isinstance(summarized, dict) else {}
    excerpt = str(summary_data.get("excerpts_markdown") or "")
    meta = summarized.get("metadata") if isinstance(summarized, dict) else {}
    http_data = http_summary.get("data") if isinstance(http_summary, dict) else {}

    checks = {
        "full_mode_passthrough": unchanged == full_payload,
        "summary_truncates_excerpt": len(excerpt) < len(long_excerpt),
        "summary_keeps_citations": bool(summary_data.get("citations")),
        "summary_flag_set": bool(meta.get("summary_mode")),
        "http_body_preview": "body_preview" in http_data,
    }
    passed = all(checks.values())
    summary = {
        "suite_name": "tool_message_policy",
        "status": "PASS" if passed else "FAIL",
        "checks": checks,
    }
    summary_path = ROOT / "artifacts" / "phase4" / "tool-message-policy-summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"tool_message_policy={'PASS' if passed else 'FAIL'}")
    print(f"summary_json={summary_path}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
