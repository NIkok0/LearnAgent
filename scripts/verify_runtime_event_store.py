#!/usr/bin/env python
"""Verify thread/run/event persistence without requiring OPENAI_API_KEY."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.runtime.event_store import EventStore, ThreadNotActiveError  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify LearnAgent runtime event store.")
    parser.add_argument(
        "--event-store-path",
        default=settings.agent_event_store_path,
        help="SQLite event store path (default from settings.agent_event_store_path).",
    )
    parser.add_argument(
        "--thread-id",
        default=f"runtime-{uuid.uuid4().hex[:8]}",
        help="Thread id for verification.",
    )
    parser.add_argument(
        "--summary-json",
        default=str(ROOT / "artifacts/runtime/event-store-summary.json"),
        help="Path to write structured verification summary JSON.",
    )
    args = parser.parse_args()

    event_store_path = Path(args.event_store_path).resolve()
    event_store_path.parent.mkdir(parents=True, exist_ok=True)

    store = EventStore(str(event_store_path))
    thread = store.ensure_thread(args.thread_id, title="runtime verification")
    run = store.create_run(args.thread_id)
    store.append_event(args.thread_id, run["id"], "token", {"text": "hello"})
    store.append_event(args.thread_id, run["id"], "tool_start", {"name": "search_docs", "arguments": {"query": "Redis"}})
    store.append_event(args.thread_id, run["id"], "tool_end", {"name": "search_docs", "result": {"sources": ["DEPLOY-SERVER.md"]}})
    store.append_event(args.thread_id, run["id"], "done", {})
    completed = store.complete_run(run["id"])

    fetched = store.get_thread(args.thread_id)
    fetched_run = store.get_run(run["id"])
    runs = store.list_runs(args.thread_id)
    events = store.list_events(args.thread_id, run_id=run["id"])
    run_events = store.list_run_events(run["id"])
    archived = store.archive_thread(args.thread_id)
    archived_runs = store.list_runs(args.thread_id)
    archived_events = store.list_run_events(run["id"])
    archived_create_blocked = False
    try:
        store.create_run(args.thread_id)
    except ThreadNotActiveError:
        archived_create_blocked = True

    ok_db_exists = event_store_path.exists()
    ok_thread = bool(thread and fetched and fetched["id"] == args.thread_id and fetched["status"] == "active")
    ok_run = bool(runs and fetched_run and completed["status"] == "completed" and completed["completed_at"])
    ok_events = [e["type"] for e in events] == ["token", "tool_start", "tool_end", "done"] and events == run_events
    ok_payload = events[0].get("payload", {}).get("text") == "hello" and "payload_json" not in events[0]
    ok_archived = bool(
        archived
        and archived["status"] == "archived"
        and archived_create_blocked
        and archived_runs
        and archived_events == run_events
    )

    summary = {
        "thread_id": args.thread_id,
        "run_id": run["id"],
        "event_store_path": str(event_store_path),
        "event_count": len(events),
        "event_types": [e["type"] for e in events],
        "db_exists": ok_db_exists,
        "thread_ok": ok_thread,
        "run_ok": ok_run,
        "events_ok": ok_events,
        "payload_ok": ok_payload,
        "archived_ok": ok_archived,
        "runtime_event_store": "PASS" if all([ok_db_exists, ok_thread, ok_run, ok_events, ok_payload, ok_archived]) else "FAIL",
    }

    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"thread_id={summary['thread_id']}")
    print(f"run_id={summary['run_id']}")
    print(f"event_store_path={summary['event_store_path']}")
    print(f"event_count={summary['event_count']}")
    print(f"event_types={','.join(summary['event_types'])}")
    print(f"db_exists={summary['db_exists']}")
    print(f"thread_ok={summary['thread_ok']}")
    print(f"run_ok={summary['run_ok']}")
    print(f"events_ok={summary['events_ok']}")
    print(f"payload_ok={summary['payload_ok']}")
    print(f"archived_ok={summary['archived_ok']}")
    print(f"summary_json={summary_path}")
    print(f"runtime_event_store={summary['runtime_event_store']}")

    return 0 if summary["runtime_event_store"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
