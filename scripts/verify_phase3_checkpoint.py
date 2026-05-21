#!/usr/bin/env python
"""Phase 3 Step 4: verify LangGraph checkpoint persistence and recovery.

This script validates that:
1) Graph state is persisted to SQLite checkpoint storage.
2) A second turn with the same thread_id resumes prior state.

It uses a deterministic mock assistant/tool flow and does NOT require OPENAI_API_KEY.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.agent.graph import build_agent_graph  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402


class EchoArgs(BaseModel):
    text: str = Field(description="Echo payload")


def _assistant_node(state: dict[str, Any]) -> dict[str, list[AIMessage]]:
    msgs = state.get("messages", [])
    if not msgs:
        return {"messages": [AIMessage(content="assistant_ready")]}
    last = msgs[-1]
    if isinstance(last, HumanMessage):
        content = str(last.content or "")
        if "tool" in content.lower():
            return {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "call_echo_1",
                                "name": "echo_tool",
                                "args": {"text": "pong"},
                                "type": "tool_call",
                            }
                        ],
                    )
                ]
            }
        return {"messages": [AIMessage(content=f"ack:{content}")]}
    if isinstance(last, ToolMessage):
        return {"messages": [AIMessage(content=f"tool_result:{last.content}")]}
    return {"messages": [AIMessage(content="assistant_continue")]}


def _safety_gate_node(state: dict[str, Any], _config: dict[str, Any] | None = None) -> dict[str, list[AIMessage]]:
    # Step 4 script focuses on checkpoint resume; gate is pass-through.
    return {}


def _planner_node(_state: dict[str, Any], _config: dict[str, Any] | None = None) -> dict[str, list[AIMessage]]:
    return {}


def _echo_tool(text: str) -> dict[str, Any]:
    return {"ok": True, "text": text}


def _messages_count(graph, config: dict[str, Any]) -> int:
    snapshot = graph.get_state(config)
    values = getattr(snapshot, "values", {}) or {}
    msgs = values.get("messages", [])
    return len(msgs)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Phase 3 checkpoint persistence/recovery.")
    parser.add_argument(
        "--checkpoint-path",
        default=settings.agent_checkpoint_path,
        help="SQLite checkpoint file path (default from settings.agent_checkpoint_path).",
    )
    parser.add_argument(
        "--thread-id",
        default=f"phase3-step4-{uuid.uuid4().hex[:8]}",
        help="Thread id for checkpoint namespace.",
    )
    parser.add_argument(
        "--summary-json",
        default=str(ROOT / "artifacts/phase3/phase3-checkpoint-summary.json"),
        help="Path to write structured verification summary JSON.",
    )
    args = parser.parse_args()

    checkpoint_path = Path(args.checkpoint_path).resolve()
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)

    tool = StructuredTool.from_function(
        name="echo_tool",
        description="Echo text for checkpoint verification.",
        func=_echo_tool,
        args_schema=EchoArgs,
    )
    graph = build_agent_graph(
        _planner_node,
        _assistant_node,
        _safety_gate_node,
        [tool],
        checkpoint_path=str(checkpoint_path),
    )
    config = {"configurable": {"thread_id": args.thread_id}}

    # Turn 1: includes a tool call path.
    out1 = graph.invoke({"messages": [HumanMessage(content="please call tool")]}, config=config)
    c1 = _messages_count(graph, config)

    # Turn 2: regular continuation, should append over previous state.
    out2 = graph.invoke({"messages": [HumanMessage(content="second turn")]}, config=config)
    c2 = _messages_count(graph, config)

    ok_state_grew = c2 > c1 >= 3
    ok_checkpoint_file = checkpoint_path.exists()
    ok_tool_path = any(isinstance(m, ToolMessage) for m in out1.get("messages", []))

    summary = {
        "thread_id": args.thread_id,
        "checkpoint_path": str(checkpoint_path),
        "messages_after_turn1": c1,
        "messages_after_turn2": c2,
        "tool_path_executed": ok_tool_path,
        "checkpoint_file_exists": ok_checkpoint_file,
        "state_resumed": ok_state_grew,
        "phase3_step4": "PASS" if (ok_state_grew and ok_checkpoint_file and ok_tool_path) else "FAIL",
    }
    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"thread_id={summary['thread_id']}")
    print(f"checkpoint_path={summary['checkpoint_path']}")
    print(f"messages_after_turn1={summary['messages_after_turn1']}")
    print(f"messages_after_turn2={summary['messages_after_turn2']}")
    print(f"tool_path_executed={summary['tool_path_executed']}")
    print(f"checkpoint_file_exists={summary['checkpoint_file_exists']}")
    print(f"state_resumed={summary['state_resumed']}")
    print(f"summary_json={summary_path}")

    if summary["phase3_step4"] == "PASS":
        print("phase3_step4=PASS")
        return 0
    print("phase3_step4=FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
