#!/usr/bin/env python
"""Verify ExecutionEngine lifecycle, cancellation, approval, and thread guards."""

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

from copilot_agent.runtime.event_store import ActiveRunExistsError, EventStore, ThreadNotActiveError  # noqa: E402
from copilot_agent.runtime.execution_engine import ExecutionEngine, GraphInterrupted  # noqa: E402
from copilot_agent.runtime.timeline import TimelineProjector  # noqa: E402
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
        resume: bool | None = None,
    ) -> AsyncIterator[str]:
        thread_id = conversation_id
        scenario = str(messages[-1].get("content", "")) if messages else ""
        if resume is False:
            yield self._emit(thread_id, str(run_id), "token", {"text": "Dangerous tool call was rejected by the user."})
            yield self._emit(thread_id, str(run_id), "done", {})
            return
        if "slow" in scenario:
            await asyncio.sleep(30)
            yield self._emit(thread_id, str(run_id), "done", {})
            return
        if "boom" in scenario:
            yield self._emit(thread_id, str(run_id), "token", {"text": "before failure"})
            raise RuntimeError("simulated runtime failure")
        if "approval" in scenario and not confirm_dangerous and resume is None:
            payload = {"required": True, "reason": "dangerous_tool"}
            yield self._emit(thread_id, str(run_id), "approval_required", payload)
            raise GraphInterrupted(payload)
        yield self._emit(thread_id, str(run_id), "token", {"text": f"ok:{scenario}"})
        yield self._emit(
            thread_id,
            str(run_id),
            "tool_start",
            {
                "name": "search_docs",
                "call_id": run_id or "",
                "category": "memory",
                "risk_level": "low",
                "requires_approval": False,
                "arguments": {"query": "Redis"},
            },
        )
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
    manager = ExecutionEngine(event_store=store, runner=FakeRunner(store))  # type: ignore[arg-type]

    completed_thread = f"{thread_prefix}-completed"
    completed = await manager.create_run(
        thread_id=completed_thread,
        messages=[{"role": "user", "content": "normal"}],
    )
    completed_run = await _wait_for_status(store, completed.run_id, {"completed"})
    completed_events = store.list_run_events(completed.run_id)
    completed_tool_start = next((e for e in completed_events if e["type"] == "tool_start"), {})
    completed_events_before_cancel = len(completed_events)
    try:
        completed_cancel = await manager.cancel(completed.run_id)
    except KeyError:
        completed_cancel = store.get_run(completed.run_id) or {}
    completed_events_after_cancel = len(store.list_run_events(completed.run_id))
    second_completed = await manager.create_run(
        thread_id=completed_thread,
        messages=[{"role": "user", "content": "normal second"}],
    )
    second_completed_run = await _wait_for_status(store, second_completed.run_id, {"completed"})

    cancelled_thread = f"{thread_prefix}-cancelled"
    cancelled = await manager.create_run(
        thread_id=cancelled_thread,
        messages=[{"role": "user", "content": "slow"}],
    )
    await _wait_for_status(store, cancelled.run_id, {"running"})
    concurrent_blocked = False
    try:
        await manager.create_run(
            thread_id=cancelled_thread,
            messages=[{"role": "user", "content": "concurrent"}],
        )
    except ActiveRunExistsError:
        concurrent_blocked = True
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

    archived_thread = f"{thread_prefix}-archived"
    store.ensure_thread(archived_thread, title="archived verification")
    store.archive_thread(archived_thread)
    archived_blocked = False
    try:
        await manager.create_run(
            thread_id=archived_thread,
            messages=[{"role": "user", "content": "blocked"}],
        )
    except ThreadNotActiveError:
        archived_blocked = True

    failed_thread = f"{thread_prefix}-failed"
    failed = await manager.create_run(
        thread_id=failed_thread,
        messages=[{"role": "user", "content": "boom"}],
    )
    failed_run = await _wait_for_status(store, failed.run_id, {"failed"})
    failed_events = store.list_run_events(failed.run_id)
    failed_meta = next((e for e in failed_events if e["type"] == "run_failed_meta"), {})
    failed_meta_payload = failed_meta.get("payload", {}) if failed_meta else {}
    failed_error = next((e for e in failed_events if e["type"] == "error"), {})
    failed_timeline = TimelineProjector().project_run(failed_run, failed_events)
    failed_consistency = (failed_timeline.get("checkpoint") or {}).get("consistency") or {}

    return {
        "event_store_path": str(event_store_path),
        "completed": {
            "run_id": completed.run_id,
            "status": completed_run.get("status"),
            "cancel_after_terminal_status": completed_cancel.get("status"),
            "cancel_after_terminal_added_events": completed_events_after_cancel - completed_events_before_cancel,
            "second_run_status": second_completed_run.get("status"),
            "event_types": [e["type"] for e in completed_events],
            "tool_start_has_metadata": bool(
                completed_tool_start
                and completed_tool_start.get("payload", {}).get("category")
                and completed_tool_start.get("payload", {}).get("risk_level")
                and "requires_approval" in completed_tool_start.get("payload", {})
            ),
        },
        "cancelled": {
            "run_id": cancelled.run_id,
            "status": cancelled_run.get("status"),
            "concurrent_blocked": concurrent_blocked,
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
        "archived": {
            "blocked": archived_blocked,
        },
        "failed": {
            "run_id": failed.run_id,
            "status": failed_run.get("status"),
            "event_types": [e["type"] for e in failed_events],
            "error": failed_run.get("error"),
            "error_event_id": failed_error.get("id"),
            "failed_meta": failed_meta_payload,
            "timeline_failed_meta": (failed_timeline.get("checkpoint") or {}).get("failed"),
            "timeline_consistency": failed_consistency,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify LearnAgent ExecutionEngine runtime behavior.")
    parser.add_argument(
        "--event-store-path",
        default=settings.agent_event_store_path,
        help="SQLite event store path (default from settings.agent_event_store_path).",
    )
    parser.add_argument(
        "--thread-prefix",
        default=f"execution-engine-{uuid.uuid4().hex[:8]}",
        help="Thread id prefix for verification.",
    )
    parser.add_argument(
        "--summary-json",
        default=str(ROOT / "artifacts/runtime/execution-engine-summary.json"),
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
        and summary["completed"]["second_run_status"] == "completed"
        and summary["completed"]["tool_start_has_metadata"]
        and "run_created" in summary["completed"]["event_types"]
    )
    ok_cancelled = (
        summary["cancelled"]["status"] == "cancelled"
        and summary["cancelled"]["concurrent_blocked"]
        and "cancel_requested" in summary["cancelled"]["event_types"]
    )
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
    ok_archived = bool(summary["archived"]["blocked"])
    ok_failed = (
        summary["failed"]["status"] == "failed"
        and "error" in summary["failed"]["event_types"]
        and "run_failed_meta" in summary["failed"]["event_types"]
        and summary["failed"]["failed_meta"].get("reason") == "runtime_exception"
        and summary["failed"]["failed_meta"].get("phase") == "execute"
        and summary["failed"]["failed_meta"].get("last_successful_event_id") == summary["failed"]["error_event_id"]
        and summary["failed"]["timeline_failed_meta"].get("reason") == "runtime_exception"
        and summary["failed"]["timeline_consistency"].get("ok") is True
    )
    passed = ok_completed and ok_cancelled and ok_approved and ok_rejected and ok_archived and ok_failed
    summary["runtime_execution_engine"] = "PASS" if passed else "FAIL"

    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"event_store_path={summary['event_store_path']}")
    print(f"completed_status={summary['completed']['status']}")
    print(f"completed_cancel_after_terminal_status={summary['completed']['cancel_after_terminal_status']}")
    print(f"completed_second_run_status={summary['completed']['second_run_status']}")
    print(f"completed_tool_start_has_metadata={summary['completed']['tool_start_has_metadata']}")
    print(f"cancelled_status={summary['cancelled']['status']}")
    print(f"cancelled_concurrent_blocked={summary['cancelled']['concurrent_blocked']}")
    print(f"approved_status={summary['approved']['status']}")
    print(f"approved_waiting_status_seen={summary['approved']['waiting_status_seen']}")
    print(f"rejected_status={summary['rejected']['status']}")
    print(f"rejected_tool_executed={summary['rejected']['tool_executed']}")
    print(f"archived_blocked={summary['archived']['blocked']}")
    print(f"failed_status={summary['failed']['status']}")
    print(f"failed_meta_reason={summary['failed']['failed_meta'].get('reason')}")
    print(f"failed_meta_last_successful_event_id={summary['failed']['failed_meta'].get('last_successful_event_id')}")
    print(f"failed_consistency_ok={summary['failed']['timeline_consistency'].get('ok')}")
    print(f"summary_json={summary_path}")
    print(f"runtime_execution_engine={summary['runtime_execution_engine']}")

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
