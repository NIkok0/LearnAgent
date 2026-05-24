#!/usr/bin/env python
"""Verify route-first planner contract, plan_updated, and troubleshooting replan."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402

from copilot_agent.agent.plan_builder import (  # noqa: E402
    build_plan_from_route,
    maybe_replan_troubleshooting,
    update_plan_outcomes,
)
from copilot_agent.context.manager import ContextManager  # noqa: E402
from copilot_agent.memory import MemoryManager  # noqa: E402
from copilot_agent.rag.retriever import RagStore  # noqa: E402
from copilot_agent.runtime.event_store import EventStore  # noqa: E402
from copilot_agent.scenario import load_scenario  # noqa: E402
from copilot_agent.scenario.router import route_tools  # noqa: E402
from copilot_agent.tools.registry import ToolRegistry  # noqa: E402


def main() -> int:
    scenario = load_scenario("watermark")
    goal = "watermark task stays QUEUED, how should I troubleshoot it?"
    route = route_tools(goal, engine=scenario.router_engine)
    plan = build_plan_from_route(route, goal=goal)
    messages = [
        ToolMessage(content='{"success": true, "data": {"citations": []}}', name="search_docs", tool_call_id="c1"),
        ToolMessage(content='{"success": true, "data": {"status_code": 200}}', name="http_get", tool_call_id="c2"),
        AIMessage(content="", tool_calls=[]),
    ]
    updated = update_plan_outcomes(plan, messages)
    replanned = maybe_replan_troubleshooting(updated, route.kind, messages)

    context = ContextManager(
        scenario=scenario,
        memory=MemoryManager(
            rag_store=RagStore([]),
            event_store=EventStore(str(ROOT / "storage/verify-plan-module-events.sqlite")),
            checkpoint_path=":memory:",
        ),
        tool_registry=ToolRegistry(),
    )
    plan_created = context.plan_created_payload(goal=goal, route=route)
    plan_created["plan"] = plan.as_dict()
    plan_updated = {
        "update_reason": "replan",
        "route_kind": route.kind,
        "plan": replanned.as_dict() if replanned is not None else updated.as_dict(),
    }

    checks = {
        "plan_has_steps": len(plan.steps) >= 2,
        "route_kind_troubleshooting": route.kind == "troubleshooting",
        "outcomes_mark_completed": any(step.status == "completed" for step in updated.steps),
        "replan_adds_summary_step": replanned is not None
        and any(step.id == "step-replan-summary" for step in replanned.steps),
        "plan_created_contract": bool(
            plan_created.get("goal")
            and plan_created.get("strategy") == "route_first_react"
            and isinstance(plan_created.get("tool_route"), dict)
            and isinstance((plan_created.get("plan") or {}).get("steps"), list)
        ),
        "plan_updated_contract": bool(
            plan_updated.get("update_reason")
            and isinstance((plan_updated.get("plan") or {}).get("steps"), list)
        ),
    }
    passed = all(checks.values())
    summary = {
        "suite_name": "plan_module",
        "status": "PASS" if passed else "FAIL",
        "checks": checks,
        "plan": plan.as_dict(),
        "plan_created": plan_created,
        "plan_updated": plan_updated,
        "replanned": replanned.as_dict() if replanned is not None else None,
    }
    summary_path = ROOT / "artifacts" / "phase4" / "plan-module-summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"plan_module={'PASS' if passed else 'FAIL'}")
    print(f"summary_json={summary_path}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
