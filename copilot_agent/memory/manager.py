from __future__ import annotations

from typing import Any

from copilot_agent.contracts.retrieval import RetrievalRequest
from copilot_agent.memory.context_builder import build_memory_context, build_memory_preview
from copilot_agent.memory.policy_config import MemoryPolicyConfig, memory_policy_from_settings
from copilot_agent.memory.schema import EpisodicInjectBundle
from copilot_agent.memory.item_store import DELETED_MEMORY_CONTENT, MemoryItemStore
from copilot_agent.memory.item_schema import MemoryItemRecord, MemoryScope
from copilot_agent.memory.schema import MemoryContext
from copilot_agent.memory.item_writer import MemoryItemWriter
from copilot_agent.memory.summary_service import (
    CHECKPOINT_COMPACTED_EVENT,
    MEMORY_RUN_SUMMARY_EVENT,
    MEMORY_THREAD_SUMMARY_EVENT,
    MemorySummaryService,
)
from copilot_agent.rag import RagStore
from copilot_agent.runtime.event_schema import (
    EVENT_MEMORY_ITEM_CONFIRMED,
    EVENT_MEMORY_ITEM_DELETED,
    EVENT_MEMORY_ITEM_DELETE_PROOF,
    EVENT_MEMORY_ITEM_REJECTED,
)
from copilot_agent.runtime.event_store import ActiveRunExistsError, EventStore, utc_now_iso
from copilot_agent.runtime.run_state import RUN_STATUS_COMPLETED
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
        """Compatibility wrapper for unchecked debug/test retrieval; production code must use policy-aware search."""
        return self.search_docs_unchecked(query, top_k=top_k)

    def search_docs_unchecked(self, query: str, top_k: int = 8):
        return self._rag.search(query, top_k=top_k)

    def search_docs_detailed(self, query: str, top_k: int = 8):
        """Compatibility wrapper for unchecked debug/test retrieval; production code must use policy-aware search."""
        return self.search_docs_detailed_unchecked(query, top_k=top_k)

    def search_docs_detailed_unchecked(self, query: str, top_k: int = 8):
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
        if confirmed is not None:
            self._record_memory_governance(
                EVENT_MEMORY_ITEM_CONFIRMED,
                confirmed,
                action="confirmed",
                reason="confirmed",
                actor="user",
            )
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
        if rejected is not None:
            self._record_memory_governance(
                EVENT_MEMORY_ITEM_REJECTED,
                rejected,
                action="rejected",
                reason=reason,
                actor="user",
            )
        return rejected.as_dict() if rejected is not None else None

    def delete_memory_item(
        self,
        item_id: str,
        *,
        thread_id: str,
        reason: str = "user_deleted",
        actor: str = "user",
    ) -> dict[str, Any] | None:
        if self._item_store is None:
            return None
        item = self._item_store.get(item_id)
        if item is None or item.user_id != self.resolve_user_id(thread_id):
            return None
        deleted_at = utc_now_iso()
        deleted = self._item_store.delete_with_tombstone(
            item_id,
            history_entry={
                "action": "deleted",
                "reason": reason,
                "at": deleted_at,
                "actor": actor,
                "content_redacted": True,
                "embedding_removed": True,
            },
        )
        if deleted is None:
            return None
        delete_event = self._record_memory_governance(
            EVENT_MEMORY_ITEM_DELETED,
            deleted,
            action="deleted",
            reason=reason,
            actor=actor,
            at=deleted_at,
            content_redacted=True,
            embedding_removed=True,
        )
        self._record_memory_delete_proof(
            deleted,
            reason=reason,
            actor=actor,
            deleted_at=deleted_at,
            delete_event_id=int((delete_event or {}).get("id") or 0),
        )
        return deleted.as_dict()

    def latest_memory_delete_proof(self, thread_id: str, item_id: str) -> dict[str, Any] | None:
        if self._events is None:
            return None
        user_id = self.resolve_user_id(thread_id)
        for event in reversed(self._events.list_events(thread_id)):
            if str(event.get("type") or "") != EVENT_MEMORY_ITEM_DELETE_PROOF:
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            if str(payload.get("item_id") or "") != item_id:
                continue
            if str(payload.get("user_id") or "") != user_id:
                continue
            return payload
        return None

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
            _sanitize_memory_item_for_api(item)
            for item in self._item_store.list_items(
                user_id=user_id,
                thread_id=thread_id,
                status=status,
                scopes=scopes,
                limit=limit,
            )
        ]

    def _record_memory_governance(
        self,
        event_type: str,
        item: MemoryItemRecord,
        *,
        action: str,
        reason: str,
        actor: str,
        at: str | None = None,
        content_redacted: bool = False,
        embedding_removed: bool = False,
    ) -> dict[str, Any] | None:
        if self._events is None:
            return None
        event_at = at or utc_now_iso()
        run_id = self._memory_governance_run_id(item.thread_id or item.user_id)
        payload = _memory_governance_payload(
            item,
            action=action,
            reason=reason,
            actor=actor,
            at=event_at,
            content_redacted=content_redacted,
            embedding_removed=embedding_removed,
        )
        return self._events.append_event(item.thread_id or item.user_id, run_id, event_type, payload)

    def _record_memory_delete_proof(
        self,
        item: MemoryItemRecord,
        *,
        reason: str,
        actor: str,
        deleted_at: str,
        delete_event_id: int,
    ) -> dict[str, Any] | None:
        if self._events is None:
            return None
        run_id = self._memory_governance_run_id(item.thread_id or item.user_id)
        payload = {
            "item_id": item.id,
            "user_id": item.user_id,
            "thread_id": item.thread_id,
            "scope": item.scope.value,
            "memory_type": item.memory_type.value,
            "source_run_id": item.source_run_id,
            "deleted_at": deleted_at,
            "reason": reason,
            "deleted_by": actor,
            "content_redacted": True,
            "embedding_removed": True,
            "delete_event_id": delete_event_id,
        }
        return self._events.append_event(item.thread_id or item.user_id, run_id, EVENT_MEMORY_ITEM_DELETE_PROOF, payload)

    def _memory_governance_run_id(self, thread_id: str) -> str:
        if self._events is None:
            return ""
        run_id = f"memory-governance-{thread_id}"
        if self._events.get_run(run_id) is None:
            try:
                self._events.create_run(thread_id, run_id=run_id, status=RUN_STATUS_COMPLETED)
            except ActiveRunExistsError:
                runs = self._events.list_runs(thread_id)
                if runs:
                    return str(runs[-1].get("id") or run_id)
                raise
        return run_id

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


def _memory_governance_payload(
    item: MemoryItemRecord,
    *,
    action: str,
    reason: str,
    actor: str,
    at: str,
    content_redacted: bool,
    embedding_removed: bool,
) -> dict[str, Any]:
    return {
        "item_id": item.id,
        "user_id": item.user_id,
        "thread_id": item.thread_id,
        "scope": item.scope.value,
        "memory_type": item.memory_type.value,
        "source_run_id": item.source_run_id,
        "action": action,
        "reason": reason,
        "actor": actor,
        "at": at,
        "deleted_at": at if action == "deleted" else None,
        "deleted_by": actor if action == "deleted" else None,
        "content_redacted": content_redacted,
        "embedding_removed": embedding_removed,
    }


def _sanitize_memory_item_for_api(item: MemoryItemRecord) -> dict[str, Any]:
    payload = item.as_dict()
    if item.is_deprecated and item.content == DELETED_MEMORY_CONTENT:
        payload.pop("content", None)
        payload["content_redacted"] = True
        payload["tombstone"] = True
    return payload
