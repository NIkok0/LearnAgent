from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from copilot_agent.memory import MemoryManager
from copilot_agent.memory.policy_config import MemoryPolicyConfig
from copilot_agent.rag.retriever import RagStore
from copilot_agent.rag.schema import DocChunk
from copilot_agent.runtime.event_store import EventStore, RUN_STATUS_RUNNING
from copilot_agent.settings import settings


def unique_sqlite_path(prefix: str, *, base_path: str | None = None) -> Path:
    base = Path(base_path or settings.agent_event_store_path)
    path = base.with_name(f"{prefix}-{uuid.uuid4().hex[:8]}.sqlite")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def make_memory_fixture(
    *,
    event_store_path: str | Path | None = None,
    checkpoint_path: str | Path | None = None,
    policy: MemoryPolicyConfig | None = None,
    chunks: list[DocChunk] | None = None,
) -> tuple[EventStore, MemoryManager]:
    resolved_event_path = Path(event_store_path) if event_store_path is not None else unique_sqlite_path("verify-memory")
    resolved_event_path.parent.mkdir(parents=True, exist_ok=True)
    store = EventStore(str(resolved_event_path))
    rag = RagStore(chunks or [DocChunk(source="README.md", start_line=1, text="redis stream guide")])
    memory = MemoryManager(
        rag_store=rag,
        event_store=store,
        checkpoint_path=str(Path(checkpoint_path or settings.agent_checkpoint_path).resolve()),
        policy=policy or MemoryPolicyConfig(enabled=True, long_term_enabled=True),
    )
    return store, memory


def seed_completed_run(
    memory: MemoryManager,
    store: EventStore,
    thread_id: str,
    *,
    goal: str,
    token: str,
    done_payload: dict[str, Any] | None = None,
) -> str:
    run = store.create_run(thread_id)
    run_id = str(run["id"])
    store.update_run_status(run_id, RUN_STATUS_RUNNING)
    memory.append_event(thread_id, run_id, "plan_created", {"goal": goal})
    memory.append_event(thread_id, run_id, "token", {"text": token})
    memory.append_event(thread_id, run_id, "done", done_payload or {})
    store.complete_run(run_id)
    memory.summarize_run(thread_id, run_id, fallback_goal=goal)
    memory.update_thread_summary(thread_id, run_id)
    return run_id


def latest_run_summary(store: EventStore, run_id: str) -> dict[str, Any]:
    for event in reversed(store.list_run_events(run_id)):
        if event.get("type") == "memory_run_summary":
            payload = event.get("payload")
            return payload if isinstance(payload, dict) else {}
    return {}


__all__ = ["latest_run_summary", "make_memory_fixture", "seed_completed_run", "unique_sqlite_path"]
