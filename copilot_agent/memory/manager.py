from __future__ import annotations

from typing import Any

from copilot_agent.contracts.retrieval import RetrievalRequest
from copilot_agent.memory.context_builder import build_memory_context, build_memory_preview
from copilot_agent.memory.policy_config import MemoryPolicyConfig, memory_policy_from_settings
from copilot_agent.memory.schema import EpisodicInjectBundle
from copilot_agent.memory.item_store import MemoryItemStore
from copilot_agent.memory.item_schema import MemoryScope
from copilot_agent.memory.schema import MemoryContext
from copilot_agent.memory.item_writer import MemoryItemWriter
from copilot_agent.memory.summary_service import (
    CHECKPOINT_COMPACTED_EVENT,
    MEMORY_RUN_SUMMARY_EVENT,
    MEMORY_THREAD_SUMMARY_EVENT,
    MemorySummaryService,
)
from copilot_agent.rag import RagStore
from copilot_agent.runtime.event_store import EventStore, utc_now_iso
from copilot_agent.settings import settings


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
        self._summary_service = MemorySummaryService(
            event_store=self._events,
            policy=self._policy,
            item_writer=self._item_writer,
            resolve_user_id=self.resolve_user_id,
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

    def policy_aware_search_docs(self, request: RetrievalRequest, top_k: int = 8):
        return self._rag.policy_aware_search(request, top_k=top_k)

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
        record_memory_access: bool = True,
        route_context: dict[str, Any] | None = None,
    ) -> MemoryContext:
        bundle = self.get_memory_preview(
            thread_id,
            goal=goal,
            current_run_id=run_id,
            record_access=record_memory_access,
            route_context=route_context,
        )
        return build_memory_context(
            thread_id=thread_id,
            run_id=run_id,
            messages=messages,
            goal=goal,
            checkpoint_path=self.checkpoint_path,
            rag_store=self._rag,
            policy=self._policy,
            bundle=bundle,
            route_context=route_context,
        )

    def get_memory_preview(
        self,
        thread_id: str,
        *,
        goal: str | None = None,
        current_run_id: str | None = None,
        record_access: bool = True,
        route_context: dict[str, Any] | None = None,
    ) -> EpisodicInjectBundle:
        return build_memory_preview(
            thread_id=thread_id,
            goal=goal or "",
            current_run_id=current_run_id,
            record_access=record_access,
            route_context=route_context,
            policy=self._policy,
            item_store=self._item_store,
            llm_provider=self._llm_provider,
            resolve_user_id=self.resolve_user_id,
            thread_summary=self.get_thread_summary(thread_id) if self._policy.enabled else None,
            eligible_run_summaries=self.get_eligible_run_summaries(thread_id),
        )

    def confirm_memory_item(self, item_id: str, *, thread_id: str | None = None) -> dict[str, Any] | None:
        if self._item_store is None:
            return None
        if thread_id is not None:
            existing = self._item_store.get(item_id)
            if existing is None or existing.user_id != self.resolve_user_id(thread_id):
                return None
        confirmed = self._item_store.confirm_item(item_id)
        return confirmed.as_dict() if confirmed is not None else None

    def reject_memory_item(
        self,
        item_id: str,
        *,
        thread_id: str | None = None,
        reason: str = "rejected",
    ) -> dict[str, Any] | None:
        if self._item_store is None:
            return None
        item = self._item_store.get(item_id)
        if item is None:
            return None
        if thread_id is not None and item.user_id != self.resolve_user_id(thread_id):
            return None
        self._item_store.deprecate(
            item_id,
            history_entry={"action": "rejected", "reason": reason, "at": utc_now_iso()},
        )
        rejected = self._item_store.get(item_id)
        return rejected.as_dict() if rejected is not None else None

    def list_memory_items(
        self,
        thread_id: str,
        *,
        status: str = "active",
        scope: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if self._item_store is None:
            return []
        scopes: tuple[MemoryScope, ...] | None = None
        if scope:
            scopes = (MemoryScope(scope),)
        user_id = self.resolve_user_id(thread_id)
        return [
            item.as_dict()
            for item in self._item_store.list_items(
                user_id=user_id,
                thread_id=thread_id,
                status=status,
                scopes=scopes,
                limit=limit,
            )
        ]

    def append_event(self, thread_id: str, run_id: str | None, event_type: str, payload: dict[str, Any]) -> None:
        if self._events is not None and run_id:
            self._events.append_event(thread_id, run_id, event_type, payload)

    def get_thread_events(self, thread_id: str, *, run_id: str | None = None) -> list[dict[str, Any]]:
        if self._events is None:
            return []
        return self._events.list_events(thread_id, run_id=run_id)

    def get_eligible_run_summaries(self, thread_id: str) -> list[dict[str, Any]]:
        return self._summary_service.get_eligible_run_summaries(thread_id)

    def summarize_run(self, thread_id: str, run_id: str, *, fallback_goal: str = "") -> dict[str, Any]:
        return self._summary_service.summarize_run(thread_id, run_id, fallback_goal=fallback_goal)

    def update_thread_summary(self, thread_id: str, run_id: str | None = None) -> dict[str, Any]:
        return self._summary_service.update_thread_summary(thread_id, run_id)

    def get_thread_summary(self, thread_id: str) -> dict[str, Any] | None:
        return self._summary_service.get_thread_summary(thread_id)
