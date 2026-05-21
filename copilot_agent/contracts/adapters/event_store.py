from __future__ import annotations

from typing import Any

from copilot_agent.contracts.base import RuntimeEvent
from copilot_agent.runtime.event_store import EventStore


class EventStoreAdapter:
    """Persist RuntimeEvent to SQLite EventStore."""

    @staticmethod
    def to_store_payload(event: RuntimeEvent) -> dict[str, Any]:
        return event.to_store_payload()

    @staticmethod
    def append(store: EventStore, event: RuntimeEvent) -> dict[str, Any]:
        thread_id = event.correlation.thread_id
        run_id = event.correlation.run_id
        if not thread_id or not run_id:
            raise ValueError("RuntimeEvent requires correlation.thread_id and correlation.run_id")
        return store.append_event(thread_id, run_id, event.kind, event.to_store_payload())

    @staticmethod
    def append_memory(memory: Any, event: RuntimeEvent) -> None:
        """Append via MemoryManager facade (same payload contract)."""
        thread_id = event.correlation.thread_id
        run_id = event.correlation.run_id
        if not thread_id or not run_id:
            return
        append = getattr(memory, "append_event", None)
        if callable(append):
            append(thread_id, run_id, event.kind, event.to_store_payload())
