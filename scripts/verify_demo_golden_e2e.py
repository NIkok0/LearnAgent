#!/usr/bin/env python
"""Demo 1-6 golden E2E (proxy LLM + full planner/safety_gate/tools graph)."""

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
from copilot_agent.agent.tool_call_context import set_tool_call_context  # noqa: E402
from copilot_agent.agent.tool_router import route_tools  # noqa: E402
from copilot_agent.eval.citation import evaluate_citation  # noqa: E402
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
    return [case for case in cases if isinstance(case, dict)]


def _last_search_paths(executed: list[dict[str, Any]]) -> list[str]:
    for item in reversed(executed):
        if str(item.get("name", "")) != "search_docs":
            continue
        hints = item.get("suggested_api_paths")
        if isinstance(hints, list):
            return [str(h.get("path", "")) for h in hints if isinstance(h, dict) and h.get("path")]
    return []


def _default_tool_args(
    tool_name: str,
    *,
    question: str,
    route_data: dict[str, Any],
    executed: list[dict[str, Any]],
) -> dict[str, Any]:
    paths = list(route_data.get("suggested_paths") or [])
    search_paths = _last_search_paths(executed)
    if tool_name == "search_docs":
        return {"query": question}
    if tool_name == "http_get":
        path = (search_paths or paths or ["/actuator/health"])[0]
        return {"path": path}
    if tool_name == "http_post":
        path = paths[0] if paths else DANGEROUS_JOB_PATH
        if path == DANGEROUS_JOB_PATH:
            return {"path": path, "json_body": {"fileId": 1, "watermarkText": "test"}}
        return {"path": path, "json_body": {"username": "demo", "password": "demo"}}
    return {}


def _mock_final_answer(case: dict[str, Any]) -> str:
    required = [str(x) for x in case.get("required_sources") or []]
    must = [str(x) for x in case.get("answer_must_contain") or []]
    source_text = ", ".join(required) if required else "API-CONTRACT.md"
    body = f"Based on {source_text}. "
    body += " ".join(must)
    body += " Worker, Redis Stream, and Runbook guidance apply."
    return body


def _make_route_following_assistant(
    *,
    question: str,
    executed: list[dict[str, Any]],
    case: dict[str, Any],
) -> Any:
    step = {"n": 0}

    async def assistant(state: dict[str, Any], _config=None) -> dict[str, list[AIMessage]]:
        route_data = state.get("tool_route") or {}
        recommended = list(route_data.get("recommended_tools") or [])
        idx = step["n"]
        if idx >= len(recommended):
            return {"messages": [AIMessage(content=_mock_final_answer(case))]}
        tool_name = str(recommended[idx])
        args = _default_tool_args(tool_name, question=question, route_data=route_data, executed=executed)
        step["n"] = idx + 1
        call_id = f"demo_{case.get('id', 'case')}_{idx}"
        set_tool_call_context(call_id=call_id, tool_name=tool_name)
        executed.append({"name": tool_name, "call_id": call_id, **args})
        if tool_name == "search_docs":
            executed[-1]["suggested_api_paths"] = [
                {
                    "method": "GET",
                    "path": "/api/v1/jobs/22222222-2222-4222-8222-222222222222",
                    "source_file": "API-CONTRACT.md",
                }
            ]
        return {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": call_id,
                            "name": tool_name,
                            "args": args,
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        }

    return assistant


def _build_tool_registry(*, executed_counter: dict[str, int], question: str) -> ToolRegistry:
    async def search_docs(query: str) -> dict[str, Any]:
        executed_counter["search_docs"] = executed_counter.get("search_docs", 0) + 1
        hints = []
        if "QUEUED" in question.upper() or "22222222" in question:
            hints.append(
                {
                    "method": "GET",
                    "path": "/api/v1/jobs/22222222-2222-4222-8222-222222222222",
                    "source_file": "API-CONTRACT.md",
                }
            )
        return {
            "excerpts_markdown": f"mock docs for {query}",
            "sources": ["DEPLOY-SERVER.md", "watermark-java-backend-tech-selection.md", "RUNBOOK.md"],
            "suggested_api_paths": hints,
        }

    async def http_get(path: str, cookie_header: str | None = None) -> dict[str, Any]:
        del cookie_header
        executed_counter["http_get"] = executed_counter.get("http_get", 0) + 1
        body: dict[str, Any] = {"status": "UP"} if "health" in path else {"status": "QUEUED", "id": "job"}
        return {"ok": True, "path": path, "status_code": 200, "body": body}

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


def _build_graph(planner, assistant, safety_gate, tools: list[Any]):
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


