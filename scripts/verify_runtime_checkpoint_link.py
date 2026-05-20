#!/usr/bin/env python
"""Verify run_completed_meta aligns with LangGraph checkpoint snapshots."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from langgraph.graph import END, StateGraph
from typing_extensions import TypedDict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.agent.graph import _build_checkpointer  # noqa: E402
from copilot_agent.agent.stream.event_mapper import GraphEventMapper  # noqa: E402
from copilot_agent.memory import MemoryManager  # noqa: E402
from copilot_agent.rag.retriever import RagStore  # noqa: E402
from copilot_agent.runtime.checkpoint_reader import CheckpointReader  # noqa: E402
from copilot_agent.runtime.checkpoint_store import CheckpointStore  # noqa: E402
from copilot_agent.runtime.event_store import EventStore  # noqa: E402
from copilot_agent.runtime.thread_checkpoint import archive_thread_and_purge_checkpoint  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402
from copilot_agent.tools.registry import ToolRegistry  # noqa: E402


class EchoState(TypedDict):
    value: str


async def verify(event_store_path: Path, checkpoint_path: Path, thread_prefix: str) -> dict[str, Any]:
    store = EventStore(str(event_store_path))
    thread_id = f"{thread_prefix}-checkpoint"
    run = store.create_run(thread_id)
    run_id = str(run["id"])

    graph = StateGraph(EchoState)

    async def echo_node(state: EchoState) -> EchoState:
        return {"value": state.get("value", "") + "-done"}

    graph.add_node("echo", echo_node)
    graph.set_entry_point("echo")
    graph.add_edge("echo", END)
    compiled = graph.compile(checkpointer=_build_checkpointer(str(checkpoint_path), async_checkpoint=True))

    memory = MemoryManager(
        rag_store=RagStore([]),
        event_store=store,
        checkpoint_path=str(checkpoint_path),
    )
    mapper = GraphEventMapper(
        memory=memory,
        tool_registry=ToolRegistry(),
        checkpoint_reader=CheckpointReader(compiled),
    )
    config = {"configurable": {"thread_id": thread_id}}
    domain_events: list[dict[str, Any]] = []
    async for domain_event in mapper.map(
        graph=compiled,
        graph_input={"value": "start"},
        graph_config=config,
        thread_id=thread_id,
        run_id=run_id,
    ):
        memory.append_event(thread_id, run_id, domain_event["type"], domain_event["payload"])
        domain_events.append(domain_event)

    reader = CheckpointReader(compiled)
    snapshot = await reader.snapshot(thread_id)
    completed_meta = next((event for event in domain_events if event["type"] == "run_completed_meta"), {})
    meta_payload = completed_meta.get("payload") or {}
    events = store.list_run_events(run_id)

    checkpoint_store = CheckpointStore(str(checkpoint_path))
    had_checkpoint = checkpoint_store.has_thread(thread_id)
    archived = archive_thread_and_purge_checkpoint(store, checkpoint_store, thread_id)
    purged = not checkpoint_store.has_thread(thread_id)

    return {
        "thread_id": thread_id,
        "run_id": run_id,
        "snapshot_message_count": snapshot.get("message_count"),
        "meta_message_count": meta_payload.get("message_count"),
        "meta_has_interrupt": meta_payload.get("has_interrupt"),
        "event_types": [event["type"] for event in events],
        "had_checkpoint_before_archive": had_checkpoint,
        "archived_status": (archived or {}).get("status"),
        "checkpoint_purged": purged,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify checkpoint metadata and purge behavior.")
    parser.add_argument("--event-store-path", default=settings.agent_event_store_path)
    parser.add_argument("--checkpoint-path", default=settings.agent_checkpoint_path)
    parser.add_argument("--thread-prefix", default=f"checkpoint-link-{uuid.uuid4().hex[:8]}")
    parser.add_argument("--summary-json", default=str(ROOT / "artifacts/runtime/checkpoint-link-summary.json"))
    args = parser.parse_args()

    event_store_path = Path(args.event_store_path).with_name(
        f"{Path(args.event_store_path).stem}-checkpoint-link.sqlite"
    )
    checkpoint_path = Path(args.checkpoint_path).with_name(
        f"{Path(args.checkpoint_path).stem}-checkpoint-link.sqlite"
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
    run_events = EventStore(str(event_store_path)).list_run_events(summary["run_id"])
    checks = {
        "meta_written": "run_completed_meta" in summary["event_types"],
        "message_count_matches": summary["snapshot_message_count"] == summary["meta_message_count"],
        "meta_schema_version": all(
            event.get("payload", {}).get("schema_version") == 1
            for event in run_events
            if event.get("type") == "run_completed_meta"
        ),
        "had_checkpoint_before_archive": summary["had_checkpoint_before_archive"],
        "archive_purged_checkpoint": summary["archived_status"] == "archived" and summary["checkpoint_purged"],
    }
    summary["checks"] = checks
    summary["runtime_checkpoint_link"] = "PASS" if all(checks.values()) else "FAIL"

    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    for key, value in checks.items():
        print(f"{key}={value}")
    print(f"summary_json={summary_path}")
    print(f"runtime_checkpoint_link={summary['runtime_checkpoint_link']}")
    return 0 if summary["runtime_checkpoint_link"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
