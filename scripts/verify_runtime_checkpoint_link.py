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

from copilot_agent.runtime.checkpoint_store import CheckpointStore  # noqa: E402
from copilot_agent.runtime.event_store import EventStore  # noqa: E402
from copilot_agent.runtime.thread_checkpoint import archive_thread_and_purge_checkpoint  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402
from scripts._verify_helpers import build_verify_fixture, close_fixture, collect_runtime_events  # noqa: E402


class EchoState(TypedDict):
    value: str


async def verify(event_store_path: Path, checkpoint_path: Path, thread_prefix: str) -> dict[str, Any]:
    thread_id = f"{thread_prefix}-checkpoint"
    fixture = None

    try:
        graph = StateGraph(EchoState)

        async def echo_node(state: EchoState) -> EchoState:
            return {"value": state.get("value", "") + "-done"}

        graph.add_node("echo", echo_node)
        graph.set_entry_point("echo")
        graph.add_edge("echo", END)
        fixture = build_verify_fixture(
            event_store_path=event_store_path,
            checkpoint_path=checkpoint_path,
            thread_id=thread_id,
            graph=graph,
        )
        domain_events = await collect_runtime_events(
            fixture,
            graph_input={"value": "start"},
        )

        snapshot = await fixture.reader.snapshot(thread_id)
        completed_meta = next((event for event in domain_events if event["type"] == "run_completed_meta"), {})
        meta_payload = completed_meta.get("payload") or {}
        events = fixture.store.list_run_events(fixture.run_id)

        checkpoint_store = CheckpointStore(str(checkpoint_path))
        had_checkpoint = checkpoint_store.has_thread(thread_id)
        archived = archive_thread_and_purge_checkpoint(fixture.store, checkpoint_store, thread_id)
        purged = not checkpoint_store.has_thread(thread_id)

        return {
            "thread_id": thread_id,
            "run_id": fixture.run_id,
            "snapshot_message_count": snapshot.get("message_count"),
            "meta_message_count": meta_payload.get("message_count"),
            "meta_has_interrupt": meta_payload.get("has_interrupt"),
            "event_types": [event["type"] for event in events],
            "had_checkpoint_before_archive": had_checkpoint,
            "archived_status": (archived or {}).get("status"),
            "checkpoint_purged": purged,
        }
    finally:
        await close_fixture(fixture)


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
