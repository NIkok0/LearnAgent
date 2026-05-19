#!/usr/bin/env python
"""Verify ended threads are archived by the lifecycle cleaner."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.runtime.event_store import EventStore, ThreadNotActiveError  # noqa: E402
from copilot_agent.runtime.thread_lifecycle import ThreadLifecycleCleaner  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402


def _set_thread_updated_at(db_path: Path, thread_id: str, updated_at: str) -> None:
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("UPDATE threads SET updated_at = ? WHERE id = ?", (updated_at, thread_id))


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify LearnAgent thread lifecycle cleaner.")
    parser.add_argument("--event-store-path", default=settings.agent_event_store_path)
    parser.add_argument("--summary-json", default=str(ROOT / "artifacts/runtime/thread-lifecycle-summary.json"))
    args = parser.parse_args()

    event_store_path = Path(args.event_store_path).resolve()
    event_store_path.parent.mkdir(parents=True, exist_ok=True)

    store = EventStore(str(event_store_path))
    old_thread_id = f"ended-old-{uuid.uuid4().hex[:8]}"
    fresh_thread_id = f"ended-fresh-{uuid.uuid4().hex[:8]}"

    store.ensure_thread(old_thread_id, title="old ended thread")
    run = store.create_run(old_thread_id)
    store.append_event(old_thread_id, run["id"], "token", {"text": "retained"})
    store.complete_run(run["id"])
    store.end_thread(old_thread_id)

    store.ensure_thread(fresh_thread_id, title="fresh ended thread")
    store.end_thread(fresh_thread_id)

    old_updated_at = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    _set_thread_updated_at(event_store_path, old_thread_id, old_updated_at)

    cleaner = ThreadLifecycleCleaner(event_store=store, ended_archive_ttl_seconds=60, interval_seconds=60)
    archived = cleaner.run_once()

    old_thread = store.get_thread(old_thread_id)
    fresh_thread = store.get_thread(fresh_thread_id)
    retained_runs = store.list_runs(old_thread_id)
    retained_events = store.list_run_events(run["id"])
    blocked_new_run = False
    try:
        store.create_run(old_thread_id)
    except ThreadNotActiveError:
        blocked_new_run = True

    summary = {
        "event_store_path": str(event_store_path),
        "archived_ids": [thread["id"] for thread in archived],
        "old_thread_status": old_thread.get("status") if old_thread else None,
        "fresh_thread_status": fresh_thread.get("status") if fresh_thread else None,
        "retained_run_count": len(retained_runs),
        "retained_event_types": [event["type"] for event in retained_events],
        "blocked_new_run": blocked_new_run,
    }
    passed = (
        old_thread is not None
        and old_thread.get("status") == "archived"
        and fresh_thread is not None
        and fresh_thread.get("status") == "ended"
        and old_thread_id in summary["archived_ids"]
        and len(retained_runs) == 1
        and summary["retained_event_types"] == ["token"]
        and blocked_new_run
    )
    summary["thread_lifecycle_cleaner"] = "PASS" if passed else "FAIL"

    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    for key, value in summary.items():
        print(f"{key}={value}")
    print(f"summary_json={summary_path}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
