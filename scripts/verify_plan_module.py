#!/usr/bin/env python
"""Verify route-first planner contract, plan_updated, and troubleshooting replan."""

from __future__ import annotations

import json
import sys
from uuid import uuid4
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402

from copilot_agent.agent.nodes import AgentNodes  # noqa: E402
from copilot_agent.agent.plan_builder import (  # noqa: E402
    build_plan_from_route,
    maybe_replan_troubleshooting,
    update_plan_outcomes,
)
from copilot_agent.agent.llm_planner import LlmPlannerError, LlmPlannerUnavailable, plan_with_llm  # noqa: E402
from copilot_agent.context.manager import ContextManager  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402
from copilot_agent.memory import MemoryManager  # noqa: E402
from copilot_agent.rag.retriever import RagStore  # noqa: E402
from copilot_agent.runtime.event_store import EventStore  # noqa: E402
from copilot_agent.scenario import load_scenario  # noqa: E402
from copilot_agent.scenario.router import route_tools  # noqa: E402
from copilot_agent.tools.registry import ToolRegistry  # noqa: E402


class _FakeLlm:
    def __init__(self, content: str) -> None:
        self._content = content

    async def ainvoke(self, _messages):
        return AIMessage(content=self._content)


class _FakeProvider:
    def __init__(self, content: str) -> None:
        self._content = content

    def get_chat_model(self) -> _FakeLlm:
        return _FakeLlm(self._content)


class _FakePolicy:
    pass


async def _verify_llm_planner(route, plan, registry: ToolRegistry) -> dict[str, bool]:
    old_enabled = settings.agent_llm_planner_enabled
    old_key = settings.openai_api_key
    try:
        settings.agent_llm_planner_enabled = True
        settings.openai_api_key = "test-key"
        ok_payload = json.dumps(
            {
                "tool_route": {
                    "kind": "troubleshooting",
                    "recommended_tools": ["search_docs", "http_get"],
                    "forbidden_tools": ["http_post"],
                    "suggested_paths": ["/api/v1/jobs/demo"],
                    "rationale": "Need docs and live status.",
                },
                "plan": {
                    "goal": "troubleshoot demo",
                    "route_kind": "troubleshooting",
                    "steps": [
                        {"id": "step-docs", "goal": "Read runbook", "tool_hint": "search_docs"},
                        {"id": "step-status", "goal": "Read job status", "tool_hint": "http_get"},
                    ],
                },
            }
        )
        result = await plan_with_llm(
            goal="troubleshoot demo",
            baseline_route=route,
            baseline_plan=plan,
            tool_registry=registry,
            llm_provider=_FakeProvider(ok_payload),  # type: ignore[arg-type]
        )
        illegal_tool_failed = False
        bad_payload = '{"tool_route":{"kind":"knowledge","recommended_tools":["delete_file"]},"plan":{"steps":[{"goal":"x"}]}}'
        try:
            await plan_with_llm(
                goal="bad",
                baseline_route=route,
                baseline_plan=plan,
                tool_registry=registry,
                llm_provider=_FakeProvider(bad_payload),  # type: ignore[arg-type]
            )
        except LlmPlannerError:
            illegal_tool_failed = True
        illegal_json_failed = False
        try:
            await plan_with_llm(
                goal="bad json",
                baseline_route=route,
                baseline_plan=plan,
                tool_registry=registry,
                llm_provider=_FakeProvider("not-json"),  # type: ignore[arg-type]
            )
        except LlmPlannerError:
            illegal_json_failed = True
        illegal_route_failed = False
        bad_route_payload = '{"tool_route":{"kind":"unsupported","recommended_tools":["search_docs"]},"plan":{"steps":[{"goal":"x"}]}}'
        try:
            await plan_with_llm(
                goal="bad route",
                baseline_route=route,
                baseline_plan=plan,
                tool_registry=registry,
                llm_provider=_FakeProvider(bad_route_payload),  # type: ignore[arg-type]
            )
        except LlmPlannerError:
            illegal_route_failed = True
        settings.openai_api_key = ""
        missing_key_failed = False
        try:
            await plan_with_llm(
                goal="no key",
                baseline_route=route,
                baseline_plan=plan,
                tool_registry=registry,
                llm_provider=_FakeProvider(ok_payload),  # type: ignore[arg-type]
            )
        except LlmPlannerUnavailable as exc:
            missing_key_failed = "openai_api_key_missing" in str(exc)
        return {
            "llm_planner_accepts_valid_route": result.route.kind == "troubleshooting"
            and result.plan.steps[0].tool_hint == "search_docs",
            "llm_planner_rejects_invalid_json": illegal_json_failed,
            "llm_planner_rejects_illegal_tool": illegal_tool_failed,
            "llm_planner_rejects_illegal_route_kind": illegal_route_failed,
            "llm_planner_missing_key_falls_back": missing_key_failed,
        }
    finally:
        settings.agent_llm_planner_enabled = old_enabled
        settings.openai_api_key = old_key


