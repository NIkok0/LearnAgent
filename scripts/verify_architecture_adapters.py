#!/usr/bin/env python
"""Verify first-pass architecture adapters without calling an external LLM."""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.agent.graph import build_agent_graph  # noqa: E402
from copilot_agent.llm import LLMProvider  # noqa: E402
from copilot_agent.memory import MemoryManager  # noqa: E402
from copilot_agent.policy import PolicyRegistry  # noqa: E402
from copilot_agent.rag.schema import DocChunk  # noqa: E402
from copilot_agent.rag.retriever import RagStore  # noqa: E402
from copilot_agent.runtime.event_store import EventStore  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402
from copilot_agent.tools.registry import ToolRegistry  # noqa: E402


class EchoArgs(BaseModel):
    text: str = Field(description="Echo payload")


async def _echo_tool(text: str) -> dict[str, Any]:
    return {"ok": True, "text": text}


def _build_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_async(
        name="search_docs",
        description="Mock search_docs",
        coroutine=_echo_tool,
        args_schema=EchoArgs,
        category="memory",
        risk_level="low",
        requires_approval=False,
        timeout_seconds=30.0,
    )
    registry.register_async(
        name="http_get",
        description="Mock http_get",
        coroutine=_echo_tool,
        args_schema=EchoArgs,
        category="http",
        risk_level="medium",
        requires_approval=False,
        timeout_seconds=60.0,
    )
    registry.register_async(
        name="http_post",
        description="Mock http_post",
        coroutine=_echo_tool,
        args_schema=EchoArgs,
        category="http",
        risk_level="high",
        requires_approval=lambda args: str(args.get("path", "")).split("?", 1)[0] == "/api/v1/jobs/watermark",
        timeout_seconds=120.0,
    )
    return registry


def _assistant_node(state: dict[str, Any]) -> dict[str, list[AIMessage]]:
    msgs = state.get("messages", [])
    if msgs and isinstance(msgs[-1], HumanMessage):
        return {"messages": [AIMessage(content="adapter-ok")]}
    return {"messages": [AIMessage(content="done")]}


def _safety_gate_node(_state: dict[str, Any], _config: dict[str, Any] | None = None) -> dict[str, list[AIMessage]]:
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify LearnAgent architecture adapters.")
    parser.add_argument(
        "--event-store-path",
        default=settings.agent_event_store_path,
        help="SQLite event store path.",
    )
    parser.add_argument(
        "--checkpoint-path",
        default=settings.agent_checkpoint_path,
        help="SQLite checkpoint path.",
    )
    parser.add_argument(
        "--thread-id",
        default=f"architecture-adapters-{uuid.uuid4().hex[:8]}",
        help="Thread id for verification.",
    )
    args = parser.parse_args()

    event_store = EventStore(str(Path(args.event_store_path).resolve()))
    rag = RagStore([DocChunk(source="README.md", start_line=1, text="Redis deployment guide")])
    memory = MemoryManager(
        rag_store=rag,
        event_store=event_store,
        checkpoint_path=str(Path(args.checkpoint_path).resolve()),
    )

    registry = _build_tool_registry()
    tool_names = registry.names()
    tool_specs = registry.public_specs()

    llm_provider = LLMProvider()
    llm_metadata = llm_provider.metadata()

    policy = PolicyRegistry(registry)
    dangerous_decision = policy.evaluate_tool_calls(
        [
            {
                "name": "http_post",
                "args": {"path": "/api/v1/jobs/watermark"},
            }
        ],
        allow_job_post=True,
        confirm_dangerous=False,
    )
    safe_decision = policy.evaluate_tool_calls(
        [{"name": "search_docs", "args": {"query": "Redis"}}],
        allow_job_post=True,
        confirm_dangerous=False,
    )

    memory.append_event(args.thread_id, "adapter-run", "plan_created", {"goal": "verify adapters"})
    memory.append_event(args.thread_id, "adapter-run", "token", {"text": "adapter memory output"})
    memory.append_event(
        args.thread_id,
        "adapter-run",
        "tool_start",
        {"name": "search_docs", "category": "memory", "risk_level": "low", "arguments": {"query": "Redis"}},
    )
    memory.append_event(args.thread_id, "adapter-run", "done", {})
    memory_events = memory.get_thread_events(args.thread_id, run_id="adapter-run")
    memory_hits = memory.search_docs("Redis", top_k=1)
    run_summary = memory.summarize_run(args.thread_id, "adapter-run", fallback_goal="verify adapters")
    thread_summary = memory.update_thread_summary(args.thread_id, "adapter-run")
    memory_context = memory.build_context(
        thread_id=args.thread_id,
        run_id="adapter-run",
        messages=[{"role": "user", "content": "verify adapters"}],
        goal="verify adapters",
    )

    def _planner_node(_state: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, list[AIMessage]]:
        ctx = (config.get("configurable") or {}) if config else {}
        memory.append_event(
            str(ctx.get("thread_id", args.thread_id)),
            str(ctx.get("run_id", "graph-run")),
            "plan_created",
            {"goal": "graph verify", "strategy": "observe_only", "available_tools": registry.public_specs()},
        )
        return {}

    graph = build_agent_graph(
        _planner_node,
        _assistant_node,
        _safety_gate_node,
        [StructuredTool.from_function(name="echo_tool", description="Echo", coroutine=_echo_tool, args_schema=EchoArgs)],
        checkpoint_path=str(Path(args.checkpoint_path).resolve()),
    )
    out = graph.invoke(
        {"messages": [HumanMessage(content="hello")]},
        config={"configurable": {"thread_id": args.thread_id, "run_id": "graph-run"}},
    )
    graph_events = memory.get_thread_events(args.thread_id, run_id="graph-run")

    checks = {
        "tool_registry_names": tool_names == ["search_docs", "http_get", "http_post"],
        "tool_registry_specs": (
            len(tool_specs) == 3
            and all("category" in spec and "risk_level" in spec and "timeout_seconds" in spec for spec in tool_specs)
            and registry.get_spec("http_post").requires_approval_for({"path": "/api/v1/jobs/watermark"})
        ),
        "llm_metadata": llm_metadata.get("model_name") == settings.openai_model,
        "policy_dangerous_requires_approval": dangerous_decision.requires_approval and not dangerous_decision.allowed,
        "policy_safe_allowed": safe_decision.allowed,
        "memory_facade": bool(memory_events and memory_hits),
        "memory_summary": bool(
            run_summary.get("source_event_ids")
            and thread_summary.get("source_run_ids")
            and memory_context.episodic.get("thread_summary")
        ),
        "plan_event": any(
            e["type"] == "plan_created"
            and (e["payload"].get("available_tools") or [{}])[0].get("category")
            for e in graph_events
        ),
        "graph_completed": bool(out.get("messages")),
    }
    passed = all(checks.values())

    for key, value in checks.items():
        print(f"{key}={value}")
    print(f"architecture_adapters={'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
