#!/usr/bin/env python
"""Verify checkpoint consistency v2 events, timeline projection, and debug bundle."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import Annotated, TypedDict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.runtime.event_schema import (  # noqa: E402
    EVENT_CHECKPOINT_CONSISTENCY_CHECKED,
    EVENT_RUN_COMPLETED_META,
    EVENT_RUN_CONSISTENCY_CHECKED,
)
from copilot_agent.runtime.event_store import EventStore, RUN_STATUS_RUNNING  # noqa: E402
from copilot_agent.runtime.execution_engine import ExecutionEngine, ManagedRun  # noqa: E402
from copilot_agent.runtime.timeline import TimelineProjector  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402
from scripts._verify_helpers import build_verify_fixture, close_fixture, collect_runtime_events  # noqa: E402
from scripts.export_run_debug_bundle import build_debug_bundle  # noqa: E402


class MessageState(TypedDict):
    messages: Annotated[list, add_messages]


class GraphRunner:
    def __init__(self, store: EventStore, graph: Any) -> None:
        self._store = store
        self.graph = graph

    async def run_stream(
        self,
        *,
        conversation_id: str,
        run_id: str | None = None,
        messages: list[dict[str, Any]],
        confirm_dangerous: bool,
        resume: bool | None = None,
    ) -> AsyncIterator[str]:
        del conversation_id, run_id, messages, confirm_dangerous, resume
        if False:
            yield ""


async def verify(event_store_path: Path, checkpoint_path: Path, thread_prefix: str) -> dict[str, Any]:
    thread_id = f"{thread_prefix}-match"
    fixture = None
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
        await collect_runtime_events(
            fixture,
            graph_input={"messages": [HumanMessage(content="checkpoint v2")]},
        )
        fixture.store.complete_run(fixture.run_id)
        engine = ExecutionEngine(
            event_store=fixture.store,
            runner=GraphRunner(fixture.store, fixture.graph),  # type: ignore[arg-type]
        )
        managed = ManagedRun(
            run_id=fixture.run_id,
            thread_id=thread_id,
            messages=[{"role": "user", "content": "checkpoint v2"}],
        )
        checkpoint_payload = await engine._append_checkpoint_consistency_checked(managed)
        engine._append_consistency_checked(managed, checkpoint_consistency=checkpoint_payload)

        events = fixture.store.list_run_events(fixture.run_id)
        run = fixture.store.get_run(fixture.run_id) or {}
        timeline = TimelineProjector().project_run(run, events)
        bundle_path = ROOT / "artifacts/runtime/debug-bundles" / f"verify-{fixture.run_id}.json"
        bundle = build_debug_bundle(
            event_store_path=event_store_path,
            checkpoint_path=checkpoint_path,
            run_id=fixture.run_id,
        )
        bundle_path.parent.mkdir(parents=True, exist_ok=True)
        bundle_path.write_text(json.dumps(bundle, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

        missing_thread_id = f"{thread_prefix}-missing"
        missing_store = EventStore(str(event_store_path))
        missing_run = missing_store.create_run(missing_thread_id)
        missing_run_id = str(missing_run["id"])
        missing_store.update_run_status(missing_run_id, RUN_STATUS_RUNNING)
        missing_store.append_event(
            missing_thread_id,
            missing_run_id,
            EVENT_RUN_COMPLETED_META,
            {"checkpoint_thread_id": missing_thread_id, "message_count": 2, "has_interrupt": False},
        )
        missing_store.append_event(missing_thread_id, missing_run_id, "done", {})
        missing_store.complete_run(missing_run_id)
        missing_managed = ManagedRun(
            run_id=missing_run_id,
            thread_id=missing_thread_id,
            messages=[{"role": "user", "content": "missing checkpoint"}],
        )
        missing_payload = await engine._append_checkpoint_consistency_checked(missing_managed)
        engine._append_consistency_checked(missing_managed, checkpoint_consistency=missing_payload)
        missing_events = missing_store.list_run_events(missing_run_id)
        missing_timeline = TimelineProjector().project_run(
            missing_store.get_run(missing_run_id) or {},
            missing_events,
        )

        checkpoint_event = next(
            (event for event in events if event.get("type") == EVENT_CHECKPOINT_CONSISTENCY_CHECKED),
            {},
        )
        run_consistency_event = next(
            (event for event in events if event.get("type") == EVENT_RUN_CONSISTENCY_CHECKED),
            {},
        )
        return {
            "thread_id": thread_id,
            "run_id": fixture.run_id,
            "event_types": [event.get("type") for event in events],
            "checkpoint_consistency": checkpoint_event.get("payload") or {},
            "run_consistency": run_consistency_event.get("payload") or {},
            "timeline_checkpoint": timeline.get("checkpoint"),
            "timeline_warnings": [warning.get("code") for warning in timeline.get("warnings", [])],
            "debug_bundle_path": str(bundle_path),
            "debug_bundle_keys": sorted(bundle.keys()),
            "debug_bundle_has_checkpoint_raw": isinstance(bundle.get("checkpoint_raw"), dict),
            "missing": {
                "run_id": missing_run_id,
                "status": (missing_store.get_run(missing_run_id) or {}).get("status"),
                "checkpoint_consistency": missing_payload,
                "timeline_warnings": [
                    warning.get("code") for warning in missing_timeline.get("warnings", [])
                ],
            },
        }
    finally:
        await close_fixture(fixture)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify checkpoint consistency v2.")
    parser.add_argument("--event-store-path", default=settings.agent_event_store_path)
    parser.add_argument("--checkpoint-path", default=settings.agent_checkpoint_path)
    parser.add_argument("--thread-prefix", default=f"checkpoint-v2-{uuid.uuid4().hex[:8]}")
    parser.add_argument(
        "--summary-json",
        default=str(ROOT / "artifacts/runtime/checkpoint-consistency-v2-summary.json"),
    )
    args = parser.parse_args()

    event_store_path = Path(args.event_store_path).with_name(
        f"{Path(args.event_store_path).stem}-checkpoint-v2.sqlite"
    )
    checkpoint_path = Path(args.checkpoint_path).with_name(
        f"{Path(args.checkpoint_path).stem}-checkpoint-v2.sqlite"
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
    checkpoint = summary.get("checkpoint_consistency") if isinstance(summary.get("checkpoint_consistency"), dict) else {}
    run_consistency = summary.get("run_consistency") if isinstance(summary.get("run_consistency"), dict) else {}
    timeline_checkpoint = summary.get("timeline_checkpoint") if isinstance(summary.get("timeline_checkpoint"), dict) else {}
    missing = summary.get("missing") if isinstance(summary.get("missing"), dict) else {}
    missing_checkpoint = (
        missing.get("checkpoint_consistency")
        if isinstance(missing.get("checkpoint_consistency"), dict)
        else {}
    )
    checks = {
        "checkpoint_event_written": EVENT_CHECKPOINT_CONSISTENCY_CHECKED in summary.get("event_types", []),
        "message_count_matches": checkpoint.get("checkpoint_match") is True,
        "run_consistency_has_checkpoint_summary": run_consistency.get("checkpoint_match") is True
        and run_consistency.get("checkpoint_warning_count") == 0,
        "timeline_consistency_v2": isinstance(timeline_checkpoint.get("consistency_v2"), dict)
        and timeline_checkpoint.get("consistency_v2", {}).get("checkpoint_match") is True,
        "missing_checkpoint_warns_only": missing.get("status") == "completed"
        and missing_checkpoint.get("checkpoint_missing") is True
        and missing_checkpoint.get("checkpoint_match") is False,
        "missing_timeline_warning": "checkpoint_missing" in missing.get("timeline_warnings", []),
        "debug_bundle_exported": Path(str(summary.get("debug_bundle_path"))).is_file()
        and summary.get("debug_bundle_has_checkpoint_raw") is True,
    }
    passed = all(checks.values())
    summary["checks"] = checks
    summary["checkpoint_consistency_v2"] = "PASS" if passed else "FAIL"

    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    for key, value in checks.items():
        print(f"{key}={value}")
    print(f"summary_json={summary_path}")
    print(f"checkpoint_consistency_v2={summary['checkpoint_consistency_v2']}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
