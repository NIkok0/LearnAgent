#!/usr/bin/env python
"""Verify Durable Runtime / Idempotency v1 contracts."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.runtime.event_store import EventStore, IdempotencyConflictError  # noqa: E402
from copilot_agent.runtime.execution_engine import ExecutionEngine, GraphInterrupted  # noqa: E402
from copilot_agent.runtime.run_state import (  # noqa: E402
    RUN_STATUS_CANCELLING,
    RUN_STATUS_QUEUED,
    RUN_STATUS_RUNNING,
    RUN_STATUS_WAITING_APPROVAL,
)
from copilot_agent.settings import settings  # noqa: E402


class DurableFakeRunner:
    def __init__(self, store: EventStore) -> None:
        self.store = store
        self.post_calls = 0

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
        run = str(run_id)
        text = str(messages[-1].get("content") or "") if messages else ""
        if "approval" in text and not confirm_dangerous and resume is None:
            payload = {"required": True, "reason": "dangerous_tool", "tool_calls": [{"name": "http_post", "id": "post-1"}]}
            self.store.append_event(thread_id, run, "approval_required", payload)
            raise GraphInterrupted(payload)
        if "post" in text or resume is True:
            self.store.append_event(
                thread_id,
                run,
                "tool_start",
                {
                    "name": "http_post",
                    "call_id": "post-1",
                    "category": "http",
                    "risk_level": "high",
                    "requires_approval": True,
                    "arguments": {"path": "/write", "idempotency_key": "tool-idem-1"},
                    "idempotency_key": "tool-idem-1",
                    "idempotency_key_present": True,
                },
            )
            existing = self.store.find_successful_tool_end_by_idempotency(
                run,
                tool_name="http_post",
                idempotency_key="tool-idem-1",
            )
            if existing is None:
                self.post_calls += 1
                self.store.append_event(
                    thread_id,
                    run,
                    "tool_end",
                    {
                        "name": "http_post",
                        "call_id": "post-1",
                        "result": {"success": True, "data": {"status_code": 200}, "error": None, "metadata": {}, "sanitized": True},
                        "duration_ms": 1,
                        "success": True,
                        "idempotency_key": "tool-idem-1",
                        "idempotency_key_present": True,
                        "attempt": 1,
                        "max_attempts": 1,
                        "retry_count": 0,
                    },
                )
            else:
                self.store.append_event(
                    thread_id,
                    run,
                    "tool_end",
                    {
                        "name": "http_post",
                        "call_id": "post-1-reused",
                        "result": {"success": True, "data": {"status_code": 200}, "error": None, "metadata": {}, "sanitized": True},
                        "duration_ms": 0,
                        "success": True,
                        "idempotency_key": "tool-idem-1",
                        "idempotency_key_present": True,
                        "idempotency_reused": True,
                    },
                )
        self.store.append_event(thread_id, run, "done", {"assistant_message": {"content": "ok"}})
        yield _sse("done", {})

    def finalize_memory(self, thread_id: str, run_id: str, *, messages: list[dict[str, Any]]) -> None:
        del thread_id, run_id, messages


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _wait_for_status(store: EventStore, run_id: str, statuses: set[str], *, timeout: float = 3.0) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        run = store.get_run(run_id) or {}
        if str(run.get("status") or "") in statuses:
            return run
        await asyncio.sleep(0.05)
    return store.get_run(run_id) or {}


async def verify(event_store_path: Path, thread_prefix: str) -> dict[str, Any]:
    store = EventStore(str(event_store_path))
    runner = DurableFakeRunner(store)
    engine = ExecutionEngine(event_store=store, runner=runner)  # type: ignore[arg-type]

    idem_thread = f"{thread_prefix}-idem"
    first = await engine.create_run(
        thread_id=idem_thread,
        messages=[{"role": "user", "content": "hello"}],
        idempotency_key="idem-1",
    )
    await _wait_for_status(store, first.run_id, {"completed"})
    second = await engine.create_run(
        thread_id=idem_thread,
        messages=[{"role": "user", "content": "hello"}],
        idempotency_key="idem-1",
    )
    conflict = False
    try:
        await engine.create_run(
            thread_id=idem_thread,
            messages=[{"role": "user", "content": "different"}],
            idempotency_key="idem-1",
        )
    except IdempotencyConflictError:
        conflict = True

    recovery_store = EventStore(str(event_store_path.with_name(f"{event_store_path.stem}-recovery.sqlite")))
    queued = recovery_store.create_run(f"{thread_prefix}-queued", status=RUN_STATUS_QUEUED)
    running = recovery_store.create_run(f"{thread_prefix}-running", status=RUN_STATUS_QUEUED)
    recovery_store.update_run_status(str(running["id"]), RUN_STATUS_RUNNING)
    cancelling = recovery_store.create_run(f"{thread_prefix}-cancelling", status=RUN_STATUS_QUEUED)
    recovery_store.update_run_status(str(cancelling["id"]), RUN_STATUS_RUNNING)
    recovery_store.update_run_status(str(cancelling["id"]), RUN_STATUS_CANCELLING)
    waiting = recovery_store.create_run(f"{thread_prefix}-waiting", status=RUN_STATUS_QUEUED)
    recovery_store.update_run_status(str(waiting["id"]), RUN_STATUS_RUNNING)
    recovery_store.append_event(str(waiting["thread_id"]), str(waiting["id"]), "approval_required", {"required": True})
    recovery_store.update_run_status(str(waiting["id"]), RUN_STATUS_WAITING_APPROVAL)
    restarted = ExecutionEngine(event_store=recovery_store, runner=DurableFakeRunner(recovery_store))  # type: ignore[arg-type]
    del restarted

    tool_thread = f"{thread_prefix}-tool"
    tool_run = await engine.create_run(
        thread_id=tool_thread,
        messages=[{"role": "user", "content": "post"}],
    )
    await _wait_for_status(store, tool_run.run_id, {"completed"})
    before_calls = runner.post_calls
    await runner.run_stream(
        conversation_id=tool_thread,
        run_id=tool_run.run_id,
        messages=[{"role": "user", "content": "post"}],
        confirm_dangerous=True,
    ).__anext__()
    after_calls = runner.post_calls
    tool_events = store.list_run_events(tool_run.run_id)

    terminal_runs = [first.run_id, tool_run.run_id, str(queued["id"]), str(running["id"]), str(cancelling["id"])]
    consistency = {
        run_id: any(event.get("type") == "run_consistency_checked" for event in (
            store.list_run_events(run_id) if store.get_run(run_id) else recovery_store.list_run_events(run_id)
        ))
        for run_id in terminal_runs
    }

    return {
        "idempotency": {
            "first_run_id": first.run_id,
            "second_run_id": second.run_id,
            "same_run_reused": first.run_id == second.run_id,
            "conflict": conflict,
        },
        "recovery": {
            "queued_status": (recovery_store.get_run(str(queued["id"])) or {}).get("status"),
            "running_status": (recovery_store.get_run(str(running["id"])) or {}).get("status"),
            "cancelling_status": (recovery_store.get_run(str(cancelling["id"])) or {}).get("status"),
            "waiting_status": (recovery_store.get_run(str(waiting["id"])) or {}).get("status"),
            "queued_failed_meta_reason": _event_payload(recovery_store, str(queued["id"]), "run_failed_meta").get("reason"),
            "running_failed_meta_reason": _event_payload(recovery_store, str(running["id"]), "run_failed_meta").get("reason"),
            "cancelling_cancel_reason": _event_payload(recovery_store, str(cancelling["id"]), "cancelled").get("reason"),
            "waiting_recovery_reason": (recovery_store.get_run(str(waiting["id"])) or {}).get("recovery_reason"),
        },
        "tool_idempotency": {
            "post_calls_before": before_calls,
            "post_calls_after": after_calls,
            "post_not_reexecuted": before_calls == after_calls,
            "reused_event": any((event.get("payload") or {}).get("idempotency_reused") is True for event in tool_events),
        },
        "consistency": consistency,
    }


def _event_payload(store: EventStore, run_id: str, event_type: str) -> dict[str, Any]:
    for event in reversed(store.list_run_events(run_id)):
        if event.get("type") == event_type:
            payload = event.get("payload")
            return payload if isinstance(payload, dict) else {}
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify durable runtime and idempotency v1.")
    parser.add_argument("--event-store-path", default=settings.agent_event_store_path)
    parser.add_argument("--thread-prefix", default=f"durability-{uuid.uuid4().hex[:8]}")
    parser.add_argument(
        "--summary-json",
        default=str(ROOT / "artifacts/runtime/runtime-durability-v1-summary.json"),
    )
    args = parser.parse_args()

    event_store_path = Path(args.event_store_path).resolve()
    event_store_path.parent.mkdir(parents=True, exist_ok=True)
    summary = asyncio.run(verify(event_store_path, args.thread_prefix))
    checks = {
        "idempotent_run_reused": summary["idempotency"]["same_run_reused"],
        "idempotency_conflict": summary["idempotency"]["conflict"],
        "queued_failed_on_restart": summary["recovery"]["queued_status"] == "failed"
        and summary["recovery"]["queued_failed_meta_reason"] == "process_restarted",
        "running_failed_on_restart": summary["recovery"]["running_status"] == "failed"
        and summary["recovery"]["running_failed_meta_reason"] == "process_restarted",
        "cancelling_cancelled_on_restart": summary["recovery"]["cancelling_status"] == "cancelled"
        and summary["recovery"]["cancelling_cancel_reason"] == "process_restarted",
        "waiting_approval_rehydrated": summary["recovery"]["waiting_status"] == "waiting_approval"
        and summary["recovery"]["waiting_recovery_reason"] == "waiting_approval_rehydrated",
        "tool_idempotency_reused": summary["tool_idempotency"]["post_not_reexecuted"]
        and summary["tool_idempotency"]["reused_event"],
        "terminal_consistency_checked": all(summary["consistency"].values()),
    }
    summary["checks"] = checks
    summary["runtime_durability_v1"] = "PASS" if all(checks.values()) else "FAIL"

    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    for key, value in checks.items():
        print(f"{key}={value}")
    print(f"summary_json={summary_path}")
    print(f"runtime_durability_v1={summary['runtime_durability_v1']}")
    return 0 if summary["runtime_durability_v1"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
