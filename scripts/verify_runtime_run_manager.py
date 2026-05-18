#!/usr/bin/env python
"""Verify background run lifecycle, cancellation, and approval workflow."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.runtime.event_store import EventStore  # noqa: E402
from copilot_agent.runtime.run_manager import RunManager  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402


class FakeRunner:
    def __init__(self, event_store: EventStore) -> None:
        self._events = event_store

    async def run_stream(
        self,
        *,
        conversation_id: str,
        run_id: str | None = None,
        messages: list[dict[str, Any]],
        confirm_dangerous: bool,
    ) -> AsyncIterator[str]:
        thread_id = conversation_id
        scenario = str(messages[-1].get("content", "")) if messages else ""
        if "slow" in scenario:
            await asyncio.sleep(5)
            yield self._emit(thread_id, str(run_id), "done", {})
            return
        if "approval" in scenario and not confirm_dangerous:
            yield self._emit(thread_id, str(run_id), "approval_required", {"required": True, "reason": "dangerous_tool"})
            return
        yield self._emit(thread_id, str(run_id), "token", {"text": f"ok:{scenario}"})
        yield self._emit(thread_id, str(run_id), "tool_start", {"name": "search_docs", "call_id": run_id or "", "arguments": {"query": "Redis"}})
        yield self._emit(thread_id, str(run_id), "tool_end", {"name": "search_docs", "call_id": run_id or "", "result": {"ok": True}, "duration_ms": 1, "success": True})
        yield self._emit(thread_id, str(run_id), "done", {})

    def _emit(self, thread_id: str, run_id: str, event_type: str, payload: dict[str, Any]) -> str:
        self._events.append_event(thread_id, run_id, event_type, payload)
        return _sse(event_type, payload)


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _wait_for_status(store: EventStore, run_id: str, statuses: set[str], *, timeout: float = 3.0) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        run = store.get_run(run_id) or {}
        if str(run.get("status", "")) in statuses:
            return run
        await asyncio.sleep(0.05)
    return store.get_run(run_id) or {}


async def verify(event_store_path: Path, thread_prefix: str) -> dict[str, Any]:
    store = EventStore(str(event_store_path))
    manager = RunManager(event_store=store, runner=FakeRunner(store))  # type: ignore[arg-type]

    completed_thread = f"{thread_prefix}-completed"
    completed = await manager.create_run(
        thread_id=completed_thread,
        messages=[{"role": "user", "content": "normal"}],
    )
    completed_run = await _wait_for_status(store, completed.run_id, {"completed"})
    completed_events = store.list_run_events(completed.run_id)
    completed_events_before_cancel = len(completed_events)
    try:
        completed_cancel = await manager.cancel(completed.run_id)
    except KeyError:
        completed_cancel = store.get_run(completed.run_id) or {}
    completed_events_after_cancel = len(store.list_run_events(completed.run_id))

    cancelled_thread = f"{thread_prefix}-cancelled"
    cancelled = await manager.create_run(
        thread_id=cancelled_thread,
        messages=[{"role": "user", "content": "slow"}],
    )
    await _wait_for_status(store, cancelled.run_id, {"running"})
    await manager.cancel(cancelled.run_id)
    cancelled_run = await _wait_for_status(store, cancelled.run_id, {"cancelled"})
    cancelled_events = store.list_run_events(cancelled.run_id)

    approved_thread = f"{thread_prefix}-approved"
    approved = await manager.create_run(
        thread_id=approved_thread,
        messages=[{"role": "user", "content": "approval"}],
    )
    waiting_run = await _wait_for_status(store, approved.run_id, {"waiting_approval"})
    await manager.approve(approved.run_id)
    approved_run = await _wait_for_status(store, approved.run_id, {"completed"})
    approved_events = store.list_run_events(approved.run_id)

    rejected_thread = f"{thread_prefix}-rejected"
    rejected = await manager.create_run(
        thread_id=rejected_thread,
        messages=[{"role": "user", "content": "approval"}],
    )
    await _wait_for_status(store, rejected.run_id, {"waiting_approval"})
    await manager.reject(rejected.run_id)
    rejected_run = await _wait_for_status(store, rejected.run_id, {"completed"})
    rejected_events = store.list_run_events(rejected.run_id)

    return {
        "event_store_path": str(event_store_path),
        "completed": {
            "run_id": completed.run_id,
            "status": completed_run.get("status"),
            "cancel_after_terminal_status": completed_cancel.get("status"),
            "cancel_after_terminal_added_events": completed_events_after_cancel - completed_events_before_cancel,
            "event_types": [e["type"] for e in completed_events],
        },
        "cancelled": {
            "run_id": cancelled.run_id,
            "status": cancelled_run.get("status"),
            "event_types": [e["type"] for e in cancelled_events],
        },
        "approved": {
            "run_id": approved.run_id,
            "waiting_status_seen": waiting_run.get("status") == "waiting_approval",
            "status": approved_run.get("status"),
            "event_types": [e["type"] for e in approved_events],
        },
        "rejected": {
            "run_id": rejected.run_id,
            "status": rejected_run.get("status"),
            "event_types": [e["type"] for e in rejected_events],
            "tool_executed": any(e["type"] == "tool_start" for e in rejected_events),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify LearnAgent RunManager runtime behavior.")
    parser.add_argument(
        "--event-store-path",
        default=settings.agent_event_store_path,
        help="SQLite event store path (default from settings.agent_event_store_path).",
    )
    parser.add_argument(
        "--thread-prefix",
        default=f"run-manager-{uuid.uuid4().hex[:8]}",
        help="Thread id prefix for verification.",
    )
    parser.add_argument(
        "--summary-json",
        default=str(ROOT / "artifacts/runtime/run-manager-summary.json"),
        help="Path to write structured verification summary JSON.",
    )
    args = parser.parse_args()

    event_store_path = Path(args.event_store_path).resolve()
    event_store_path.parent.mkdir(parents=True, exist_ok=True)
    summary = asyncio.run(verify(event_store_path, args.thread_prefix))

    ok_completed = (
        summary["completed"]["status"] == "completed"
        and summary["completed"]["cancel_after_terminal_status"] == "completed"
        and summary["completed"]["cancel_after_terminal_added_events"] == 0
        and "run_created" in summary["completed"]["event_types"]
    )
    ok_cancelled = summary["cancelled"]["status"] == "cancelled" and "cancel_requested" in summary["cancelled"]["event_types"]
    ok_approved = (
        summary["approved"]["waiting_status_seen"]
        and summary["approved"]["status"] == "completed"
        and "approval_required" in summary["approved"]["event_types"]
        and "approval_resolved" in summary["approved"]["event_types"]
        and "tool_start" in summary["approved"]["event_types"]
    )
    ok_rejected = (
        summary["rejected"]["status"] == "completed"
        and "approval_resolved" in summary["rejected"]["event_types"]
        and not summary["rejected"]["tool_executed"]
    )
    passed = ok_completed and ok_cancelled and ok_approved and ok_rejected
    summary["runtime_run_manager"] = "PASS" if passed else "FAIL"

    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"event_store_path={summary['event_store_path']}")
    print(f"completed_status={summary['completed']['status']}")
    print(f"completed_cancel_after_terminal_status={summary['completed']['cancel_after_terminal_status']}")
    print(f"cancelled_status={summary['cancelled']['status']}")
    print(f"approved_status={summary['approved']['status']}")
    print(f"approved_waiting_status_seen={summary['approved']['waiting_status_seen']}")
    print(f"rejected_status={summary['rejected']['status']}")
    print(f"rejected_tool_executed={summary['rejected']['tool_executed']}")
    print(f"summary_json={summary_path}")
    print(f"runtime_run_manager={summary['runtime_run_manager']}")

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
