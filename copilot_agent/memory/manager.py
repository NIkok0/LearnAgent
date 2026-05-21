from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from copilot_agent.memory.policy import (
    EpisodicInjectBundle,
    MemoryPolicyConfig,
    build_episodic_inject_bundle,
    is_run_eligible_for_thread,
    memory_policy_from_settings,
    recall_episodic_runs,
)
from copilot_agent.memory.item_store import MemoryItemStore
from copilot_agent.memory.item_writer import (
    MemoryItemWriter,
    recall_long_term_items,
    render_long_term_memory_body,
)
from copilot_agent.rag import RagStore
from copilot_agent.runtime.event_store import EventStore
from copilot_agent.settings import settings

MEMORY_RUN_SUMMARY_EVENT = "memory_run_summary"
MEMORY_THREAD_SUMMARY_EVENT = "memory_thread_summary"
CHECKPOINT_COMPACTED_EVENT = "checkpoint_compacted"


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
        policy: MemoryPolicyConfig | None = None,
        llm_provider: Any | None = None,
    ) -> None:
        self._rag = rag_store
        self._events = event_store
        self.checkpoint_path = checkpoint_path
        self._policy = policy or memory_policy_from_settings(settings)
        self._llm_provider = llm_provider
        store_path = str(event_store.path) if event_store is not None else settings.agent_event_store_path
        self._item_store = MemoryItemStore(store_path) if self._policy.long_term_enabled else None
        self._item_writer = (
            MemoryItemWriter(self._item_store, policy=self._policy, llm_provider=self._llm_provider)
            if self._item_store is not None
            else None
        )

    def resolve_user_id(self, thread_id: str) -> str:
        if self._events is not None:
            return self._events.get_user_id(thread_id)
        return thread_id

    @property
    def policy(self) -> MemoryPolicyConfig:
        return self._policy

    @property
    def rag_store(self) -> RagStore:
        return self._rag

    @property
    def event_store(self) -> EventStore | None:
        return self._events

    def search_docs(self, query: str, top_k: int = 8):
        return self._rag.search(query, top_k=top_k)

    def search_docs_detailed(self, query: str, top_k: int = 8):
        return self._rag.search_detailed(query, top_k=top_k)

    def reload_rag_store(self, rag_store: RagStore) -> None:
        """Swap the in-process RAG store (hot reload)."""
        self._rag = rag_store

    def build_context(
        self,
        *,
        thread_id: str,
        run_id: str | None,
        messages: list[dict[str, Any]],
        goal: str,
    ) -> MemoryContext:
        bundle = self.get_memory_preview(thread_id, goal=goal, current_run_id=run_id)
        return MemoryContext(
            working={
                "thread_id": thread_id,
                "run_id": run_id,
                "goal": goal,
                "current_turn_messages": messages,
                "messages": messages,
                "checkpoint_path": self.checkpoint_path,
            },
            semantic={
                "rag_enabled": True,
                "rag_chunks": len(getattr(self._rag, "chunks", []) or []),
            },
            episodic={
                "enabled": self._policy.enabled,
                "thread_summary": bundle.thread_summary,
                "recalled_runs": bundle.recalled_runs,
                "recalled_long_term": bundle.recalled_long_term,
                "dropped_conflicts": bundle.dropped_conflicts,
                "inject_preview": bundle.inject_preview,
                "budget_applied": bundle.budget_applied,
                "sources": bundle.sources,
            },
        )

    def get_memory_preview(
        self,
        thread_id: str,
        *,
        goal: str | None = None,
        current_run_id: str | None = None,
    ) -> EpisodicInjectBundle:
        thread_summary = self.get_thread_summary(thread_id) if self._policy.enabled else None
        eligible = self.get_eligible_run_summaries(thread_id)
        recalled, dropped = recall_episodic_runs(
            run_summaries=eligible,
            goal=goal or "",
            current_run_id=current_run_id,
            config=self._policy,
        )
        long_term_rows: list[dict[str, Any]] = []
        long_term_body = ""
        if self._item_store is not None and self._policy.long_term_enabled and (goal or "").strip():
            user_id = self.resolve_user_id(thread_id)
            recalled_items = recall_long_term_items(
                store=self._item_store,
                user_id=user_id,
                thread_id=thread_id,
                query=goal or "",
                policy=self._policy,
                llm_provider=self._llm_provider,
            )
            long_term_rows = [row.as_dict() for row in recalled_items]
            long_term_body = render_long_term_memory_body(recalled_items)
        return build_episodic_inject_bundle(
            thread_summary=thread_summary,
            recalled_runs=recalled,
            dropped_conflicts=dropped,
            recalled_long_term=long_term_rows,
            long_term_body=long_term_body,
            config=self._policy,
        )

    def confirm_memory_item(self, item_id: str) -> dict[str, Any] | None:
        if self._item_store is None:
            return None
        confirmed = self._item_store.confirm_item(item_id)
        return confirmed.as_dict() if confirmed is not None else None

    def append_event(self, thread_id: str, run_id: str | None, event_type: str, payload: dict[str, Any]) -> None:
        if self._events is not None and run_id:
            self._events.append_event(thread_id, run_id, event_type, payload)

    def get_thread_events(self, thread_id: str, *, run_id: str | None = None) -> list[dict[str, Any]]:
        if self._events is None:
            return []
        return self._events.list_events(thread_id, run_id=run_id)

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
        summary = _summarize_run_events(events, fallback_goal=fallback_goal, policy=self._policy)
        self._events.append_event(thread_id, run_id, MEMORY_RUN_SUMMARY_EVENT, summary)
        if self._item_writer is not None:
            user_id = self.resolve_user_id(thread_id)
            self._item_writer.persist_run_memories(
                user_id=user_id,
                thread_id=thread_id,
                goal=str(summary.get("goal", "")),
                key_outputs=list(summary.get("key_outputs") or []),
                outcome=str(summary.get("outcome", "")),
                run_id=run_id,
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


def _summarize_run_events(
    events: list[dict[str, Any]],
    *,
    fallback_goal: str = "",
    policy: MemoryPolicyConfig,
) -> dict[str, Any]:
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
    output = _truncate("".join(token_parts).strip(), policy.key_output_max_chars)
    eligible_for_thread = is_run_eligible_for_thread({"outcome": outcome}, policy)
    summary = {
        "summary_type": "run",
        "goal": goal,
        "outcome": outcome,
        "tools_used": list(tools.keys()),
        "tool_details": list(tools.values()),
        "key_outputs": [output] if output else [],
        "errors": errors,
        "source_event_ids": source_event_ids,
        "eligible_for_thread": eligible_for_thread,
    }
    summary["char_count"] = len(str(summary))
    return summary


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
