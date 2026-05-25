from __future__ import annotations

from typing import Any, Callable

from copilot_agent.memory.item_store import MemoryItemStore
from copilot_agent.memory.episodic_recall import recall_episodic_runs
from copilot_agent.memory.injection_render import build_episodic_inject_bundle
from copilot_agent.memory.recall_policy import (
    recall_long_term_items_with_explain,
    render_long_term_memory_body,
)
from copilot_agent.memory.policy_config import MemoryPolicyConfig
from copilot_agent.memory.schema import EpisodicInjectBundle
from copilot_agent.memory.schema import MemoryContext


def build_memory_context(
    *,
    thread_id: str,
    run_id: str | None,
    messages: list[dict[str, Any]],
    goal: str,
    checkpoint_path: str,
    rag_store: Any,
    policy: MemoryPolicyConfig,
    bundle: EpisodicInjectBundle,
    route_context: dict[str, Any] | None = None,
) -> MemoryContext:
    return MemoryContext(
        working={
            "thread_id": thread_id,
            "run_id": run_id,
            "goal": goal,
            "current_turn_messages": messages,
            "messages": messages,
            "checkpoint_path": checkpoint_path,
        },
        semantic={
            "rag_enabled": True,
            "rag_chunks": len(getattr(rag_store, "chunks", []) or []),
        },
        episodic={
            "enabled": policy.enabled,
            "thread_summary": bundle.thread_summary,
            "recalled_runs": bundle.recalled_runs,
            "recalled_long_term": bundle.recalled_long_term,
            "dropped_conflicts": bundle.dropped_conflicts,
            "dropped_long_term": bundle.dropped_long_term,
            "inject_preview": bundle.inject_preview,
            "budget_applied": bundle.budget_applied,
            "sources": bundle.sources,
            "route_context": route_context or {},
        },
    )


def build_memory_preview(
    *,
    thread_id: str,
    goal: str,
    current_run_id: str | None,
    record_access: bool,
    route_context: dict[str, Any] | None,
    policy: MemoryPolicyConfig,
    item_store: MemoryItemStore | None,
    llm_provider: Any | None,
    resolve_user_id: Callable[[str], str],
    thread_summary: dict[str, Any] | None,
    eligible_run_summaries: list[dict[str, Any]],
) -> EpisodicInjectBundle:
    recalled, dropped = recall_episodic_runs(
        run_summaries=eligible_run_summaries,
        goal=goal,
        current_run_id=current_run_id,
        config=policy,
    )
    long_term_rows: list[dict[str, Any]] = []
    dropped_long_term: list[dict[str, Any]] = []
    long_term_body = ""
    if item_store is not None and policy.long_term_enabled and goal.strip():
        user_id = resolve_user_id(thread_id)
        route_kind = str((route_context or {}).get("kind") or "")
        recommended_tools = tuple(str(item) for item in (route_context or {}).get("recommended_tools", []) or [])
        recalled_items, dropped_long_term = recall_long_term_items_with_explain(
            store=item_store,
            user_id=user_id,
            thread_id=thread_id,
            query=goal,
            policy=policy,
            llm_provider=llm_provider,
            record_access=record_access,
            route_kind=route_kind,
            recommended_tools=recommended_tools,
        )
        long_term_rows = [row.as_dict() for row in recalled_items]
        long_term_body = render_long_term_memory_body(recalled_items)
    return build_episodic_inject_bundle(
        thread_summary=thread_summary,
        recalled_runs=recalled,
        dropped_conflicts=dropped,
        dropped_long_term=dropped_long_term,
        recalled_long_term=long_term_rows,
        long_term_body=long_term_body,
        config=policy,
    )


__all__ = ["build_memory_context", "build_memory_preview"]
