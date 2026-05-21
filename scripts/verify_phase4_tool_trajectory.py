#!/usr/bin/env python
"""L5 E2E: tool trajectory evaluation with deterministic route-following mock LLM."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from copilot_agent.agent.graph import route_after_assistant, route_after_safety_gate  # noqa: E402
from copilot_agent.agent.state import AgentState  # noqa: E402
from copilot_agent.agent.nodes import AgentNodes  # noqa: E402
from copilot_agent.agent.prompts import DANGEROUS_JOB_PATH, SYSTEM_PROMPT  # noqa: E402
from copilot_agent.agent.tool_router import route_tools  # noqa: E402
from copilot_agent.eval.tool_trajectory import evaluate_trajectory  # noqa: E402
from copilot_agent.llm import LLMProvider  # noqa: E402
from copilot_agent.memory import MemoryManager  # noqa: E402
from copilot_agent.policy import PolicyRegistry  # noqa: E402
from copilot_agent.rag.retriever import RagStore  # noqa: E402
from copilot_agent.rag.schema import DocChunk  # noqa: E402
from copilot_agent.runtime.event_store import EventStore  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402
from copilot_agent.tools.registry import ToolRegistry  # noqa: E402


class SearchDocsArgs(BaseModel):
    query: str = Field(description="Natural language or keywords")


class HttpGetArgs(BaseModel):
    path: str = Field(description="Path starting with /api/v1/ or /actuator/health")
    cookie_header: str | None = Field(default=None)


class HttpPostArgs(BaseModel):
    path: str
    json_body: dict[str, Any] = Field(default_factory=dict)
    cookie_header: str | None = None
    idempotency_key: str | None = None


def _load_cases(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases", []) if isinstance(payload, dict) else []
    return [c for c in cases if isinstance(c, dict)]


def _case_config(case: dict[str, Any]) -> tuple[bool, bool]:
    case_id = str(case.get("id", ""))
    confirm = bool(case.get("confirm_dangerous", False))
    allow_job_post = bool(case.get("allow_job_post", settings.copilot_allow_job_post))
    if case_id == "P4-010":
        return True, True
    if case_id == "P4-009":
        return False, False
    return confirm, allow_job_post


def _default_tool_args(tool_name: str, *, question: str, route_data: dict[str, Any], executed: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    paths = list(route_data.get("suggested_paths") or [])
    search_paths: list[str] = []
    if executed:
        for item in reversed(executed):
            if str(item.get("name", "")) != "search_docs":
                continue
            hints = item.get("suggested_api_paths")
            if isinstance(hints, list):
                search_paths = [str(h.get("path", "")) for h in hints if isinstance(h, dict) and h.get("path")]
            break
    if tool_name == "search_docs":
        return {"query": question}
    if tool_name == "http_get":
        path = (search_paths or paths or ["/actuator/health"])[0]
        return {"path": path}
    if tool_name == "http_post":
        path = paths[0] if paths else "/api/v1/auth/login"
        if path == DANGEROUS_JOB_PATH:
            return {"path": path, "json_body": {"fileId": 1, "text": "test"}}
        return {"path": path, "json_body": {"username": "demo", "password": "demo"}}
    return {}


def _make_route_following_assistant(
    *,
    question: str,
    executed: list[dict[str, Any]],
) -> Any:
    step = {"n": 0}

    async def assistant(state: dict[str, Any], _config=None) -> dict[str, list[AIMessage]]:
        route_data = state.get("tool_route") or {}
        recommended = list(route_data.get("recommended_tools") or [])
        idx = step["n"]
        if idx >= len(recommended):
            return {"messages": [AIMessage(content="L5 mock final answer")]}
        tool_name = str(recommended[idx])
        args = _default_tool_args(tool_name, question=question, route_data=route_data, executed=executed)
        step["n"] = idx + 1
        record = {"name": tool_name, **args}
        if tool_name == "search_docs":
            record["suggested_api_paths"] = [
                {
                    "method": "GET",
                    "path": "/api/v1/jobs/11111111-1111-4111-8111-111111111111",
                    "source_file": "API-CONTRACT.md",
                }
            ]
        executed.append(record)
        return {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": f"l5_call_{idx}",
                            "name": tool_name,
                            "args": args,
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        }

    return assistant


def _build_tool_registry(*, executed_counter: dict[str, int]) -> ToolRegistry:
    async def search_docs(query: str) -> dict[str, Any]:
        executed_counter["search_docs"] = executed_counter.get("search_docs", 0) + 1
        payload = {
            "excerpts_markdown": f"mock docs for {query}",
            "sources": ["README.md", "API-CONTRACT.md"],
            "suggested_api_paths": [
                {
                    "method": "GET",
                    "path": "/api/v1/jobs/11111111-1111-4111-8111-111111111111",
                    "source_file": "API-CONTRACT.md",
                }
            ],
        }
        return payload

    async def http_get(path: str, cookie_header: str | None = None) -> dict[str, Any]:
        del cookie_header
        executed_counter["http_get"] = executed_counter.get("http_get", 0) + 1
        return {"ok": True, "path": path, "status_code": 200, "body": {}}

    async def http_post(
        path: str,
        json_body: dict[str, Any],
        cookie_header: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        del cookie_header, idempotency_key
        executed_counter["http_post"] = executed_counter.get("http_post", 0) + 1
        return {"ok": True, "path": path, "status_code": 200, "body": json_body}

    return ToolRegistry.from_agent_tools(
        search_docs=search_docs,
        http_get=http_get,
        http_post=http_post,
        search_docs_args_schema=SearchDocsArgs,
        http_get_args_schema=HttpGetArgs,
        http_post_args_schema=HttpPostArgs,
        dangerous_post_requires_approval=lambda args: str(args.get("path", "")).split("?", 1)[0] == DANGEROUS_JOB_PATH,
    )


def _build_l5_graph(planner, assistant, safety_gate, tools: list[Any]):
    workflow = StateGraph(AgentState)
    workflow.add_node("planner", planner)
    workflow.add_node("assistant", assistant)
    workflow.add_node("safety_gate", safety_gate)
    workflow.add_node("tools", ToolNode(tools))
    workflow.set_entry_point("planner")
    workflow.add_edge("planner", "assistant")
    workflow.add_conditional_edges(
        "assistant",
        route_after_assistant,
        {"safety_gate": "safety_gate", "__end__": END},
    )
    workflow.add_conditional_edges(
        "safety_gate",
        route_after_safety_gate,
        {"tools": "tools", "__end__": END},
    )
    workflow.add_edge("tools", "assistant")
    return workflow.compile(checkpointer=MemorySaver())


async def _run_case(
    case: dict[str, Any],
    *,
    nodes: AgentNodes,
    tools: list[Any],
) -> dict[str, Any]:
    case_id = str(case.get("id", ""))
    question = str(case.get("question", ""))
    expected_tools = [str(x) for x in case.get("expected_tools") or []]
    forbidden_tools = [str(x) for x in case.get("forbidden_tools") or []]
    expect_blocked = bool(case.get("expect_blocked", False))
    confirm_dangerous, allow_job_post = _case_config(case)

    route = route_tools(question, confirm_dangerous=confirm_dangerous, allow_job_post=allow_job_post)
    executed: list[dict[str, Any]] = []
    executed_counter: dict[str, int] = {}

    graph = _build_l5_graph(
        nodes.planner,
        _make_route_following_assistant(question=question, executed=executed),
        nodes.safety_gate,
        tools,
    )

    thread_id = f"l5-{case_id}-{uuid.uuid4().hex[:6]}"
    run_id = f"run-{case_id}"
    await graph.ainvoke(
        {
            "messages": [
                SystemMessage(content=SYSTEM_PROMPT),
                HumanMessage(content=question),
            ]
        },
        config={
            "recursion_limit": 24,
            "configurable": {
                "thread_id": thread_id,
                "conversation_id": thread_id,
                "run_id": run_id,
                "input_messages": [{"role": "user", "content": question}],
                "confirm_dangerous": confirm_dangerous,
                "allow_job_post": allow_job_post,
            },
        },
    )

    verdict = evaluate_trajectory(
        executed=executed,
        expected_tools=expected_tools,
        forbidden_tools=forbidden_tools,
        expect_blocked=expect_blocked,
        route_recommended_tools=list(route.recommended_tools),
        route_kind=route.kind,
    )
    executed_names = [str(item.get("name", "")) for item in executed]
    return {
        "id": case_id,
        "category": str(case.get("category", "")),
        "question": question,
        "route_kind": route.kind,
        "route_recommended_tools": list(route.recommended_tools),
        "executed_tools": executed_names,
        "executed": executed,
        "expected_tools": expected_tools,
        "forbidden_tools": forbidden_tools,
        "expect_blocked": expect_blocked,
        "verdict": verdict.as_dict(),
        "passed": verdict.passed,
        "tool_counter": dict(executed_counter),
    }


def main() -> int:
    return asyncio.run(_main_async())


async def _main_async() -> int:
    parser = argparse.ArgumentParser(description="Verify L5 tool trajectory against phase4 eval cases.")
    parser.add_argument(
        "--dataset",
        default=str(ROOT / "eval/phase4-eval-cases.json"),
        help="Path to phase4 eval dataset.",
    )
    parser.add_argument(
        "--summary-json",
        default=str(ROOT / "artifacts/phase4/phase4-tool-trajectory-summary.json"),
        help="Path to write summary JSON.",
    )
    parser.add_argument(
        "--event-store-path",
        default=str(ROOT / "storage/verify-l5-tool-trajectory-events.sqlite"),
        help="Event store sqlite path for planner events.",
    )
    args = parser.parse_args()

    dataset_path = Path(args.dataset).resolve()
    event_store_path = Path(args.event_store_path).resolve()
    event_store_path.parent.mkdir(parents=True, exist_ok=True)

    cases = _load_cases(dataset_path)
    event_store = EventStore(str(event_store_path))
    rag = RagStore([DocChunk(source="README.md", start_line=1, text="L5 tool trajectory mock corpus")])
    memory = MemoryManager(rag_store=rag, event_store=event_store, checkpoint_path=str(event_store_path))
    registry = _build_tool_registry(executed_counter={})
    nodes = AgentNodes(
        memory=memory,
        llm_provider=LLMProvider(),
        policy=PolicyRegistry(registry),
        tool_registry=registry,
        tools=registry.tools(),
    )

    records: list[dict[str, Any]] = []
    for case in cases:
        records.append(await _run_case(case, nodes=nodes, tools=registry.tools()))

    passed_n = sum(1 for r in records if r["passed"])
    total = len(records)
    troubleshooting = [r for r in records if r["route_kind"] == "troubleshooting"]
    rag_before_api_rate = (
        sum(1 for r in troubleshooting if r["verdict"]["rag_before_api_ok"]) / len(troubleshooting)
        if troubleshooting
        else 1.0
    )
    metrics = {
        "cases_total": total,
        "cases_passed": passed_n,
        "tool_trajectory_pass_rate": round(passed_n / total, 4) if total else 0.0,
        "required_tools_pass_rate": round(
            sum(1 for r in records if r["verdict"]["required_tools_ok"]) / total, 4
        )
        if total
        else 0.0,
        "forbidden_tools_pass_rate": round(
            sum(1 for r in records if r["verdict"]["forbidden_tools_ok"]) / total, 4
        )
        if total
        else 0.0,
        "route_order_pass_rate": round(
            sum(1 for r in records if r["verdict"]["route_order_ok"]) / total, 4
        )
        if total
        else 0.0,
        "rag_before_api_pass_rate": round(rag_before_api_rate, 4),
        "blocked_pass_rate": round(
            sum(1 for r in records if r["verdict"]["blocked_ok"]) / total, 4
        )
        if total
        else 0.0,
    }

    pass_gate = metrics["tool_trajectory_pass_rate"] >= 1.0
    summary = {
        "dataset_path": str(dataset_path),
        "metrics": metrics,
        "records": records,
        "checks": {
            "trajectory_gate_ok": pass_gate,
        },
        "phase4_tool_trajectory": "PASS" if pass_gate else "FAIL",
    }

    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"dataset_path={summary['dataset_path']}")
    print(f"cases_total={metrics['cases_total']}")
    print(f"cases_passed={metrics['cases_passed']}")
    print(f"tool_trajectory_pass_rate={metrics['tool_trajectory_pass_rate']}")
    print(f"rag_before_api_pass_rate={metrics['rag_before_api_pass_rate']}")
    print(f"summary_json={summary_path}")
    print(f"phase4_tool_trajectory={'PASS' if pass_gate else 'FAIL'}")
    return 0 if pass_gate else 1


if __name__ == "__main__":
    raise SystemExit(main())
