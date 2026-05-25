#!/usr/bin/env python
"""Verify Policy Decision Audit v1 across tool, RAG, output guard, and timeline."""

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
from copilot_agent.context import ContextManager  # noqa: E402
from copilot_agent.contracts.adapters.tool_rag import RagSearchAdapter  # noqa: E402
from copilot_agent.contracts.events.registry import PayloadValidationError, validate_payload_for_kind  # noqa: E402
from copilot_agent.contracts.retrieval import RetrievalRequest  # noqa: E402
from copilot_agent.contracts.validate import validate_event_rows  # noqa: E402
from copilot_agent.memory import MemoryManager  # noqa: E402
from copilot_agent.policy import PolicyRegistry  # noqa: E402
from copilot_agent.rag.context_guard import detect_sensitive_output  # noqa: E402
from copilot_agent.rag.retriever import RagStore  # noqa: E402
from copilot_agent.rag.schema import DocChunk, format_chunks_for_prompt  # noqa: E402
from copilot_agent.runtime.event_schema import (  # noqa: E402
    EVENT_POLICY_DECISION_RECORDED,
    EVENT_RETRIEVAL_COMPLETED,
    EVENT_TOOL_SIDE_EFFECT_RECORDED,
)
from copilot_agent.runtime.event_store import EventStore  # noqa: E402
from copilot_agent.runtime.policy_audit import (  # noqa: E402
    build_output_guard_policy_decision_payload,
    build_policy_decision_payload,
    build_policy_read_model,
    build_rag_policy_decision_payloads,
)
from copilot_agent.runtime.timeline import TimelineProjector  # noqa: E402
from copilot_agent.scenario import load_scenario  # noqa: E402
from copilot_agent.scenario.schema import ScenarioPolicyConfig  # noqa: E402
from copilot_agent.tools.audit import audit_payload_has_secret  # noqa: E402
from copilot_agent.tools.registry import ToolRegistry  # noqa: E402
from scripts._verify_helpers import build_verify_fixture, close_fixture, collect_runtime_events  # noqa: E402
from scripts.export_run_debug_bundle import build_debug_bundle  # noqa: E402


class HttpGetArgs(BaseModel):
    path: str


class HttpPostArgs(BaseModel):
    path: str
    json_body: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None


class FakeLlmProvider:
    def chat_model(self):
        return None


def _chunk(
    source: str,
    text: str,
    *,
    tenant_id: str = "tenant-a",
    acl: list[str] | None = None,
    classification: str = "internal",
    pii_level: str = "none",
    start_line: int = 1,
) -> DocChunk:
    return DocChunk(
        source=source,
        start_line=start_line,
        text=text,
        section_title=source,
        tenant_id=tenant_id,
        doc_id=source,
        acl=acl or [],
        classification=classification,
        pii_level=pii_level,
        source_hash=f"hash-{source}",
        retention_policy="mvp",
    )


def _register_tools() -> ToolRegistry:
    async def http_get(path: str) -> dict[str, Any]:
        return {"ok": True, "status_code": 200, "path": path}

    async def http_post(path: str, json_body: dict[str, Any], idempotency_key: str | None = None) -> dict[str, Any]:
        del json_body, idempotency_key
        return {"ok": True, "status_code": 201, "path": path, "method": "POST"}

    registry = ToolRegistry()
    registry.register_async(
        name="http_get",
        description="GET",
        coroutine=http_get,
        args_schema=HttpGetArgs,
        category="http",
        risk_level="medium",
        requires_approval=False,
    )
    registry.register_async(
        name="http_post",
        description="POST",
        coroutine=http_post,
        args_schema=HttpPostArgs,
        category="http",
        risk_level="high",
        requires_approval=True,
        idempotency_key_field="idempotency_key",
    )
    return registry


async def _verify_tool_allow(event_store_path: Path, checkpoint_path: Path, thread_id: str) -> dict[str, Any]:
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
                            "id": "call-allow",
                            "name": "http_get",
                            "args": {"path": "/actuator/health?token=secret-token"},
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        }

    registry = _register_tools()
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
        await collect_runtime_events(
            fixture,
            graph_input={"messages": [HumanMessage(content="allow path")]},
            graph_config={"configurable": {"thread_id": thread_id}},
        )
        events = fixture.store.list_run_events(fixture.run_id)
        policy = next(
            (
                event.get("payload") or {}
                for event in events
                if event.get("type") == EVENT_POLICY_DECISION_RECORDED
                and (event.get("payload") or {}).get("decision") == "allow"
            ),
            {},
        )
        return {"run_id": fixture.run_id, "policy": policy, "validation": validate_event_rows(events)}
    finally:
        await close_fixture(fixture)


