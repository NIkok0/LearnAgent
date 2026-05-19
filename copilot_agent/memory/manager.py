from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from copilot_agent.rag import RagStore
from copilot_agent.runtime.event_store import EventStore

MEMORY_RUN_SUMMARY_EVENT = "memory_run_summary"
MEMORY_THREAD_SUMMARY_EVENT = "memory_thread_summary"
MAX_KEY_OUTPUT_CHARS = 800
MAX_THREAD_SUMMARY_RUNS = 5


@dataclass(frozen=True)
class MemoryContext:
    working: dict[str, Any]
    semantic: dict[str, Any]
    episodic: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "working": self.working,
            "semantic": self.semantic,
            "episodic": self.episodic,
        }


class MemoryManager:
    """Facade over working, semantic, and episodic memory backends."""

    def __init__(
        self,
        *,
        rag_store: RagStore,
        event_store: EventStore | None,
        checkpoint_path: str,
    ) -> None:
        self._rag = rag_store
        self._events = event_store
        self.checkpoint_path = checkpoint_path

    @property
    def rag_store(self) -> RagStore:
        return self._rag

    @property
    def event_store(self) -> EventStore | None:
        return self._events

    def search_docs(self, query: str, top_k: int = 8):
        return self._rag.search(query, top_k=top_k)

    def build_context(
        self,
        *,
        thread_id: str,
        run_id: str | None,
        messages: list[dict[str, Any]],
        goal: str,
    ) -> MemoryContext:
        thread_summary = self.get_thread_summary(thread_id)
        return MemoryContext(
            working={
                "thread_id": thread_id,
                "run_id": run_id,
                "goal": goal,
                "messages": messages,
                "checkpoint_path": self.checkpoint_path,
            },
            semantic={
                "rag_enabled": True,
                "rag_chunks": len(getattr(self._rag, "chunks", []) or []),
            },
            episodic={
                "thread_summary": thread_summary,
            },
        )

    def append_event(self, thread_id: str, run_id: str | None, event_type: str, payload: dict[str, Any]) -> None:
        if self._events is not None and run_id:
            self._events.append_event(thread_id, run_id, event_type, payload)

    def get_thread_events(self, thread_id: str, *, run_id: str | None = None) -> list[dict[str, Any]]:
        if self._events is None:
            return []
        return self._events.list_events(thread_id, run_id=run_id)

    def summarize_run(self, thread_id: str, run_id: str, *, fallback_goal: str = "") -> dict[str, Any]:
        if self._events is None:
            return {}
        events = [
            event
            for event in self._events.list_run_events(run_id)
            if event.get("type") not in {MEMORY_RUN_SUMMARY_EVENT, MEMORY_THREAD_SUMMARY_EVENT}
        ]
        if not events:
            return {}
        summary = _summarize_run_events(events, fallback_goal=fallback_goal)
        self._events.append_event(thread_id, run_id, MEMORY_RUN_SUMMARY_EVENT, summary)
        return summary

    def update_thread_summary(self, thread_id: str, run_id: str | None = None) -> dict[str, Any]:
        if self._events is None:
            return {}
        summary_events = [
            event
            for event in self._events.list_events(thread_id)
            if event.get("type") == MEMORY_RUN_SUMMARY_EVENT
        ]
        if not summary_events:
            return {}
        latest_by_run: dict[str, dict[str, Any]] = {}
        for event in summary_events:
            latest_by_run[str(event.get("run_id", ""))] = event
        latest_events = list(latest_by_run.values())[-MAX_THREAD_SUMMARY_RUNS:]
        payloads = [event.get("payload", {}) for event in latest_events]
        summary = {
            "summary_type": "thread",
            "recent_goals": [_non_empty(payload.get("goal")) for payload in payloads if _non_empty(payload.get("goal"))],
            "recent_outcomes": [_non_empty(payload.get("outcome")) for payload in payloads if _non_empty(payload.get("outcome"))],
            "tools_used": sorted(
                {
                    str(tool)
                    for payload in payloads
                    for tool in payload.get("tools_used", [])
                    if str(tool)
                }
            ),
            "open_items": [],
            "source_run_ids": [str(event.get("run_id", "")) for event in latest_events if str(event.get("run_id", ""))],
            "source_event_ids": [int(event["id"]) for event in latest_events if event.get("id") is not None],
        }
        target_run_id = run_id or str(latest_events[-1].get("run_id", ""))
        if target_run_id:
            self._events.append_event(thread_id, target_run_id, MEMORY_THREAD_SUMMARY_EVENT, summary)
        return summary

    def get_thread_summary(self, thread_id: str) -> dict[str, Any] | None:
        if self._events is None:
            return None
        for event in reversed(self._events.list_events(thread_id)):
            if event.get("type") == MEMORY_THREAD_SUMMARY_EVENT:
                payload = event.get("payload")
                return payload if isinstance(payload, dict) else None
        return None


def _summarize_run_events(events: list[dict[str, Any]], *, fallback_goal: str = "") -> dict[str, Any]:
    goal = _goal_from_events(events) or fallback_goal
    outcome = _outcome_from_events(events)
    tools: dict[str, dict[str, str]] = {}
    token_parts: list[str] = []
    errors: list[str] = []
    source_event_ids: list[int] = []
    for event in events:
        if event.get("id") is not None:
            source_event_ids.append(int(event["id"]))
        payload = event.get("payload", {})
        event_type = str(event.get("type", ""))
        if event_type == "tool_start":
            name = str(payload.get("name", ""))
            if name:
                tools[name] = {
                    "name": name,
                    "category": str(payload.get("category", "")),
                    "risk_level": str(payload.get("risk_level", "")),
                }
        elif event_type == "token":
            text = str(payload.get("text", ""))
            if text:
                token_parts.append(text)
        elif event_type == "error":
            error = str(payload.get("error", ""))
            if error:
                errors.append(error)
    output = _truncate("".join(token_parts).strip(), MAX_KEY_OUTPUT_CHARS)
    return {
        "summary_type": "run",
        "goal": goal,
        "outcome": outcome,
        "tools_used": list(tools.keys()),
        "tool_details": list(tools.values()),
        "key_outputs": [output] if output else [],
        "errors": errors,
        "source_event_ids": source_event_ids,
    }


def _goal_from_events(events: list[dict[str, Any]]) -> str:
    for event in events:
        if event.get("type") == "plan_created":
            payload = event.get("payload", {})
            goal = str(payload.get("goal", "")).strip()
            if goal:
                return goal
    return ""


def _outcome_from_events(events: list[dict[str, Any]]) -> str:
    types = [str(event.get("type", "")) for event in events]
    if "cancelled" in types or "cancel_requested" in types:
        return "cancelled"
    if "error" in types:
        return "failed"
    if "done" in types:
        return "completed"
    return "unknown"


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def _non_empty(value: Any) -> str:
    return str(value or "").strip()
