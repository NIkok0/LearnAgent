from __future__ import annotations

from typing import Any

from copilot_agent.runtime.checkpoint_store import CheckpointStore
from copilot_agent.runtime.event_schema import EVENT_THREAD_CHECKPOINT_PURGED
from copilot_agent.runtime.event_store import EventStore


def purge_thread_checkpoint(
    event_store: EventStore,
    checkpoint_store: CheckpointStore,
    thread_id: str,
) -> int:
    deleted_rows = checkpoint_store.purge_thread(thread_id)
    if deleted_rows:
        run_id = event_store.latest_run_id(thread_id)
        if run_id:
            event_store.append_event(
                thread_id,
                run_id,
                EVENT_THREAD_CHECKPOINT_PURGED,
                {
                    "thread_id": thread_id,
                    "deleted_rows": deleted_rows,
                },
            )
    return deleted_rows


def archive_thread_and_purge_checkpoint(
    event_store: EventStore,
    checkpoint_store: CheckpointStore,
    thread_id: str,
) -> dict[str, Any] | None:
    thread = event_store.archive_thread(thread_id)
    if thread is None or str(thread.get("status", "")) != "archived":
        return thread
    purge_thread_checkpoint(event_store, checkpoint_store, thread_id)
    return thread
