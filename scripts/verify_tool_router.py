#!/usr/bin/env python
"""Verify declarative scenario router rules (watermark router/rules.yaml)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.scenario import load_scenario  # noqa: E402
from copilot_agent.agent.tool_route_merge import merge_api_paths_into_route  # noqa: E402
from copilot_agent.scenario.router import route_tools, tool_allowed  # noqa: E402
from copilot_agent.scenario.router.types import ToolRoute  # noqa: E402


def _assert(name: str, ok: bool) -> None:
    if not ok:
        raise SystemExit(f"FAIL: {name}")
    print(f"PASS: {name}")


def _load_cases(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("cases") or [])


def main() -> int:
    scenario = load_scenario("watermark")
    _assert("watermark_router_loaded", scenario.router_rules is not None)
    _assert("watermark_router_rules", len(scenario.router_rules.rules) >= 6)  # type: ignore[union-attr]

    engine = scenario.router_engine
    cases_path = ROOT / "eval" / "phase4-eval-cases.json"
    cases = _load_cases(cases_path)

    for case in cases:
        case_id = str(case.get("id", ""))
        question = str(case.get("question", ""))
        category = str(case.get("category", ""))
        expected = [str(x) for x in case.get("expected_tools") or []]
        expect_blocked = bool(case.get("expect_blocked", False))
        route = route_tools(question, engine=engine)

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
            route = route_tools(question, engine=engine, confirm_dangerous=True, allow_job_post=True)
            _assert("P4-010 dangerous execute", route.kind == "dangerous_execute")
            _assert("P4-010 allows http_post", tool_allowed(route, "http_post"))
            continue

    deploy = route_tools("生产部署 Java API 的大致步骤是什么？", engine=engine)
    _assert("deploy steps knowledge", deploy.kind == "knowledge")
    _assert("deploy forbids http_get", not tool_allowed(deploy, "http_get"))

    troubleshoot = route_tools("水印任务一直 QUEUED 或 PROCESSING 怎么排查？", engine=engine)
    _assert("queued troubleshooting", troubleshoot.kind == "troubleshooting")
    _assert("troubleshoot search first", troubleshoot.recommended_tools[0] == "search_docs")

    contract = route_tools("POST /api/v1/jobs/watermark 默认 algorithmType 是什么？", engine=engine)
    _assert("contract question knowledge", contract.kind == "knowledge")

    merged, changed = merge_api_paths_into_route(
        ToolRoute(
            kind="troubleshooting",
            recommended_tools=("search_docs", "http_get"),
            forbidden_tools=("http_post",),
            suggested_paths=("/actuator/health",),
            rationale="test",
        ),
        ["/api/v1/jobs/22222222-2222-4222-8222-222222222222"],
    )
    _assert("path merge adds retrieval hint", changed)
    _assert(
        "path merge keeps prior paths",
        "/actuator/health" in merged.suggested_paths
        and "/api/v1/jobs/22222222-2222-4222-8222-222222222222" in merged.suggested_paths,
    )

    defaults_query = "介绍一下水印平台的核心模块架构"
    baseline = engine.route(defaults_query)
    decision = engine.route_detailed(defaults_query)
    _assert("defaults query uses rule fallback", decision.used_defaults)
    _assert("defaults query is knowledge", baseline.kind == "knowledge")

    async def _mock_live_classifier(_query: str, _baseline: ToolRoute) -> dict:
        return {
            "kind": "live_status",
            "recommended_tools": ["http_get"],
            "suggested_paths": ["/actuator/health"],
            "rationale": "Needs live health check",
        }

    import asyncio

    from copilot_agent.scenario.router.llm_fallback import refine_route_with_llm  # noqa: E402
    from copilot_agent.settings import settings  # noqa: E402

    original_fallback = settings.agent_tool_route_llm_fallback
    settings.agent_tool_route_llm_fallback = True
    try:
        refined = asyncio.run(
            refine_route_with_llm(
                defaults_query,
                baseline,
                classifier=_mock_live_classifier,
            )
        )
    finally:
        settings.agent_tool_route_llm_fallback = original_fallback
    _assert("llm fallback upgrades kind", refined.kind == "live_status")
    _assert("llm fallback recommends http_get", "http_get" in refined.recommended_tools)

    print("verify_tool_router=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
