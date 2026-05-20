#!/usr/bin/env python
"""Verify centralized run state transition validation."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.runtime.event_store import EventStore  # noqa: E402
from copilot_agent.runtime.execution_engine import ExecutionEngine  # noqa: E402
from copilot_agent.runtime.run_state import (  # noqa: E402
    RUN_STATUS_CANCELLED,
    RUN_STATUS_CANCELLING,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_RUNNING,
    RUN_STATUS_WAITING_APPROVAL,
    InvalidRunTransitionError,
)
from copilot_agent.settings import settings  # noqa: E402


class _NoopRunner:
    async def run_stream(self, **_kwargs):
        if False:
            yield ""


def _new_run(store: EventStore, prefix: str, name: str) -> tuple[str, str]:
    thread_id = f"{prefix}-{name}-{uuid.uuid4().hex[:8]}"
    run = store.create_run(thread_id)
    return thread_id, str(run["id"])


def _expect_invalid(fn) -> bool:
    try:
        fn()
    except InvalidRunTransitionError:
        return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify LearnAgent run state machine.")
    parser.add_argument("--event-store-path", default=settings.agent_event_store_path)
    parser.add_argument("--thread-prefix", default=f"run-state-{uuid.uuid4().hex[:8]}")
    parser.add_argument("--summary-json", default=str(ROOT / "artifacts/runtime/run-state-summary.json"))
    args = parser.parse_args()

    event_store_path = Path(args.event_store_path).resolve()
    event_store_path.parent.mkdir(parents=True, exist_ok=True)
    store = EventStore(str(event_store_path))

    _, completed_run_id = _new_run(store, args.thread_prefix, "completed")
    store.update_run_status(completed_run_id, RUN_STATUS_RUNNING)
    completed = store.update_run_status(completed_run_id, RUN_STATUS_COMPLETED, completed=True)
    completed_again = store.update_run_status(completed_run_id, RUN_STATUS_COMPLETED, completed=True)

    _, queued_failed_run_id = _new_run(store, args.thread_prefix, "queued-failed")
    queued_failed = store.update_run_status(queued_failed_run_id, RUN_STATUS_FAILED, error="startup failure", completed=True)

    _, approval_reject_run_id = _new_run(store, args.thread_prefix, "approval-reject")
    store.update_run_status(approval_reject_run_id, RUN_STATUS_RUNNING)
    store.update_run_status(approval_reject_run_id, RUN_STATUS_WAITING_APPROVAL)
    approval_reject = store.update_run_status(approval_reject_run_id, RUN_STATUS_COMPLETED, completed=True)

    _, cancelled_run_id = _new_run(store, args.thread_prefix, "cancelled")
    store.update_run_status(cancelled_run_id, RUN_STATUS_RUNNING)
    store.update_run_status(cancelled_run_id, RUN_STATUS_CANCELLING)
    cancelled = store.update_run_status(cancelled_run_id, RUN_STATUS_CANCELLED, completed=True)

    _, cancelling_failed_run_id = _new_run(store, args.thread_prefix, "cancelling-failed")
    store.update_run_status(cancelling_failed_run_id, RUN_STATUS_RUNNING)
    store.update_run_status(cancelling_failed_run_id, RUN_STATUS_CANCELLING)
    cancelling_failed = store.update_run_status(
        cancelling_failed_run_id,
        RUN_STATUS_FAILED,
        error="cancel cleanup failed",
        completed=True,
    )

    invalid_completed_to_running = _expect_invalid(
        lambda: store.update_run_status(completed_run_id, RUN_STATUS_RUNNING)
    )
    invalid_failed_to_completed = _expect_invalid(
        lambda: store.update_run_status(queued_failed_run_id, RUN_STATUS_COMPLETED, completed=True)
    )
    invalid_cancelled_to_failed = _expect_invalid(
        lambda: store.update_run_status(cancelled_run_id, RUN_STATUS_FAILED, error="late failure", completed=True)
    )
    invalid_queued_to_completed = _expect_invalid(
        lambda: store.update_run_status(_new_run(store, args.thread_prefix, "queued-completed")[1], RUN_STATUS_COMPLETED, completed=True)
    )

    orphan_run_ids = []
    for status in [None, RUN_STATUS_RUNNING, RUN_STATUS_WAITING_APPROVAL, RUN_STATUS_CANCELLING]:
        _, orphan_run_id = _new_run(store, args.thread_prefix, f"orphan-{status or 'queued'}")
        orphan_run_ids.append(orphan_run_id)
        if status is not None:
            if status in {RUN_STATUS_WAITING_APPROVAL, RUN_STATUS_CANCELLING}:
                store.update_run_status(orphan_run_id, RUN_STATUS_RUNNING)
            store.update_run_status(orphan_run_id, status)
    ExecutionEngine(event_store=store, runner=_NoopRunner())
    orphan_runs = [store.get_run(run_id) or {} for run_id in orphan_run_ids]

    checks = {
        "completed_status": completed.get("status") == RUN_STATUS_COMPLETED,
        "terminal_idempotent_status": completed_again.get("status") == RUN_STATUS_COMPLETED,
        "terminal_idempotent_completed_at": completed_again.get("completed_at") == completed.get("completed_at"),
        "queued_failed_status": queued_failed.get("status") == RUN_STATUS_FAILED,
        "approval_reject_status": approval_reject.get("status") == RUN_STATUS_COMPLETED,
        "cancelled_status": cancelled.get("status") == RUN_STATUS_CANCELLED,
        "cancelling_failed_status": cancelling_failed.get("status") == RUN_STATUS_FAILED,
        "terminal_completed_at": all(
            bool(run.get("completed_at"))
            for run in (completed, queued_failed, approval_reject, cancelled, cancelling_failed)
        ),
        "invalid_completed_to_running": invalid_completed_to_running,
        "invalid_failed_to_completed": invalid_failed_to_completed,
        "invalid_cancelled_to_failed": invalid_cancelled_to_failed,
        "invalid_queued_to_completed": invalid_queued_to_completed,
        "orphan_cleanup_failed": all(run.get("status") == RUN_STATUS_FAILED for run in orphan_runs),
        "orphan_cleanup_error": all(run.get("error") == "server restarted before run completed" for run in orphan_runs),
    }
    passed = all(checks.values())
    summary = {
        "event_store_path": str(event_store_path),
        "checks": checks,
        "run_state_machine": "PASS" if passed else "FAIL",
    }

    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    for key, value in checks.items():
        print(f"{key}={value}")
    print(f"summary_json={summary_path}")
    print(f"run_state_machine={summary['run_state_machine']}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
