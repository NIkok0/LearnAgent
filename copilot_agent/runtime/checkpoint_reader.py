from __future__ import annotations

from typing import Any


class CheckpointReader:
    """Read LangGraph checkpoint snapshots for run↔checkpoint metadata."""

    def __init__(self, graph: Any) -> None:
        self._graph = graph

    async def snapshot(self, thread_id: str) -> dict[str, Any]:
        config = {"configurable": {"thread_id": thread_id}}
        state = await self._graph.aget_state(config)
        values = getattr(state, "values", None) or {}
        messages = values.get("messages") if isinstance(values, dict) else []
        message_count = len(messages) if isinstance(messages, list) else 0
        has_interrupt = bool(getattr(state, "next", None))
        return {
            "checkpoint_thread_id": thread_id,
            "message_count": message_count,
            "has_interrupt": has_interrupt,
        }
