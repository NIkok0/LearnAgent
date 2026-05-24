#!/usr/bin/env python
"""Verify memory + checkpoint unification: current-turn input and compaction."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph
from langgraph.types import interrupt
from typing_extensions import Annotated, TypedDict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.agent.graph import _build_checkpointer, close_graph_checkpointer  # noqa: E402
from copilot_agent.agent.message_utils import current_turn_messages  # noqa: E402
from copilot_agent.agent.stream.event_mapper import GraphEventMapper  # noqa: E402
from copilot_agent.memory.checkpoint_compactor import CheckpointCompactor  # noqa: E402
from copilot_agent.memory.policy import MemoryPolicyConfig  # noqa: E402
from copilot_agent.runtime.checkpoint_reader import CheckpointReader  # noqa: E402
from copilot_agent.runtime.event_store import RUN_STATUS_RUNNING  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402
from copilot_agent.tools.registry import ToolRegistry  # noqa: E402
from scripts._verify_helpers import build_verify_fixture, close_fixture, collect_runtime_events  # noqa: E402
from langgraph.graph.message import add_messages  # noqa: E402


class MessageState(TypedDict):
    messages: Annotated[list, add_messages]


async def verify(event_store_path: Path, checkpoint_path: Path, thread_prefix: str) -> dict[str, Any]:
    thread_id = f"{thread_prefix}-memory-checkpoint"
    fixture = None
    interrupt_compiled = None

    try:
        graph = StateGraph(MessageState)

        async def echo_node(state: MessageState) -> MessageState:
            last = state["messages"][-1]
            if isinstance(last, HumanMessage):
                return {"messages": [AIMessage(content=f"echo:{last.content}")]}
            return {}

        graph.add_node("echo", echo_node)
        graph.set_entry_point("echo")
        graph.add_edge("echo", END)
        fixture = build_verify_fixture(
            event_store_path=event_store_path,
            checkpoint_path=checkpoint_path,
            thread_id=thread_id,
            graph=graph,
            run_status=RUN_STATUS_RUNNING,
        )
        config = {"configurable": {"thread_id": thread_id}}

        full_history = [
            {"role": "user", "content": "turn-one"},
            {"role": "assistant", "content": "reply-one"},
            {"role": "user", "content": "turn-two"},
        ]
        turn_messages = current_turn_messages(full_history)

        async def _run_turn(user_text: str, active_run_id: str) -> list[dict[str, Any]]:
            return await collect_runtime_events(
                fixture,
                graph_input={"messages": [HumanMessage(content=user_text)]},
                graph_config=config,
                run_id=active_run_id,
            )

        domain_events = await _run_turn("turn-one", fixture.run_id)
        fixture.store.complete_run(fixture.run_id)
        run_two = fixture.store.create_run(thread_id)
        run_two_id = str(run_two["id"])
        fixture.store.update_run_status(run_two_id, RUN_STATUS_RUNNING)
        domain_events.extend(await _run_turn("turn-two", run_two_id))
        fixture.store.complete_run(run_two_id)

        reader = fixture.reader
        snapshot = await reader.snapshot(thread_id)
        completed_meta = next(
            (event for event in reversed(domain_events) if event["type"] == "run_completed_meta"),
            {},
        )
        meta_payload = completed_meta.get("payload") or {}

        compact_policy = MemoryPolicyConfig(
            checkpoint_compact_enabled=True,
            checkpoint_compact_message_threshold=3,
            checkpoint_compact_keep_recent_turns=1,
            checkpoint_compact_summary_max_chars=500,
        )
        compactor = CheckpointCompactor(fixture.graph, policy=compact_policy)
        compact_result = await compactor.compact_if_needed(thread_id)
        after_compact = await reader.snapshot(thread_id)

        interrupt_thread = f"{thread_prefix}-interrupt"
        interrupt_run = fixture.store.create_run(interrupt_thread)
        interrupt_run_id = str(interrupt_run["id"])
        interrupt_graph = StateGraph(MessageState)

        async def interrupt_node(_state: MessageState) -> MessageState:
            interrupt({"required": True, "reason": "dangerous_tool", "message": "approve?"})
            return {}

        interrupt_graph.add_node("gate", interrupt_node)
        interrupt_graph.set_entry_point("gate")
        interrupt_graph.add_edge("gate", END)
        interrupt_compiled = interrupt_graph.compile(
            checkpointer=_build_checkpointer(str(checkpoint_path), async_checkpoint=True)
        )
        interrupt_config = {"configurable": {"thread_id": interrupt_thread}}
        interrupt_mapper = GraphEventMapper(
            memory=fixture.memory,
            tool_registry=ToolRegistry(),
            checkpoint_reader=CheckpointReader(interrupt_compiled),
        )
        interrupt_events: list[dict[str, Any]] = []
        try:
            async for runtime_event in interrupt_mapper.map(
                graph=interrupt_compiled,
                graph_input={"messages": [HumanMessage(content="dangerous")]},
                graph_config=interrupt_config,
                thread_id=interrupt_thread,
                run_id=interrupt_run_id,
            ):
                interrupt_events.append(
                    {
                        "type": runtime_event.kind,
                        "payload": runtime_event.to_store_payload(),
                    }
                )
        except Exception:
            pass
        interrupt_compactor = CheckpointCompactor(interrupt_compiled, policy=compact_policy)
        interrupt_compact = await interrupt_compactor.compact_if_needed(interrupt_thread)

        context = fixture.memory.build_context(
            thread_id=thread_id,
            run_id=fixture.run_id,
            messages=turn_messages,
            goal="turn-two",
        )

        run_events = fixture.store.list_run_events(fixture.run_id)
        run_events_have_sequence = bool(run_events) and all(event.get("sequence") is not None for event in run_events)
        state_values = await reader.state_values(thread_id)
        checkpoint_messages = state_values.get("messages") if isinstance(state_values.get("messages"), list) else []
        system_contents = [
            str(getattr(message, "content", "") or "").strip()
            for message in checkpoint_messages
            if isinstance(message, SystemMessage)
        ]
        multi_turn_no_duplicate_system = len(system_contents) == len(set(system_contents))

        return {
            "thread_id": thread_id,
            "run_id": fixture.run_id,
            "turn_messages_count": len(turn_messages),
            "turn_messages_content": [message.get("content") for message in turn_messages],
            "snapshot_message_count": snapshot.get("message_count"),
            "meta_message_count": meta_payload.get("message_count"),
            "compact_result": compact_result,
            "after_compact_message_count": after_compact.get("message_count"),
            "context_has_current_turn": "current_turn_messages" in context.working,
            "context_messages_compat": context.working.get("messages") == turn_messages,
            "interrupt_compact_reason": interrupt_compact.get("reason"),
            "interrupt_events": [event.get("type") for event in interrupt_events],
            "run_events_have_sequence": run_events_have_sequence,
            "multi_turn_no_duplicate_system": multi_turn_no_duplicate_system,
        }
    finally:
        await close_graph_checkpointer(interrupt_compiled)
        await close_fixture(fixture)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify memory + checkpoint consistency.")
    parser.add_argument("--event-store-path", default=settings.agent_event_store_path)
    parser.add_argument("--checkpoint-path", default=settings.agent_checkpoint_path)
    parser.add_argument("--thread-prefix", default=f"memory-checkpoint-{uuid.uuid4().hex[:8]}")
    parser.add_argument(
        "--summary-json",
        default=str(ROOT / "artifacts/runtime/memory-checkpoint-consistency-summary.json"),
    )
    args = parser.parse_args()

    event_store_path = Path(args.event_store_path).with_name(
        f"{Path(args.event_store_path).stem}-memory-checkpoint.sqlite"
    )
    checkpoint_path = Path(args.checkpoint_path).with_name(
        f"{Path(args.checkpoint_path).stem}-memory-checkpoint.sqlite"
    )
    event_store_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    if checkpoint_path.exists():
        try:
            checkpoint_path.unlink()
        except OSError:
            checkpoint_path = checkpoint_path.with_name(
                f"{checkpoint_path.stem}-{uuid.uuid4().hex[:8]}{checkpoint_path.suffix}"
            )

    summary = asyncio.run(verify(event_store_path, checkpoint_path, args.thread_prefix))
    checks = {
        "current_turn_only_last_user": summary["turn_messages_count"] == 1
        and summary["turn_messages_content"] == ["turn-two"],
        "message_count_matches": summary["snapshot_message_count"] == summary["meta_message_count"],
        "compaction_reduces_messages": summary["compact_result"].get("compacted") is True
        and summary["after_compact_message_count"] < summary["snapshot_message_count"],
        "working_context_current_turn": summary["context_has_current_turn"]
        and summary["context_messages_compat"],
        "interrupt_not_compacted": summary["interrupt_compact_reason"] == "has_interrupt",
        "run_events_have_sequence": bool(summary.get("run_events_have_sequence")),
        "multi_turn_no_duplicate_system": bool(summary.get("multi_turn_no_duplicate_system")),
    }
    summary["checks"] = checks
    summary["memory_checkpoint_consistency"] = "PASS" if all(checks.values()) else "FAIL"

    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    for key, value in checks.items():
        print(f"{key}={value}")
    print(f"summary_json={summary_path}")
    print(f"memory_checkpoint_consistency={summary['memory_checkpoint_consistency']}")
    return 0 if summary["memory_checkpoint_consistency"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
