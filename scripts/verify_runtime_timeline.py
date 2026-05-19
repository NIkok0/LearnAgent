#!/usr/bin/env python
"""Verify CQRS timeline projection from persisted runtime events."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.runtime.event_store import (  # noqa: E402
    EventStore,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_FAILED,
)
from copilot_agent.runtime.timeline import TimelineProjector  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402


def _append(store: EventStore, thread_id: str, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
    store.append_event(thread_id, run_id, event_type, payload)


def _make_run(store: EventStore, thread_id: str) -> dict[str, Any]:
    store.ensure_thread(thread_id, title="timeline verification")
    return store.create_run(thread_id)


def verify(event_store_path: Path, thread_prefix: str) -> dict[str, Any]:
    store = EventStore(str(event_store_path))
    projector = TimelineProjector()

    completed_thread = f"{thread_prefix}-completed"
    completed_run = _make_run(store, completed_thread)
    completed_run_id = str(completed_run["id"])
    _append(store, completed_thread, completed_run_id, "run_created", {"status": "queued"})
    _append(store, completed_thread, completed_run_id, "run_started", {"status": "running"})
    _append(store, completed_thread, completed_run_id, "token", {"text": "hello "})
    _append(store, completed_thread, completed_run_id, "token", {"text": "world"})
    _append(
        store,
        completed_thread,
        completed_run_id,
        "tool_start",
        {
            "name": "search_docs",
            "call_id": "tool-1",
            "category": "memory",
            "risk_level": "low",
            "requires_approval": False,
            "arguments": {"query": "Redis"},
        },
    )
    _append(
        store,
        completed_thread,
        completed_run_id,
        "tool_end",
        {
            "name": "search_docs",
            "call_id": "tool-1",
            "result": {"ok": True},
            "duration_ms": 2,
            "success": True,
        },
    )
    _append(store, completed_thread, completed_run_id, "done", {})
    completed = store.complete_run(completed_run_id)
    completed_timeline = projector.project_run(completed, store.list_run_events(completed_run_id))

    missing_tool_thread = f"{thread_prefix}-missing-tool"
    missing_tool_run = _make_run(store, missing_tool_thread)
    missing_tool_run_id = str(missing_tool_run["id"])
    _append(store, missing_tool_thread, missing_tool_run_id, "run_created", {})
    _append(store, missing_tool_thread, missing_tool_run_id, "run_started", {})
    _append(
        store,
        missing_tool_thread,
        missing_tool_run_id,
        "tool_start",
        {"name": "http_get", "call_id": "tool-missing", "category": "http", "risk_level": "medium"},
    )
    _append(store, missing_tool_thread, missing_tool_run_id, "done", {})
    missing_tool_completed = store.complete_run(missing_tool_run_id)
    missing_tool_timeline = projector.project_run(missing_tool_completed, store.list_run_events(missing_tool_run_id))

    approval_thread = f"{thread_prefix}-approval"
    approval_run = _make_run(store, approval_thread)
    approval_run_id = str(approval_run["id"])
    _append(store, approval_thread, approval_run_id, "run_created", {})
    _append(store, approval_thread, approval_run_id, "run_started", {})
    _append(store, approval_thread, approval_run_id, "approval_required", {"reason": "dangerous_tool"})
    _append(store, approval_thread, approval_run_id, "approval_resolved", {"approved": True})
    _append(store, approval_thread, approval_run_id, "done", {})
    approval_completed = store.complete_run(approval_run_id)
    approval_timeline = projector.project_run(approval_completed, store.list_run_events(approval_run_id))

    memory_thread = f"{thread_prefix}-memory"
    memory_run = _make_run(store, memory_thread)
    memory_run_id = str(memory_run["id"])
    _append(store, memory_thread, memory_run_id, "run_created", {})
    _append(
        store,
        memory_thread,
        memory_run_id,
        "memory_run_summary",
        {
            "summary_type": "run",
            "goal": "verify timeline",
            "outcome": "completed",
            "source_event_ids": [1, 2],
        },
    )
    _append(store, memory_thread, memory_run_id, "done", {})
    memory_completed = store.complete_run(memory_run_id)
    memory_timeline = projector.project_run(memory_completed, store.list_run_events(memory_run_id))

    cancelled_thread = f"{thread_prefix}-cancelled"
    cancelled_run = _make_run(store, cancelled_thread)
    cancelled_run_id = str(cancelled_run["id"])
    _append(store, cancelled_thread, cancelled_run_id, "run_created", {})
    _append(store, cancelled_thread, cancelled_run_id, "run_started", {})
    _append(store, cancelled_thread, cancelled_run_id, "cancel_requested", {})
    _append(store, cancelled_thread, cancelled_run_id, "cancelled", {})
    cancelled = store.update_run_status(cancelled_run_id, RUN_STATUS_CANCELLED, completed=True)
    cancelled_timeline = projector.project_run(cancelled, store.list_run_events(cancelled_run_id))

    failed_thread = f"{thread_prefix}-failed"
    failed_run = _make_run(store, failed_thread)
    failed_run_id = str(failed_run["id"])
    _append(store, failed_thread, failed_run_id, "run_created", {})
    failed = store.update_run_status(failed_run_id, RUN_STATUS_FAILED, error="boom", completed=True)
    failed_timeline = projector.project_run(failed, store.list_run_events(failed_run_id))

    completed_kinds = [item["kind"] for item in completed_timeline["items"]]
    completed_tool = next((item for item in completed_timeline["items"] if item["kind"] == "tool_call"), {})
    approval_item = next((item for item in approval_timeline["items"] if item["kind"] == "approval"), {})
    memory_item = next((item for item in memory_timeline["items"] if item["kind"] == "memory"), {})

    return {
        "event_store_path": str(event_store_path),
        "completed": {
            "run_id": completed_run_id,
            "assistant_output": completed_timeline["assistant_output"],
            "kinds": completed_kinds,
            "tool_merged": completed_tool.get("start_event_id") is not None and completed_tool.get("end_event_id") is not None,
            "tool_success": completed_tool.get("success"),
            "warnings": [warning["code"] for warning in completed_timeline["warnings"]],
        },
        "missing_tool": {
            "run_id": missing_tool_run_id,
            "warnings": [warning["code"] for warning in missing_tool_timeline["warnings"]],
        },
        "approval": {
            "run_id": approval_run_id,
            "status": approval_item.get("status"),
            "resolved": approval_item.get("resolved"),
        },
        "memory": {
            "run_id": memory_run_id,
            "derived": memory_item.get("derived"),
            "summary_type": (memory_item.get("payload") or {}).get("summary_type"),
        },
        "cancelled": {
            "run_id": cancelled_run_id,
            "status": cancelled_timeline["status"],
            "warnings": [warning["code"] for warning in cancelled_timeline["warnings"]],
        },
        "failed_without_error": {
            "run_id": failed_run_id,
            "warnings": [warning["code"] for warning in failed_timeline["warnings"]],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify LearnAgent runtime timeline projection.")
    parser.add_argument(
        "--event-store-path",
        default=settings.agent_event_store_path,
        help="SQLite event store path (default from settings.agent_event_store_path).",
    )
    parser.add_argument(
        "--thread-prefix",
        default=f"timeline-{uuid.uuid4().hex[:8]}",
        help="Thread id prefix for verification.",
    )
    parser.add_argument(
        "--summary-json",
        default=str(ROOT / "artifacts/runtime/timeline-summary.json"),
        help="Path to write structured verification summary JSON.",
    )
    args = parser.parse_args()

    event_store_path = Path(args.event_store_path).resolve()
    event_store_path.parent.mkdir(parents=True, exist_ok=True)
    summary = verify(event_store_path, args.thread_prefix)

    ok_completed = (
        summary["completed"]["assistant_output"] == "hello world"
        and "assistant_output" in summary["completed"]["kinds"]
        and summary["completed"]["tool_merged"]
        and summary["completed"]["tool_success"] is True
        and not summary["completed"]["warnings"]
    )
    ok_missing_tool = "tool_missing_end" in summary["missing_tool"]["warnings"]
    ok_approval = summary["approval"]["status"] == "approved" and summary["approval"]["resolved"].get("approved") is True
    ok_memory = summary["memory"]["derived"] is True and summary["memory"]["summary_type"] == "run"
    ok_cancelled = summary["cancelled"]["status"] == "cancelled" and not summary["cancelled"]["warnings"]
    ok_failed_warning = "failed_without_error_event" in summary["failed_without_error"]["warnings"]
    passed = ok_completed and ok_missing_tool and ok_approval and ok_memory and ok_cancelled and ok_failed_warning
    summary["runtime_timeline"] = "PASS" if passed else "FAIL"

    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"event_store_path={summary['event_store_path']}")
    print(f"completed_assistant_output={summary['completed']['assistant_output']}")
    print(f"completed_tool_merged={summary['completed']['tool_merged']}")
    print(f"missing_tool_warnings={','.join(summary['missing_tool']['warnings'])}")
    print(f"approval_status={summary['approval']['status']}")
    print(f"memory_summary_type={summary['memory']['summary_type']}")
    print(f"cancelled_status={summary['cancelled']['status']}")
    print(f"failed_warnings={','.join(summary['failed_without_error']['warnings'])}")
    print(f"summary_json={summary_path}")
    print(f"runtime_timeline={summary['runtime_timeline']}")

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
