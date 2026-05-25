from __future__ import annotations

from typing import Any, Callable

from copilot_agent.memory.episodic_recall import is_run_eligible_for_thread
from copilot_agent.memory.item_writer import MemoryItemWriter
from copilot_agent.memory.policy_config import MemoryPolicyConfig
from copilot_agent.memory.short_term import build_short_term_run_summary
from copilot_agent.runtime.event_store import EventStore

MEMORY_RUN_SUMMARY_EVENT = "memory_run_summary"
MEMORY_THREAD_SUMMARY_EVENT = "memory_thread_summary"
CHECKPOINT_COMPACTED_EVENT = "checkpoint_compacted"


class MemorySummaryService:
    def __init__(
        self,
        *,
        event_store: EventStore | None,
        policy: MemoryPolicyConfig,
        item_writer: MemoryItemWriter | None,
        resolve_user_id: Callable[[str], str],
    ) -> None:
        self._events = event_store
        self._policy = policy
        self._item_writer = item_writer
        self._resolve_user_id = resolve_user_id

    def get_eligible_run_summaries(self, thread_id: str) -> list[dict[str, Any]]:
        if self._events is None:
            return []
        latest_by_run: dict[str, dict[str, Any]] = {}
        for event in self._events.list_events(thread_id):
            if event.get("type") != MEMORY_RUN_SUMMARY_EVENT:
                continue
            run_id = str(event.get("run_id", ""))
            if not run_id:
                continue
            latest_by_run[run_id] = {
                "run_id": run_id,
                "event_id": event.get("id"),
                "payload": event.get("payload", {}),
            }
        eligible = [
            item
            for item in latest_by_run.values()
            if isinstance(item.get("payload"), dict)
            and is_run_eligible_for_thread(item["payload"], self._policy)
        ]
        eligible.sort(key=lambda item: int(item.get("event_id") or 0))
        return eligible[-self._policy.thread_summary_max_runs :]

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
        summary = build_short_term_run_summary(events, fallback_goal=fallback_goal, policy=self._policy)
        self._events.append_event(thread_id, run_id, MEMORY_RUN_SUMMARY_EVENT, summary)
        if self._item_writer is not None:
            user_id = self._resolve_user_id(thread_id)
            self._item_writer.persist_run_memories(
                user_id=user_id,
                thread_id=thread_id,
                goal=str(summary.get("goal", "")),
                key_outputs=list(summary.get("key_outputs") or []),
                outcome=str(summary.get("outcome", "")),
                run_id=run_id,
                memory_candidates_seed=list(summary.get("memory_candidates_seed") or []),
            )
        return summary

    def update_thread_summary(self, thread_id: str, run_id: str | None = None) -> dict[str, Any]:
        if self._events is None:
            return {}
        eligible = self.get_eligible_run_summaries(thread_id)
        if not eligible:
            return {}
        payloads = [item["payload"] for item in eligible if isinstance(item.get("payload"), dict)]
        summary = {
            "summary_type": "thread",
            "recent_goals": [_non_empty(payload.get("goal")) for payload in payloads if _non_empty(payload.get("goal"))],
            "recent_outcomes": [
                _non_empty(payload.get("outcome")) for payload in payloads if _non_empty(payload.get("outcome"))
            ],
            "tools_used": sorted(
                {
                    str(tool)
                    for payload in payloads
                    for tool in payload.get("tools_used", [])
                    if str(tool)
                }
            ),
            "open_items": [],
            "source_run_ids": [str(item.get("run_id", "")) for item in eligible if str(item.get("run_id", ""))],
            "source_event_ids": [int(item["event_id"]) for item in eligible if item.get("event_id") is not None],
        }
        target_run_id = run_id or str(eligible[-1].get("run_id", ""))
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


def _non_empty(value: Any) -> str:
    return str(value or "").strip()


__all__ = [
    "CHECKPOINT_COMPACTED_EVENT",
    "MEMORY_RUN_SUMMARY_EVENT",
    "MEMORY_THREAD_SUMMARY_EVENT",
    "MemorySummaryService",
]
