#!/usr/bin/env python
"""Verify tool routing aligns with phase4 eval expected_tools."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.agent.tool_router import route_tools, tool_allowed  # noqa: E402


def _assert(name: str, ok: bool) -> None:
    if not ok:
        raise SystemExit(f"FAIL: {name}")
    print(f"PASS: {name}")


def _load_cases(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("cases") or [])


def main() -> int:
    cases_path = ROOT / "eval" / "phase4-eval-cases.json"
    cases = _load_cases(cases_path)

    for case in cases:
        case_id = str(case.get("id", ""))
        question = str(case.get("question", ""))
        category = str(case.get("category", ""))
        expected = [str(x) for x in case.get("expected_tools") or []]
        expect_blocked = bool(case.get("expect_blocked", False))
        route = route_tools(question)

        if expect_blocked:
            _assert(f"{case_id} safety reject", route.kind == "safety_reject")
            continue

        if category == "docs":
            _assert(f"{case_id} docs route", route.kind in {"knowledge", "troubleshooting"})
            _assert(f"{case_id} docs recommends search", "search_docs" in route.recommended_tools)

        if category == "api":
            _assert(f"{case_id} api live or login", route.kind == "live_status")
            for tool in expected:
                base = tool.split(":", 1)[0]
                _assert(f"{case_id} recommends {base}", base in route.recommended_tools)

        if case_id == "P4-010":
            route = route_tools(question, confirm_dangerous=True, allow_job_post=True)
            _assert("P4-010 dangerous execute", route.kind == "dangerous_execute")
            _assert("P4-010 allows http_post", tool_allowed(route, "http_post"))
            continue

    deploy = route_tools("生产部署 Java API 的大致步骤是什么？")
    _assert("deploy steps knowledge", deploy.kind == "knowledge")
    _assert("deploy forbids http_get", not tool_allowed(deploy, "http_get"))

    troubleshoot = route_tools("水印任务一直 QUEUED 或 PROCESSING 怎么排查？")
    _assert("queued troubleshooting", troubleshoot.kind == "troubleshooting")
    _assert("troubleshoot search first", troubleshoot.recommended_tools[0] == "search_docs")

    contract = route_tools("POST /api/v1/jobs/watermark 默认 algorithmType 是什么？")
    _assert("contract question knowledge", contract.kind == "knowledge")

    print("verify_tool_router=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
