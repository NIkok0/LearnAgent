#!/usr/bin/env python
"""Phase 3 safety_gate regression: dangerous http_post must be blocked.

This script validates that:
1) Graph-level safety_gate intercepts dangerous POST tool calls.
2) The blocked tool call does not reach the tools node.

It uses deterministic mock assistant/tool nodes and does NOT require OPENAI_API_KEY.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any, Optional

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.agent.graph import build_agent_graph  # noqa: E402
from copilot_agent.scenario import load_scenario  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402


class HttpPostArgs(BaseModel):
    path: str = Field(description="Target API path")
    json_body: dict[str, Any] = Field(default_factory=dict)
    cookie_header: Optional[str] = None
    idempotency_key: Optional[str] = None


def _watermark_dangerous_path() -> str:
    scenario = load_scenario("watermark")
    if scenario.policy.dangerous_paths:
        return str(scenario.policy.dangerous_paths[0])
    if scenario.router_rules and scenario.router_rules.dangerous_job_path:
        return scenario.router_rules.dangerous_job_path
    raise RuntimeError("watermark scenario must declare a dangerous path")


def _assistant_node(state: dict[str, Any]) -> dict[str, list[AIMessage]]:
    msgs = state.get("messages", [])
    if not msgs:
        return {"messages": [AIMessage(content="assistant_ready")]}
    last = msgs[-1]
    if isinstance(last, HumanMessage):
        return {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_post_1",
                            "name": "http_post",
                            "args": {
                                "path": _watermark_dangerous_path(),
                                "json_body": {"image_url": "https://example.invalid/test.png"},
                            },
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        }
    return {"messages": [AIMessage(content="assistant_continue")]}


def _safety_gate_node(state: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, list[AIMessage]]:
    msgs = state.get("messages", [])
    if not msgs:
        return {}
    last = msgs[-1]
    if not isinstance(last, AIMessage) or not last.tool_calls:
        return {}

    ctx = (config.get("configurable") or {}) if config else {}
    allow_job_post = bool(ctx.get("allow_job_post", settings.copilot_allow_job_post))
    confirm_dangerous = bool(ctx.get("confirm_dangerous", False))
    for call in last.tool_calls:
        name = str(call.get("name", ""))
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        path = str(args.get("path", ""))
        dangerous_path = _watermark_dangerous_path()
        if name == "http_post" and path.split("?", 1)[0] == dangerous_path:
            if not allow_job_post:
                return {
                    "messages": [
                        AIMessage(
                            content=(
                                f"POST {dangerous_path} is disabled by deployment. "
                                "Enable COPILOT_ALLOW_JOB_POST=true, then retry with explicit confirmation."
                            )
                        )
                    ]
                }
            if not confirm_dangerous:
                return {
                    "messages": [
                        AIMessage(
                            content=(
                                "This action is gated. Re-send chat request with confirm_dangerous=true "
                                "if you want to enqueue a watermark job."
                            )
                        )
                    ]
                }
    return {}


def _planner_node(_state: dict[str, Any], _config: dict[str, Any] | None = None) -> dict[str, list[AIMessage]]:
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Phase 3 safety_gate blocks dangerous http_post.")
    parser.add_argument(
        "--checkpoint-path",
        default=settings.agent_checkpoint_path,
        help="SQLite checkpoint file path (default from settings.agent_checkpoint_path).",
    )
    parser.add_argument(
        "--thread-id",
        default=f"phase3-safety-gate-{uuid.uuid4().hex[:8]}",
        help="Thread id for this verification run.",
    )
    parser.add_argument(
        "--summary-json",
        default=str(ROOT / "artifacts/phase3/phase3-safety-gate-summary.json"),
        help="Path to write structured verification summary JSON.",
    )
    args = parser.parse_args()

    previous_allow_job_post = settings.copilot_allow_job_post
    settings.copilot_allow_job_post = True

    checkpoint_path = Path(args.checkpoint_path).resolve()
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)

    calls = {"http_post": 0}

    def _http_post_tool(
        path: str,
        json_body: dict[str, Any],
        cookie_header: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ):
        del json_body, cookie_header, idempotency_key
        calls["http_post"] += 1
        return {"ok": True, "path": path}

    tool = StructuredTool.from_function(
        name="http_post",
        description="Mock HTTP POST tool for safety gate regression.",
        func=_http_post_tool,
        args_schema=HttpPostArgs,
    )

    graph = build_agent_graph(
        _planner_node,
        _assistant_node,
        _safety_gate_node,
        [tool],
        checkpoint_path=str(checkpoint_path),
    )
    config = {
        "configurable": {
            "thread_id": args.thread_id,
            "allow_job_post": True,
            "confirm_dangerous": False,
        }
    }
    out = graph.invoke({"messages": [HumanMessage(content="enqueue job now")]}, config=config)
    msgs = out.get("messages", [])

    has_tool_message = any(isinstance(m, ToolMessage) for m in msgs)
    blocked_msg = msgs[-1] if msgs else None
    blocked_text = str(blocked_msg.content or "") if isinstance(blocked_msg, AIMessage) else ""
    blocked_by_gate = "gated" in blocked_text.lower()
    tool_not_called = calls["http_post"] == 0

    passed = blocked_by_gate and tool_not_called and (not has_tool_message)
    summary = {
        "thread_id": args.thread_id,
        "dangerous_path": _watermark_dangerous_path(),
        "blocked_by_gate": blocked_by_gate,
        "tool_not_called": tool_not_called,
        "tool_message_seen": has_tool_message,
        "http_post_calls": calls["http_post"],
        "block_message": blocked_text,
        "phase3_safety_gate": "PASS" if passed else "FAIL",
    }
    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"thread_id={summary['thread_id']}")
    print(f"dangerous_path={summary['dangerous_path']}")
    print(f"blocked_by_gate={summary['blocked_by_gate']}")
    print(f"tool_not_called={summary['tool_not_called']}")
    print(f"tool_message_seen={summary['tool_message_seen']}")
    print(f"http_post_calls={summary['http_post_calls']}")
    print(f"summary_json={summary_path}")

    if passed:
        print("phase3_safety_gate=PASS")
        settings.copilot_allow_job_post = previous_allow_job_post
        return 0
    print("phase3_safety_gate=FAIL")
    settings.copilot_allow_job_post = previous_allow_job_post
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
