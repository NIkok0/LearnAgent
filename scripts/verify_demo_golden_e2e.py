#!/usr/bin/env python
"""Demo 1-6 golden E2E (proxy LLM + full planner/safety_gate/tools graph)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
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

from copilot_agent.agent.final_answer import build_final_answer  # noqa: E402
from copilot_agent.agent.graph import close_graph_checkpointer, route_after_assistant, route_after_safety_gate  # noqa: E402
from copilot_agent.agent.state import AgentState  # noqa: E402
from copilot_agent.agent.nodes import AgentNodes  # noqa: E402
from copilot_agent.context import ContextManager  # noqa: E402
from copilot_agent.scenario import load_scenario  # noqa: E402
from copilot_agent.agent.prompts import DEFAULT_KERNEL_PROMPT  # noqa: E402
from copilot_agent.agent.tool_call_context import set_tool_call_context  # noqa: E402
from copilot_agent.credentials import CredentialManager  # noqa: E402
from copilot_agent.eval.citation import evaluate_citation  # noqa: E402
from copilot_agent.eval.tool_trajectory import evaluate_trajectory  # noqa: E402
from copilot_agent.llm import LLMProvider  # noqa: E402
from copilot_agent.memory import MemoryManager  # noqa: E402
from copilot_agent.policy import PolicyRegistry  # noqa: E402
from copilot_agent.rag.retriever import RagStore  # noqa: E402
from copilot_agent.rag.schema import DocChunk  # noqa: E402
from copilot_agent.runtime.event_store import EventStore  # noqa: E402
from copilot_agent.agent.message_utils import current_turn_messages, last_user_content  # noqa: E402
from copilot_agent.agent.stream.event_mapper import GraphEventMapper  # noqa: E402
from copilot_agent.eval.llm_client import ensure_eval_api_env  # noqa: E402
from copilot_agent.kernel import KernelDeps, build_kernel_components  # noqa: E402
from copilot_agent.rag import build_rag_store  # noqa: E402
from copilot_agent.rag.request_context import merge_retrieval_scopes  # noqa: E402
from copilot_agent.runtime.checkpoint_reader import CheckpointReader  # noqa: E402
from copilot_agent.runtime.execution_engine import GraphInterrupted  # noqa: E402
from copilot_agent.scenario.bootstrap import apply_scenario_environment  # noqa: E402
from copilot_agent.scenario.router import route_tools  # noqa: E402
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


def _watermark_dangerous_path() -> str:
    scenario = load_scenario("watermark")
    if scenario.policy.dangerous_paths:
        return str(scenario.policy.dangerous_paths[0])
    if scenario.router_rules and scenario.router_rules.dangerous_job_path:
        return scenario.router_rules.dangerous_job_path
    raise RuntimeError("watermark scenario must declare a dangerous path")


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
        dangerous_path = _watermark_dangerous_path()
        path = paths[0] if paths else dangerous_path
        if path == dangerous_path:
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
            "success": True,
            "data": {
                "excerpts_markdown": f"mock docs for {query}",
                "sources": ["DEPLOY-SERVER.md", "watermark-java-backend-tech-selection.md", "RUNBOOK.md"],
                "citations": [
                    {
                        "source_file": "DEPLOY-SERVER.md",
                        "heading_path": "Deploy",
                        "start_line": 1,
                        "chunk_id": "DEPLOY-SERVER.md:1:demo",
                        "authority": 90,
                    }
                ],
                "suggested_api_paths": hints,
            },
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
        dangerous_post_requires_approval=lambda args: str(args.get("path", "")).split("?", 1)[0] == _watermark_dangerous_path(),
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


class _DemoStubHttpClient:
    """Deterministic HTTP stub for live LLM E2E (no Java backend required)."""

    async def http_get(
        self,
        path: str,
        cookie_header: str | None = None,
        stored_cookie: str | None = None,
    ) -> dict[str, Any]:
        del cookie_header, stored_cookie
        base = path.split("?", 1)[0]
        if base == "/actuator/health":
            return {"ok": True, "status_code": 200, "body": {"status": "UP"}}
        if base.startswith("/api/v1/jobs/"):
            return {
                "ok": True,
                "status_code": 200,
                "body": {"status": "QUEUED", "jobId": base.rsplit("/", 1)[-1]},
            }
        return {"ok": False, "status_code": 404, "error": f"stub path not found: {base}"}

    async def http_post(
        self,
        path: str,
        json_body: dict[str, Any],
        cookie_header: str | None = None,
        stored_cookie: str | None = None,
        idempotency_key: str | None = None,
        *,
        allow_job_post: bool,
        user_confirmed_dangerous: bool,
    ) -> dict[str, Any]:
        del cookie_header, stored_cookie, idempotency_key
        base = path.split("?", 1)[0]
        if base == _watermark_dangerous_path():
            if not allow_job_post:
                return {
                    "ok": False,
                    "error": "POST disabled (set COPILOT_ALLOW_JOB_POST=true and confirm_dangerous).",
                }
            if not user_confirmed_dangerous:
                return {"ok": False, "error": "Dangerous POST requires confirm_dangerous=true."}
        return {"ok": True, "status_code": 200, "body": {"path": base, **json_body}}


def _record_from_tool_event(
    *,
    kind: str,
    payload: dict[str, Any],
    pending_args: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    if kind == "tool_start":
        call_id = str(payload.get("call_id") or "")
        args = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
        if call_id:
            pending_args[call_id] = dict(args)
        return None
    if kind != "tool_end":
        return None
    name = str(payload.get("name") or "")
    call_id = str(payload.get("call_id") or "")
    args = pending_args.pop(call_id, {})
    record: dict[str, Any] = {"name": name, "call_id": call_id, **args}
    if name in {"http_get", "http_post"} and args.get("path"):
        record["path"] = str(args.get("path"))
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    hints = data.get("suggested_api_paths")
    if isinstance(hints, list):
        record["suggested_api_paths"] = hints
    return record


async def _run_case_live(case: dict[str, Any], *, artifact_dir: Path) -> dict[str, Any]:
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

    apply_scenario_environment(load_scenario("watermark"))
    scenario = load_scenario("watermark")
    route = route_tools(
        question,
        engine=scenario.router_engine,
        confirm_dangerous=confirm_dangerous,
        allow_job_post=allow_job_post,
    )

    previous_caps = settings.copilot_capabilities
    settings.copilot_capabilities = "rag,http"
    graph = None
    try:
        run_stamp = uuid.uuid4().hex[:8]
        event_store_path = artifact_dir / f"demo-live-{case_id}-{run_stamp}-events.sqlite"
        checkpoint_path = artifact_dir / f"demo-live-{case_id}-{run_stamp}-checkpoints.sqlite"

        event_store = EventStore(str(event_store_path))
        rag_store = build_rag_store()
        credentials = CredentialManager.from_scenario_resources(scenario.resources, ttl_seconds=3600)
        memory = MemoryManager(
            rag_store=rag_store,
            event_store=event_store,
            checkpoint_path=str(checkpoint_path),
            policy=scenario.resolve_memory_policy(),
        )
        kernel = build_kernel_components(
            scenario,
            KernelDeps(
                rag_store=rag_store,
                credential_manager=credentials,
                event_store=event_store,
                http=_DemoStubHttpClient(),
                memory=memory,
            ),
        )
        context_manager = ContextManager(
            scenario=scenario,
            memory=memory,
            tool_registry=kernel.tool_registry,
            router_engine=scenario.router_engine,
            credential_manager=credentials,
        )
        nodes = AgentNodes(
            memory=memory,
            llm_provider=kernel.llm_provider,
            policy=kernel.policy,
            tool_registry=kernel.tool_registry,
            tools=kernel.tools,
            context_manager=context_manager,
        )
        graph = build_agent_graph(
            nodes.planner,
            nodes.assistant,
            nodes.safety_gate,
            kernel.tools,
            checkpoint_path=str(checkpoint_path),
            async_checkpoint=True,
        )
        context_manager.bind_graph(graph)
        mapper = GraphEventMapper(
            memory=memory,
            tool_registry=kernel.tool_registry,
            checkpoint_reader=CheckpointReader(graph),
        )

        thread_id = f"demo-live-{case_id}-{uuid.uuid4().hex[:6]}"
        run_id = f"run-{case_id}"
        turn_messages = current_turn_messages(messages)
        goal = last_user_content(turn_messages)
        lc_turn = [HumanMessage(content=goal)] if goal else [HumanMessage(content=question)]

        bundle = await context_manager.assemble(
            thread_id=thread_id,
            run_id=run_id,
            turn_messages=lc_turn,
            goal=goal,
            confirm_dangerous=confirm_dangerous,
            allow_job_post=allow_job_post,
        )
        tool_route = next(
            (hint.get("tool_route") for hint in bundle.policy_hints if isinstance(hint, dict) and hint.get("tool_route")),
            route.as_dict(),
        )

        executed: list[dict[str, Any]] = []
        pending_args: dict[str, dict[str, Any]] = {}
        answer = ""
        done_final_answer: dict[str, Any] = {}
        graph_config = {
            "recursion_limit": (scenario.budgets.max_graph_rounds * 2) + 4,
            "configurable": {
                "thread_id": thread_id,
                "conversation_id": thread_id,
                "run_id": run_id,
                "input_messages": turn_messages,
                "confirm_dangerous": confirm_dangerous,
                "allow_job_post": allow_job_post,
                "preretrieval_cache": bundle.truncation_report.get("preretrieval_cache"),
                "tool_route": tool_route,
                "tenant_id": scenario.resources.default_tenant_id,
                "max_classification": scenario.resources.default_max_classification,
                "allowed_scopes": list(
                    merge_retrieval_scopes(
                        credential_manager=credentials,
                        scenario=scenario,
                        user_id=memory.resolve_user_id(thread_id),
                    )
                ),
            },
        }

        try:
            async for runtime_event in mapper.map(
                graph=graph,
                graph_input={"messages": bundle.graph_messages},
                graph_config=graph_config,
                thread_id=thread_id,
                run_id=run_id,
            ):
                payload = runtime_event.data if isinstance(runtime_event.data, dict) else {}
                record = _record_from_tool_event(
                    kind=runtime_event.kind,
                    payload=payload,
                    pending_args=pending_args,
                )
                if record is not None:
                    executed.append(record)
                if runtime_event.kind == "token":
                    answer += str(runtime_event.content or payload.get("text") or "")
                if runtime_event.kind == "done":
                    raw = payload.get("final_answer")
                    if isinstance(raw, dict):
                        done_final_answer = raw
        except GraphInterrupted:
            pass

        if not answer.strip():
            state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
            values = getattr(state, "values", None) or {}
            checkpoint_messages = values.get("messages") if isinstance(values, dict) else []
            for message in reversed(checkpoint_messages or []):
                content = getattr(message, "content", "")
                if isinstance(content, str) and content.strip():
                    answer = content
                    break

        verdict = evaluate_trajectory(
            executed=executed,
            expected_tools=must_have_tools,
            forbidden_tools=forbidden_tools,
            expect_blocked=expect_blocked,
            route_recommended_tools=list(route.recommended_tools),
            route_kind=route.kind,
            strict_route_order=False,
            strict_tool_order=False,
        )
        citation = evaluate_citation(
            answer=answer,
            retrieval_sources=required_sources or ["DEPLOY-SERVER.md", "RUNBOOK.md"],
            required_sources=required_sources,
        )
        keyword_ok = all(token.lower() in answer.lower() for token in answer_must_contain) if answer_must_contain else True
        final_answer_ok = True
        if "search_docs" in must_have_tools and not expect_blocked:
            citations = done_final_answer.get("citations") if isinstance(done_final_answer.get("citations"), list) else []
            final_answer_ok = len(citations) > 0

        passed = verdict.passed and citation.passed and keyword_ok and final_answer_ok
        return {
            "id": case_id,
            "question": question,
            "route_kind": route.kind,
            "executed_tools": [str(item.get("name", "")) for item in executed],
            "answer_preview": answer[:240],
            "trajectory": verdict.as_dict(),
            "citation": citation.as_dict(),
            "keyword_ok": keyword_ok,
            "final_answer_ok": final_answer_ok,
            "passed": passed,
        }
    finally:
        await close_graph_checkpointer(graph)
        settings.copilot_capabilities = previous_caps


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

    route = route_tools(
        question,
        engine=load_scenario("watermark").router_engine,
        confirm_dangerous=confirm_dangerous,
        allow_job_post=allow_job_post,
    )
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
        {"messages": [SystemMessage(content=DEFAULT_KERNEL_PROMPT), HumanMessage(content=question)]},
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
    final_answer_ok = True
    if "search_docs" in must_have_tools and not expect_blocked:
        final_answer = build_final_answer(
            answer=answer,
            messages=list(final_messages or []),
            route_kind=route.kind,
        )
        final_answer_ok = len(final_answer.citations) > 0

    passed = verdict.passed and citation.passed and keyword_ok and final_answer_ok
    return {
        "id": case_id,
        "question": question,
        "route_kind": route.kind,
        "executed_tools": [str(item.get("name", "")) for item in executed],
        "answer_preview": answer[:240],
        "trajectory": verdict.as_dict(),
        "citation": citation.as_dict(),
        "keyword_ok": keyword_ok,
        "final_answer_ok": final_answer_ok,
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
        help="proxy=deterministic mock LLM; live=real LLM + RAG + stub HTTP.",
    )
    parser.add_argument(
        "--live-min-pass-rate",
        type=float,
        default=0.834,
        help="Minimum pass rate for live mode (default 5/6 cases).",
    )
    args = parser.parse_args()

    if args.mode == "live" and not ensure_eval_api_env():
        print("demo_golden_e2e=SKIP")
        print("reason=missing_openai_api_key")
        return 0

    cases = _load_cases(Path(args.dataset).resolve())
    records: list[dict[str, Any]] = []

    if args.mode == "live":
        artifact_dir = Path(args.summary_json).resolve().parent / "demo-live-runs"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("SCENARIO", "watermark")
        for case in cases:
            records.append(await _run_case_live(case, artifact_dir=artifact_dir))
    else:
        event_store = EventStore(str(ROOT / "storage/verify-demo-golden-events.sqlite"))
        rag = RagStore([DocChunk(source="README.md", start_line=1, text="demo golden corpus")])
        memory = MemoryManager(
            rag_store=rag,
            event_store=event_store,
            checkpoint_path=str(ROOT / "storage/verify-demo-golden-checkpoints.sqlite"),
        )

        for case in cases:
            question = str((case.get("input") or {}).get("messages", [{}])[-1].get("content", ""))
            registry = _build_tool_registry(executed_counter={}, question=question)
            scenario = load_scenario("watermark")
            credentials = CredentialManager.from_scenario_resources(scenario.resources, ttl_seconds=120)
            context_manager = ContextManager(
                scenario=scenario,
                memory=memory,
                tool_registry=registry,
            )
            nodes = AgentNodes(
                memory=memory,
                llm_provider=LLMProvider(),
                policy=PolicyRegistry(
                    registry,
                    scenario_policy=scenario.policy,
                    credential_manager=credentials,
                ),
                tool_registry=registry,
                tools=registry.tools(),
                context_manager=context_manager,
            )
            records.append(await _run_case(case, nodes=nodes, registry=registry))

    passed_n = sum(1 for item in records if item["passed"])
    total = len(records)
    pass_rate = round(passed_n / total, 4) if total else 0.0
    gate_ok = pass_rate >= 1.0 if args.mode == "proxy" else pass_rate >= args.live_min_pass_rate
    summary = {
        "mode": args.mode,
        "cases_total": total,
        "cases_passed": passed_n,
        "demo_golden_pass_rate": pass_rate,
        "records": records,
        "checks": {"demo_golden_gate_ok": gate_ok},
        "demo_golden_e2e": "PASS" if gate_ok else "FAIL",
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