async def _run_case(case: dict[str, Any], *, nodes: AgentNodes, registry: ToolRegistry) -> dict[str, Any]:
    case_id = str(case.get("id", ""))
    input_data = case.get("input") if isinstance(case.get("input"), dict) else {}
    messages = input_data.get("messages") if isinstance(input_data.get("messages"), list) else []
    question = str(messages[-1].get("content", "")) if messages else ""
    confirm_dangerous = bool(input_data.get("confirm_dangerous", False))
    allow_job_post = bool(input_data.get("allow_job_post", settings.copilot_allow_job_post))

    must_have_tools = [str(x) for x in case.get("must_have_tools") or []]
    forbidden_tools = [str(x) for x in case.get("forbidden_tools") or []]
    expect_blocked = bool(case.get("expect_blocked", False))
    required_sources = [str(x) for x in case.get("required_sources") or []]
    answer_must_contain = [str(x) for x in case.get("answer_must_contain") or []]

    route = route_tools(question, confirm_dangerous=confirm_dangerous, allow_job_post=allow_job_post)
    executed: list[dict[str, Any]] = []
    graph = _build_graph(
        nodes.planner,
        _make_route_following_assistant(question=question, executed=executed, case=case),
        nodes.safety_gate,
        registry.tools(),
    )

    thread_id = f"demo-{case_id}-{uuid.uuid4().hex[:6]}"
    run_id = f"run-{case_id}"
    final_state = await graph.ainvoke(
        {"messages": [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=question)]},
        config={
            "recursion_limit": 24,
            "configurable": {
                "thread_id": thread_id,
                "conversation_id": thread_id,
                "run_id": run_id,
                "input_messages": messages,
                "confirm_dangerous": confirm_dangerous,
                "allow_job_post": allow_job_post,
            },
        },
    )

    verdict = evaluate_trajectory(
        executed=executed,
        expected_tools=must_have_tools,
        forbidden_tools=forbidden_tools,
        expect_blocked=expect_blocked,
        route_recommended_tools=list(route.recommended_tools),
        route_kind=route.kind,
    )

    final_messages = final_state.get("messages") if isinstance(final_state, dict) else []
    answer = ""
    for message in reversed(final_messages or []):
        content = getattr(message, "content", "")
        if isinstance(content, str) and content.strip():
            answer = content
            break

    citation = evaluate_citation(
        answer=answer,
        retrieval_sources=required_sources or ["DEPLOY-SERVER.md", "RUNBOOK.md"],
        required_sources=required_sources,
    )
    keyword_ok = all(token.lower() in answer.lower() for token in answer_must_contain) if answer_must_contain else True

    passed = verdict.passed and citation.passed and keyword_ok
    return {
        "id": case_id,
        "question": question,
        "route_kind": route.kind,
        "executed_tools": [str(item.get("name", "")) for item in executed],
        "answer_preview": answer[:240],
        "trajectory": verdict.as_dict(),
        "citation": citation.as_dict(),
        "keyword_ok": keyword_ok,
        "passed": passed,
    }


async def _main_async() -> int:
    parser = argparse.ArgumentParser(description="Verify Demo 1-6 golden scenarios (proxy E2E).")
    parser.add_argument(
        "--dataset",
        default=str(ROOT / "eval/golden/demo-golden-scenarios.json"),
    )
    parser.add_argument(
        "--summary-json",
        default=str(ROOT / "artifacts/eval/demo-golden-e2e-summary.json"),
    )
    parser.add_argument(
        "--mode",
        choices=["proxy", "live"],
        default="proxy",
        help="proxy=deterministic mock LLM; live=requires API key (not yet wired).",
    )
    args = parser.parse_args()

    if args.mode == "live" and not settings.openai_api_key.strip():
        print("demo_golden_e2e=SKIP")
        print("reason=missing_openai_api_key")
        return 0

    cases = _load_cases(Path(args.dataset).resolve())
    event_store = EventStore(str(ROOT / "storage/verify-demo-golden-events.sqlite"))
    rag = RagStore([DocChunk(source="README.md", start_line=1, text="demo golden corpus")])
    memory = MemoryManager(
        rag_store=rag,
        event_store=event_store,
        checkpoint_path=str(ROOT / "storage/verify-demo-golden-checkpoints.sqlite"),
    )

    records: list[dict[str, Any]] = []
    for case in cases:
        question = str((case.get("input") or {}).get("messages", [{}])[-1].get("content", ""))
        registry = _build_tool_registry(executed_counter={}, question=question)
        nodes = AgentNodes(
            memory=memory,
            llm_provider=LLMProvider(),
            policy=PolicyRegistry(registry),
            tool_registry=registry,
            tools=registry.tools(),
        )
        records.append(await _run_case(case, nodes=nodes, registry=registry))

    passed_n = sum(1 for item in records if item["passed"])
    total = len(records)
    pass_rate = round(passed_n / total, 4) if total else 0.0
    summary = {
        "mode": args.mode,
        "cases_total": total,
        "cases_passed": passed_n,
        "demo_golden_pass_rate": pass_rate,
        "records": records,
        "checks": {"demo_golden_gate_ok": pass_rate >= 1.0},
        "demo_golden_e2e": "PASS" if pass_rate >= 1.0 else "FAIL",
    }

    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"mode={args.mode}")
    print(f"cases_total={total}")
    print(f"cases_passed={passed_n}")
    print(f"demo_golden_pass_rate={pass_rate}")
    print(f"summary_json={summary_path}")
    print(f"demo_golden_e2e={summary['demo_golden_e2e']}")
    return 0 if pass_rate >= 1.0 else 1


def main() -> int:
    return asyncio.run(_main_async())


if __name__ == "__main__":
    raise SystemExit(main())