async def _verify_tool_ask_and_approved(event_store_path: Path, checkpoint_path: Path, thread_id: str) -> dict[str, Any]:
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

    registry = _register_tools()
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
        ask = build_policy_decision_payload(
            scope="tool",
            source="scenario_approval_policy",
            subject="http_post",
            action="tool_call",
            resource="/api/jobs?token=secret-token",
            decision="ask",
            reason="dangerous_tool_requires_approval",
            risk_level="high",
            requires_approval=True,
            related_call_id="call-approved",
            metadata={"path": "/api/jobs?token=secret-token"},
        )
        fixture.store.append_event(thread_id, fixture.run_id, EVENT_POLICY_DECISION_RECORDED, ask)
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
            "ask": ask,
            "side_effect": side_effect,
            "validation": validate_event_rows(events),
        }
    finally:
        await close_fixture(fixture)


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


async def _verify_policy_and_route_block(event_store_path: Path, checkpoint_path: Path, thread_id: str) -> dict[str, Any]:
    registry = _register_tools()
    store = EventStore(str(event_store_path))
    policy_run = store.create_run(f"{thread_id}-policy")
    policy = PolicyRegistry(registry, scenario_policy=ScenarioPolicyConfig(tool_denylist=["http_post"]))
    nodes = _make_nodes(store, checkpoint_path, policy=policy, registry=registry)
    policy_call = {
        "id": "call-policy-block",
        "name": "http_post",
        "args": {"path": "/api/jobs?cookie=secret", "json_body": {"x": 1}},
        "type": "tool_call",
    }
    await nodes.safety_gate(
        {"messages": [AIMessage(content="", tool_calls=[policy_call])]},
        {"configurable": {"thread_id": f"{thread_id}-policy", "run_id": str(policy_run["id"])}},
    )

    route_run = store.create_run(f"{thread_id}-route")
    route_policy = PolicyRegistry(registry, scenario_policy=ScenarioPolicyConfig(tool_allowlist=["http_post"]))
    route_nodes = _make_nodes(store, checkpoint_path, policy=route_policy, registry=registry)
    route_call = {
        "id": "call-route-block",
        "name": "http_post",
        "args": {"path": "/api/jobs?token=secret", "json_body": {"x": 1}},
        "type": "tool_call",
    }
    await route_nodes.safety_gate(
        {
            "messages": [AIMessage(content="", tool_calls=[route_call])],
            "tool_route": {"kind": "knowledge", "recommended_tools": ["search_docs"], "forbidden_tools": ["http_post"]},
        },
        {"configurable": {"thread_id": f"{thread_id}-route", "run_id": str(route_run["id"])}},
    )
    policy_events = store.list_run_events(str(policy_run["id"]))
    route_events = store.list_run_events(str(route_run["id"]))
    return {
        "policy": {
            "run_id": str(policy_run["id"]),
            "event_types": [event.get("type") for event in policy_events],
            "policy_events": [event.get("payload") or {} for event in policy_events if event.get("type") == EVENT_POLICY_DECISION_RECORDED],
            "side_effects": [event.get("payload") or {} for event in policy_events if event.get("type") == EVENT_TOOL_SIDE_EFFECT_RECORDED],
            "validation": validate_event_rows(policy_events),
        },
        "route": {
            "run_id": str(route_run["id"]),
            "event_types": [event.get("type") for event in route_events],
            "policy_events": [event.get("payload") or {} for event in route_events if event.get("type") == EVENT_POLICY_DECISION_RECORDED],
            "side_effects": [event.get("payload") or {} for event in route_events if event.get("type") == EVENT_TOOL_SIDE_EFFECT_RECORDED],
            "validation": validate_event_rows(route_events),
        },
    }


