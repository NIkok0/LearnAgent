from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from copilot_agent.agent.state import AgentState

log = logging.getLogger(__name__)


def route_after_assistant(state: AgentState) -> str:
    msgs = state.get("messages", [])
    if not msgs:
        return "__end__"
    last = msgs[-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "safety_gate"
    return "__end__"


def route_after_safety_gate(state: AgentState) -> str:
    msgs = state.get("messages", [])
    if not msgs:
        return "__end__"
    last = msgs[-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return "__end__"


def _build_checkpointer(checkpoint_path: str, *, async_checkpoint: bool = False):
    p = Path(checkpoint_path).expanduser().resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    if async_checkpoint:
        try:
            import aiosqlite
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        except Exception:
            log.warning("async sqlite checkpoint unavailable; using in-memory checkpoint")
            return MemorySaver()
        conn = aiosqlite.connect(str(p))
        if not hasattr(conn, "is_alive"):
            conn.is_alive = lambda: bool(getattr(conn, "_connection", None))  # type: ignore[attr-defined]
        saver = AsyncSqliteSaver(conn)
        saver._learnagent_conn = conn  # type: ignore[attr-defined]
        return saver
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except Exception:
        log.warning("langgraph-checkpoint-sqlite unavailable; using in-memory checkpoint")
        return MemorySaver()
    conn = sqlite3.connect(str(p), check_same_thread=False)
    return SqliteSaver(conn)


def build_agent_graph(
    planner_node,
    assistant_node,
    safety_gate_node,
    tools,
    *,
    checkpoint_path: str,
    async_checkpoint: bool = False,
):
    workflow = StateGraph(AgentState)
    workflow.add_node("planner", planner_node)
    workflow.add_node("assistant", assistant_node)
    workflow.add_node("safety_gate", safety_gate_node)
    workflow.add_node("tools", ToolNode(tools))
    workflow.set_entry_point("planner")
    workflow.add_edge("planner", "assistant")
    workflow.add_conditional_edges(
        "assistant",
        route_after_assistant,
        {
            "safety_gate": "safety_gate",
            "__end__": END,
        },
    )
    workflow.add_conditional_edges(
        "safety_gate",
        route_after_safety_gate,
        {
            "tools": "tools",
            "__end__": END,
        },
    )
    workflow.add_edge("tools", "assistant")
    return workflow.compile(checkpointer=_build_checkpointer(checkpoint_path, async_checkpoint=async_checkpoint))
