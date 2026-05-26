#!/usr/bin/env python
"""Verify hardened tool side-effect governance semantics."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.agent.nodes import AgentNodes  # noqa: E402
from copilot_agent.agent.state import AgentState  # noqa: E402
from copilot_agent.contracts.validate import validate_event_rows  # noqa: E402
from copilot_agent.context import ContextManager  # noqa: E402
from copilot_agent.memory import MemoryManager  # noqa: E402
from copilot_agent.policy import PolicyRegistry  # noqa: E402
from copilot_agent.rag.retriever import RagStore  # noqa: E402
from copilot_agent.runtime.event_schema import EVENT_TOOL_SIDE_EFFECT_RECORDED  # noqa: E402
from copilot_agent.runtime.event_store import EventStore  # noqa: E402
from copilot_agent.runtime.side_effects import build_side_effect_read_model  # noqa: E402
from copilot_agent.scenario import load_scenario  # noqa: E402
from copilot_agent.scenario.schema import ScenarioPolicyConfig  # noqa: E402
from copilot_agent.tools.audit import (  # noqa: E402
    audit_payload_has_secret,
    build_tool_end_payload,
    build_tool_side_effect_payload,
    build_tool_start_payload,
)
from copilot_agent.tools.registry import ToolRegistry  # noqa: E402
from scripts._verify_helpers import build_verify_fixture, close_fixture, collect_runtime_events  # noqa: E402


class HttpPostArgs(BaseModel):
    path: str
    json_body: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None


class FakeLlmProvider:
    def chat_model(self):
        return None


def _register_http_post(*, requires_approval=True) -> ToolRegistry:
    async def http_post(path: str, json_body: dict[str, Any], idempotency_key: str | None = None) -> dict[str, Any]:
        del json_body, idempotency_key
        return {"ok": True, "status_code": 201, "path": path, "method": "POST", "body": {"id": "ok"}}

    registry = ToolRegistry()
    registry.register_async(
        name="http_post",
        description="POST",
        coroutine=http_post,
        args_schema=HttpPostArgs,
        category="http",
        risk_level="high",
        requires_approval=requires_approval,
        idempotency_key_field="idempotency_key",
    )
    return registry


def _make_nodes(store: EventStore, checkpoint_path: Path, *, policy: PolicyRegistry, registry: ToolRegistry) -> AgentNodes:
    scenario = load_scenario("minimal")
    memory = MemoryManager(
        rag_store=RagStore([]),
        event_store=store,
        checkpoint_path=str(checkpoint_path),
    )
    return AgentNodes(
        memory=memory,
        llm_provider=FakeLlmProvider(),  # type: ignore[arg-type]
        policy=policy,
        tool_registry=registry,
        tools=registry.tools(),
        context_manager=ContextManager(scenario=scenario, memory=memory, tool_registry=registry),
    )


async def _verify_graph_mapper_approved(event_store_path: Path, checkpoint_path: Path, thread_id: str) -> dict[str, Any]:
    graph = StateGraph(AgentState)

    async def assistant_node(state: AgentState) -> dict[str, list[AIMessage]]:
        messages = state.get("messages", [])
        if messages and isinstance(messages[-1], ToolMessage):
            return {"messages": [AIMessage(content="done")]}
        return {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call-approved",
                            "name": "http_post",
                            "args": {
                                "path": "/api/jobs?token=secret-token",
                                "json_body": {"x": 1},
                                "idempotency_key": "idem-approved",
                            },
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        }

    registry = _register_http_post()
    graph.add_node("assistant", assistant_node)
    graph.add_node("tools", ToolNode(registry.tools()))
    graph.set_entry_point("assistant")
    graph.add_conditional_edges(
        "assistant",
        lambda state: "tools"
        if isinstance((state.get("messages") or [])[-1], AIMessage)
        and getattr((state.get("messages") or [])[-1], "tool_calls", None)
        else "__end__",
        {"tools": "tools", "__end__": END},
    )
    graph.add_edge("tools", "assistant")
    fixture = build_verify_fixture(
        event_store_path=event_store_path,
        checkpoint_path=checkpoint_path,
        thread_id=thread_id,
        graph=graph,
        tool_registry=registry,
    )
    try:
        fixture.store.append_event(
            thread_id,
            fixture.run_id,
            "approval_required",
            {
                "required": True,
                "reason": "dangerous_tool",
                "tool_calls": [{"id": "call-approved", "name": "http_post"}],
            },
        )
        await collect_runtime_events(
            fixture,
            graph_input={"messages": [HumanMessage(content="approved path")]},
            graph_config={"configurable": {"thread_id": thread_id}},
        )
        events = fixture.store.list_run_events(fixture.run_id)
        side_effect = next(
            (
                event.get("payload") or {}
                for event in events
                if event.get("type") == EVENT_TOOL_SIDE_EFFECT_RECORDED
            ),
            {},
        )
        return {
            "run_id": fixture.run_id,
            "side_effect": side_effect,
            "validation": validate_event_rows(events),
        }
    finally:
        await close_fixture(fixture)


async def _verify_policy_block(event_store_path: Path, checkpoint_path: Path, thread_id: str) -> dict[str, Any]:
    store = EventStore(str(event_store_path))
    run = store.create_run(thread_id)
    run_id = str(run["id"])
    registry = _register_http_post()
    policy = PolicyRegistry(
        registry,
        scenario_policy=ScenarioPolicyConfig(tool_denylist=["http_post"]),
    )
    nodes = _make_nodes(store, checkpoint_path, policy=policy, registry=registry)
    tool_call = {
        "id": "call-policy-block",
        "name": "http_post",
        "args": {"path": "/api/jobs?cookie=secret", "json_body": {"x": 1}},
        "type": "tool_call",
    }
    await nodes.safety_gate(
        {"messages": [AIMessage(content="", tool_calls=[tool_call])]},
        {"configurable": {"thread_id": thread_id, "run_id": run_id}},
    )
    events = store.list_run_events(run_id)
    side_effects = [event for event in events if event.get("type") == EVENT_TOOL_SIDE_EFFECT_RECORDED]
    read_model = build_side_effect_read_model(store.get_run(run_id) or run, events)
    return {
        "run_id": run_id,
        "event_types": [str(event.get("type") or "") for event in events],
        "side_effect": (side_effects[0].get("payload") if side_effects else {}),
        "read_model": read_model,
        "validation": validate_event_rows(events),
    }


async def _verify_route_block(event_store_path: Path, checkpoint_path: Path, thread_id: str) -> dict[str, Any]:
    store = EventStore(str(event_store_path))
    run = store.create_run(thread_id)
    run_id = str(run["id"])
    registry = _register_http_post()
    policy = PolicyRegistry(registry, scenario_policy=ScenarioPolicyConfig(tool_allowlist=["http_post"]))
    nodes = _make_nodes(store, checkpoint_path, policy=policy, registry=registry)
    tool_call = {
        "id": "call-route-block",
        "name": "http_post",
        "args": {"path": "/api/jobs?token=secret", "json_body": {"x": 1}},
        "type": "tool_call",
    }
    await nodes.safety_gate(
        {
            "messages": [AIMessage(content="", tool_calls=[tool_call])],
            "tool_route": {"kind": "knowledge", "recommended_tools": ["search_docs"], "forbidden_tools": ["http_post"]},
        },
        {"configurable": {"thread_id": thread_id, "run_id": run_id}},
    )
    events = store.list_run_events(run_id)
    side_effects = [event for event in events if event.get("type") == EVENT_TOOL_SIDE_EFFECT_RECORDED]
    return {
        "run_id": run_id,
        "event_types": [str(event.get("type") or "") for event in events],
        "side_effect": (side_effects[0].get("payload") if side_effects else {}),
        "validation": validate_event_rows(events),
    }


def _verify_unknown_status_warning() -> dict[str, Any]:
    run = {"id": "r", "thread_id": "t", "status": "completed"}
    read_model = build_side_effect_read_model(
        run,
        [
            {
                "id": 1,
                "sequence": 1,
                "type": EVENT_TOOL_SIDE_EFFECT_RECORDED,
                "payload": {
                    "tool_name": "http_post",
                    "call_id": "bad-status",
                    "side_effect_status": "mystery",
                },
            }
        ],
    )
    return {
        "warnings": [warning.get("code") for warning in read_model.get("warnings", [])],
        "summary": read_model.get("summary"),
    }


def _verify_direct_payload_contract() -> dict[str, Any]:
    start = build_tool_start_payload(
        name="http_post",
        call_id="direct",
        category="http",
        risk_level="high",
        requires_approval=True,
        arguments={"path": "/api/jobs?token=secret", "json_body": {"x": 1}},
        idempotency_key="idem-direct",
    )
    end = build_tool_end_payload(
        name="http_post",
        call_id="direct",
        result={"ok": True, "status_code": 200, "path": "/api/jobs?token=secret"},
        duration_ms=1,
        idempotency_key="idem-direct",
    )
    payload = build_tool_side_effect_payload(
        tool_start_payload=start,
        tool_end_payload=end,
        approval_status="approved",
    )
    return {"payload": payload}


async def verify(event_store_path: Path, checkpoint_path: Path, thread_prefix: str) -> dict[str, Any]:
    return {
        "approved": await _verify_graph_mapper_approved(
            event_store_path.with_name(f"{event_store_path.stem}-approved.sqlite"),
            checkpoint_path.with_name(f"{checkpoint_path.stem}-approved.sqlite"),
            f"{thread_prefix}-approved",
        ),
        "policy_block": await _verify_policy_block(
            event_store_path.with_name(f"{event_store_path.stem}-policy.sqlite"),
            checkpoint_path,
            f"{thread_prefix}-policy",
        ),
        "route_block": await _verify_route_block(
            event_store_path.with_name(f"{event_store_path.stem}-route.sqlite"),
            checkpoint_path,
            f"{thread_prefix}-route",
        ),
        "unknown_status": _verify_unknown_status_warning(),
        "direct_contract": _verify_direct_payload_contract(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify hardened side-effect governance v1.")
    parser.add_argument(
        "--event-store-path",
        default=str(ROOT / "storage/verify-tool-side-effect-governance-events.sqlite"),
    )
    parser.add_argument(
        "--checkpoint-path",
        default=str(ROOT / "storage/verify-tool-side-effect-governance-checkpoints.sqlite"),
    )
    parser.add_argument("--thread-prefix", default=f"side-effect-gov-{uuid.uuid4().hex[:8]}")
    parser.add_argument(
        "--summary-json",
        default=str(ROOT / "artifacts/runtime/tool-side-effect-governance-v1-summary.json"),
    )
    args = parser.parse_args(argv)
    if argv is None:
        print(
            "deprecated_wrapper=verify_tool_side_effect_governance_v1.py; "
            "use=scripts/verify_tool_governance_domain.py --case governance"
        )

    event_store_path = Path(args.event_store_path).resolve()
    checkpoint_path = Path(args.checkpoint_path).resolve()
    event_store_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    summary = asyncio.run(verify(event_store_path, checkpoint_path, args.thread_prefix))
    approved = summary["approved"]["side_effect"]
    policy = summary["policy_block"]["side_effect"]
    route = summary["route_block"]["side_effect"]
    direct = summary["direct_contract"]["payload"] or {}
    checks = {
        "approved_status": approved.get("approval_status") == "approved"
        and approved.get("side_effect_status") == "confirmed",
        "approved_path_canonicalized": approved.get("path") == "/api/jobs",
        "policy_block_recorded": policy.get("side_effect_status") == "blocked"
        and policy.get("approval_status") == "policy_blocked"
        and policy.get("policy_source") == "scenario_tool_policy",
        "policy_block_no_tool_execution": "tool_start" not in summary["policy_block"]["event_types"]
        and "tool_end" not in summary["policy_block"]["event_types"],
        "route_block_recorded": route.get("side_effect_status") == "blocked"
        and route.get("approval_status") == "policy_blocked"
        and route.get("policy_source") == "tool_route_policy",
        "route_block_no_tool_execution": "tool_start" not in summary["route_block"]["event_types"]
        and "tool_end" not in summary["route_block"]["event_types"],
        "strict_contract_validates": summary["approved"]["validation"].get("model_validate_ok") is True
        and summary["policy_block"]["validation"].get("model_validate_ok") is True
        and summary["route_block"]["validation"].get("model_validate_ok") is True,
        "unknown_status_warned": "side_effect_unknown_status" in summary["unknown_status"]["warnings"],
        "direct_payload_sanitized": direct.get("path") == "/api/jobs"
        and direct.get("approval_status") == "approved"
        and not audit_payload_has_secret(direct)
        and "?" not in str(direct.get("path") or ""),
    }
    passed = all(checks.values())
    summary["checks"] = checks
    summary["tool_side_effect_governance_v1"] = "PASS" if passed else "FAIL"
    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    for key, value in checks.items():
        print(f"{key}={value}")
    print(f"summary_json={summary_path}")
    print(f"tool_side_effect_governance_v1={summary['tool_side_effect_governance_v1']}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