def _verify_rag_policy_audit(store: EventStore, thread_id: str) -> dict[str, Any]:
    run = store.create_run(f"{thread_id}-rag")
    secret_text = "SECRET_TOKEN_SHOULD_NOT_APPEAR"
    rag_store = RagStore(
        [
            _chunk("allowed.md", "redis stream official runbook", acl=["user:alice"]),
            _chunk("other-tenant.md", "redis stream tenant b", tenant_id="tenant-b", acl=["user:alice"]),
            _chunk("secret.md", f"redis stream confidential {secret_text}", acl=["user:alice"], classification="secret"),
            _chunk("acl-denied.md", "redis stream finance-only", acl=["group:finance"]),
            _chunk("high-pii.md", "redis stream phone number", acl=["user:alice"], pii_level="high"),
        ]
    )
    request = RetrievalRequest(
        tenant_id="tenant-a",
        user_id="alice",
        query="redis stream runbook policy",
        allowed_scopes=[],
        max_classification="internal",
        purpose="agent_context",
    )
    detailed, policy_result = rag_store.policy_aware_search(request, top_k=8)
    payload = RagSearchAdapter.to_retrieval_completed_payload(
        request.query,
        detailed.chunks,
        excerpt_chars=len(format_chunks_for_prompt(detailed.chunks)),
        call_id="rag-call",
        retrieval_mode=detailed.route.mode,
        retrieval_route=detailed.route.as_dict(),
        policy_result=policy_result,
    )
    retrieval_event = store.append_event(f"{thread_id}-rag", str(run["id"]), EVENT_RETRIEVAL_COMPLETED, payload)
    for policy_payload in build_rag_policy_decision_payloads(
        retrieval_event.get("payload") if isinstance(retrieval_event.get("payload"), dict) else {},
        related_event_id=int(retrieval_event.get("id") or 0) or None,
    ):
        store.append_event(f"{thread_id}-rag", str(run["id"]), EVENT_POLICY_DECISION_RECORDED, policy_payload)
    events = store.list_run_events(str(run["id"]))
    policy_events = [event.get("payload") or {} for event in events if event.get("type") == EVENT_POLICY_DECISION_RECORDED]
    return {
        "run_id": str(run["id"]),
        "policy_events": policy_events,
        "encoded": json.dumps(policy_events, ensure_ascii=False),
        "validation": validate_event_rows(events),
    }


def _verify_output_guard_policy(store: EventStore, thread_id: str) -> dict[str, Any]:
    run = store.create_run(f"{thread_id}-output")
    verdict = detect_sensitive_output("Leaked sk-1234567890abcdef and set-cookie: a=b")
    guard_payload = {
        "guard": "private_rag_output_v1",
        "safe": bool(verdict.get("safe")),
        "action": "degrade",
        "finding_count": int(verdict.get("finding_count") or 0),
        "findings": [str(item) for item in verdict.get("findings") or []],
        "original_chars": 48,
        "emitted_chars": 120,
    }
    policy_payload = build_output_guard_policy_decision_payload(guard_payload)
    if policy_payload is not None:
        store.append_event(f"{thread_id}-output", str(run["id"]), EVENT_POLICY_DECISION_RECORDED, policy_payload)
    events = store.list_run_events(str(run["id"]))
    return {
        "run_id": str(run["id"]),
        "policy": policy_payload or {},
        "validation": validate_event_rows(events),
    }


def _verify_timeline_and_bundle(
    store: EventStore,
    checkpoint_path: Path,
    *,
    run_id: str,
) -> dict[str, Any]:
    run = store.get_run(run_id) or {}
    events = store.list_run_events(run_id)
    timeline = TimelineProjector().project_run(run, events)
    bundle = build_debug_bundle(
        event_store_path=Path(store.path),
        checkpoint_path=checkpoint_path,
        run_id=run_id,
    )
    read_model = build_policy_read_model(run, events)
    return {
        "timeline_policy_count": len([item for item in timeline.get("items", []) if item.get("kind") == "policy"]),
        "warning_codes": [warning.get("code") for warning in timeline.get("warnings", [])],
        "debugger": timeline.get("debugger") or {},
        "bundle_policy_summary": bundle.get("policy_summary") or {},
        "read_model": read_model,
    }


def _verify_strict_contract() -> dict[str, Any]:
    valid = build_policy_decision_payload(
        scope="tool",
        source="unit",
        subject="http_post",
        action="tool_call",
        resource="/api/jobs?token=secret",
        decision="deny",
        reason="unit",
        metadata={"query": "raw user query", "cookie_header": "WMSESSIONID=secret"},
    )
    validated = validate_payload_for_kind(EVENT_POLICY_DECISION_RECORDED, valid)
    rejected_extra = False
    try:
        validate_payload_for_kind(EVENT_POLICY_DECISION_RECORDED, {**valid, "json_body": {"token": "secret"}})
    except PayloadValidationError:
        rejected_extra = True
    return {"valid": validated, "rejected_extra": rejected_extra}