async def _verify_planner_node(
    *,
    scenario,
    registry: ToolRegistry,
    event_store: EventStore,
    goal: str,
    ok_payload: str,
    bad_payload: str,
) -> dict[str, bool]:
    old_enabled = settings.agent_llm_planner_enabled
    old_key = settings.openai_api_key
    try:
        settings.agent_llm_planner_enabled = True
        settings.openai_api_key = "test-key"
        memory = MemoryManager(
            rag_store=RagStore([]),
            event_store=event_store,
            checkpoint_path=":memory:",
        )
        context = ContextManager(scenario=scenario, memory=memory, tool_registry=registry)

        async def _run_planner(run_id: str, provider: _FakeProvider):
            thread_id = f"{run_id}-thread"
            event_store.create_run(thread_id, run_id=run_id)
            nodes = AgentNodes(
                memory=memory,
                llm_provider=provider,  # type: ignore[arg-type]
                policy=_FakePolicy(),  # type: ignore[arg-type]
                tool_registry=registry,
                tools=[],
                context_manager=context,
            )
            return await nodes.planner(
                {},
                {
                    "configurable": {
                        "conversation_id": thread_id,
                        "run_id": run_id,
                        "input_messages": [{"role": "user", "content": goal}],
                    }
                },
            )

        suffix = uuid4().hex
        llm_run_id = f"verify-plan-node-llm-{suffix}"
        fallback_run_id = f"verify-plan-node-fallback-{suffix}"
        llm_result = await _run_planner(llm_run_id, _FakeProvider(ok_payload))
        llm_event = event_store.list_run_events(llm_run_id)[-1]
        llm_payload = llm_event.get("payload") if isinstance(llm_event.get("payload"), dict) else {}

        fallback_result = await _run_planner(fallback_run_id, _FakeProvider(bad_payload))
        fallback_event = event_store.list_run_events(fallback_run_id)[-1]
        fallback_payload = fallback_event.get("payload") if isinstance(fallback_event.get("payload"), dict) else {}

        settings.openai_api_key = ""
        rules_run_id = f"verify-plan-node-no-key-{suffix}"
        rules_result = await _run_planner(rules_run_id, _FakeProvider(ok_payload))
        rules_event = event_store.list_run_events(rules_run_id)[-1]
        rules_payload = rules_event.get("payload") if isinstance(rules_event.get("payload"), dict) else {}

        return {
            "planner_node_llm_mode_event": llm_payload.get("planner_mode") == "llm"
            and (llm_payload.get("plan") or {}).get("route_kind") == "troubleshooting",
            "planner_node_llm_state": (llm_result.get("tool_route") or {}).get("kind") == "troubleshooting"
            and (llm_result.get("plan") or {}).get("steps", [{}])[0].get("tool_hint") == "search_docs",
            "planner_node_fallback_event": fallback_payload.get("planner_mode") == "fallback"
            and "planner_fallback_reason" in fallback_payload,
            "planner_node_fallback_state": (fallback_result.get("tool_route") or {}).get("kind")
            == "troubleshooting",
            "planner_node_no_key_uses_rules": rules_payload.get("planner_mode") == "rules"
            and "planner_fallback_reason" not in rules_payload
            and (rules_result.get("tool_route") or {}).get("kind") == "troubleshooting",
        }
    finally:
        settings.agent_llm_planner_enabled = old_enabled
        settings.openai_api_key = old_key


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

    registry = ToolRegistry()
    from pydantic import BaseModel  # noqa: WPS433

    class _EmptyArgs(BaseModel):
        pass

    async def _noop(**_kwargs):
        return {}

    for name in ("search_docs", "http_get", "http_post"):
        registry.register_async(
            name=name,
            description=name,
            coroutine=_noop,
            args_schema=_EmptyArgs,
            category="test",
            risk_level="low" if name == "search_docs" else "medium",
        )

    event_store = EventStore(str(ROOT / "storage/verify-plan-module-events.sqlite"))
    context = ContextManager(
        scenario=scenario,
        memory=MemoryManager(
            rag_store=RagStore([]),
            event_store=event_store,
            checkpoint_path=":memory:",
        ),
        tool_registry=registry,
    )
    plan_created = context.plan_created_payload(goal=goal, route=route)
    plan_created["plan"] = plan.as_dict()
    plan_updated = {
        "update_reason": "replan",
        "route_kind": route.kind,
        "plan": replanned.as_dict() if replanned is not None else updated.as_dict(),
    }

    import asyncio

    llm_checks = asyncio.run(_verify_llm_planner(route, plan, registry))
    ok_payload = json.dumps(
        {
            "tool_route": {
                "kind": "troubleshooting",
                "recommended_tools": ["search_docs", "http_get"],
                "forbidden_tools": ["http_post"],
                "suggested_paths": ["/api/v1/jobs/demo"],
                "rationale": "Need docs and live status.",
            },
            "plan": {
                "goal": goal,
                "route_kind": "troubleshooting",
                "steps": [
                    {"id": "step-docs", "goal": "Read runbook", "tool_hint": "search_docs"},
                    {"id": "step-status", "goal": "Read job status", "tool_hint": "http_get"},
                ],
            },
        }
    )
    bad_payload = '{"tool_route":{"kind":"knowledge","recommended_tools":["delete_file"]},"plan":{"steps":[{"goal":"x"}]}}'
    planner_node_checks = asyncio.run(
        _verify_planner_node(
            scenario=scenario,
            registry=registry,
            event_store=event_store,
            goal=goal,
            ok_payload=ok_payload,
            bad_payload=bad_payload,
        )
    )
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
        **llm_checks,
        **planner_node_checks,
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