async def verify(event_store_path: Path, checkpoint_path: Path, thread_prefix: str) -> dict[str, Any]:
    store = EventStore(str(event_store_path))
    tool_allow = await _verify_tool_allow(
        event_store_path.with_name(f"{event_store_path.stem}-allow.sqlite"),
        checkpoint_path.with_name(f"{checkpoint_path.stem}-allow.sqlite"),
        f"{thread_prefix}-allow",
    )
    ask = await _verify_tool_ask_and_approved(
        event_store_path.with_name(f"{event_store_path.stem}-ask.sqlite"),
        checkpoint_path.with_name(f"{checkpoint_path.stem}-ask.sqlite"),
        f"{thread_prefix}-ask",
    )
    blocked = await _verify_policy_and_route_block(
        event_store_path.with_name(f"{event_store_path.stem}-blocked.sqlite"),
        checkpoint_path,
        f"{thread_prefix}-blocked",
    )
    rag = _verify_rag_policy_audit(store, thread_prefix)
    output_guard = _verify_output_guard_policy(store, thread_prefix)
    timeline = _verify_timeline_and_bundle(store, checkpoint_path, run_id=output_guard["run_id"])
    strict = _verify_strict_contract()
    return {
        "tool_allow": tool_allow,
        "ask_approved": ask,
        "blocked": blocked,
        "rag": rag,
        "output_guard": output_guard,
        "timeline_bundle": timeline,
        "strict_contract": strict,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Policy Decision Audit v1.")
    parser.add_argument("--event-store-path", default=str(ROOT / "storage/verify-policy-decision-audit-events.sqlite"))
    parser.add_argument("--checkpoint-path", default=str(ROOT / "storage/verify-policy-decision-audit-checkpoints.sqlite"))
    parser.add_argument("--thread-prefix", default=f"policy-audit-{uuid.uuid4().hex[:8]}")
    parser.add_argument("--summary-json", default=str(ROOT / "artifacts/runtime/policy-decision-audit-v1-summary.json"))
    args = parser.parse_args()

    event_store_path = Path(args.event_store_path).resolve()
    checkpoint_path = Path(args.checkpoint_path).resolve()
    event_store_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    summary = asyncio.run(verify(event_store_path, checkpoint_path, str(args.thread_prefix)))
    allow = summary["tool_allow"]["policy"]
    ask = summary["ask_approved"]["ask"]
    approved_side_effect = summary["ask_approved"]["side_effect"]
    policy_block = summary["blocked"]["policy"]
    route_block = summary["blocked"]["route"]
    rag_events = summary["rag"]["policy_events"]
    output_policy = summary["output_guard"]["policy"]
    timeline = summary["timeline_bundle"]
    strict = summary["strict_contract"]
    checks = {
        "tool_allow_recorded": allow.get("scope") == "tool"
        and allow.get("decision") == "allow"
        and allow.get("resource") == "/actuator/health",
        "tool_ask_recorded": ask.get("decision") == "ask" and ask.get("requires_approval") is True,
        "approved_side_effect_linked": bool(approved_side_effect.get("policy_trace_id"))
        and approved_side_effect.get("policy_trace_id") == ask.get("policy_trace_id"),
        "policy_block_recorded": any(item.get("decision") == "deny" for item in policy_block["policy_events"])
        and "tool_start" not in policy_block["event_types"]
        and "tool_end" not in policy_block["event_types"],
        "policy_block_side_effect_linked": bool(policy_block["side_effects"])
        and policy_block["side_effects"][0].get("policy_trace_id") == policy_block["policy_events"][0].get("policy_trace_id"),
        "route_block_recorded": any(item.get("scope") == "route" and item.get("decision") == "block" for item in route_block["policy_events"])
        and "tool_start" not in route_block["event_types"]
        and "tool_end" not in route_block["event_types"],
        "rag_blocks_recorded": len(rag_events) >= 4
        and {"tenant_mismatch", "classification_exceeds_request", "acl_denied", "high_pii_blocked"}.issubset(
            {str(item.get("reason") or "") for item in rag_events}
        ),
        "rag_audit_sanitized": "SECRET_TOKEN_SHOULD_NOT_APPEAR" not in summary["rag"]["encoded"]
        and not audit_payload_has_secret(rag_events),
        "output_guard_recorded": output_policy.get("scope") == "output_guard"
        and output_policy.get("decision") in {"redact", "block"},
        "timeline_projected": timeline["timeline_policy_count"] >= 1
        and (timeline["debugger"].get("policy_decision_count") or 0) >= 1,
        "debug_bundle_policy_summary": (timeline["bundle_policy_summary"].get("total") or 0) >= 1,
        "strict_contract": strict["rejected_extra"] is True
        and strict["valid"].get("resource") == "/api/jobs"
        and not audit_payload_has_secret(strict["valid"]),
        "model_validation": summary["tool_allow"]["validation"].get("model_validate_ok") is True
        and summary["ask_approved"]["validation"].get("model_validate_ok") is True
        and policy_block["validation"].get("model_validate_ok") is True
        and route_block["validation"].get("model_validate_ok") is True
        and summary["rag"]["validation"].get("model_validate_ok") is True
        and summary["output_guard"]["validation"].get("model_validate_ok") is True,
    }
    passed = all(checks.values())
    summary["checks"] = checks
    summary["policy_decision_audit_v1"] = "PASS" if passed else "FAIL"
    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    for key, value in checks.items():
        print(f"{key}={value}")
    print(f"summary_json={summary_path}")
    print(f"policy_decision_audit_v1={summary['policy_decision_audit_v1']}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
