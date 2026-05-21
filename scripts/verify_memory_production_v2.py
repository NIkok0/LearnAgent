#!/usr/bin/env python
"""Verify Phase 2 memory: vector recall, HyDE, LLM pending tags, checkpoint_compacted."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.memory import MemoryManager  # noqa: E402
from copilot_agent.memory.embedding import cosine_similarity, deterministic_embedding, embed_text  # noqa: E402
from copilot_agent.memory.hyde import build_hyde_query  # noqa: E402
from copilot_agent.memory.item_schema import MemoryScope, MemoryType  # noqa: E402
from copilot_agent.memory.item_writer import MemoryItemWriter, recall_long_term_items  # noqa: E402
from copilot_agent.memory.llm_extractor import extract_memories_for_run  # noqa: E402
from copilot_agent.memory.manager import CHECKPOINT_COMPACTED_EVENT  # noqa: E402
from copilot_agent.memory.policy import MemoryPolicyConfig  # noqa: E402
from copilot_agent.rag.retriever import RagStore  # noqa: E402
from copilot_agent.rag.schema import DocChunk  # noqa: E402
from copilot_agent.runtime.event_store import EventStore, RUN_STATUS_RUNNING  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402


def _seed_completed_run(memory: MemoryManager, store: EventStore, thread_id: str, *, goal: str, token: str) -> str:
    run = store.create_run(thread_id)
    run_id = str(run["id"])
    store.update_run_status(run_id, RUN_STATUS_RUNNING)
    memory.append_event(thread_id, run_id, "plan_created", {"goal": goal})
    memory.append_event(thread_id, run_id, "token", {"text": token})
    memory.append_event(thread_id, run_id, "done", {})
    store.complete_run(run_id)
    memory.summarize_run(thread_id, run_id, fallback_goal=goal)
    memory.update_thread_summary(thread_id, run_id)
    return run_id


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify LearnAgent memory production Phase 2.")
    parser.add_argument("--event-store-path", default=settings.agent_event_store_path)
    parser.add_argument("--checkpoint-path", default=settings.agent_checkpoint_path)
    parser.add_argument(
        "--summary-json",
        default=str(ROOT / "artifacts/runtime/memory-production-v2-summary.json"),
    )
    args = parser.parse_args()

    event_store_path = Path(args.event_store_path).resolve()
    event_store_path.parent.mkdir(parents=True, exist_ok=True)
    store = EventStore(str(event_store_path))
    rag = RagStore([DocChunk(source="README.md", start_line=1, text="redis stream guide")])
    policy = MemoryPolicyConfig(
        enabled=True,
        long_term_enabled=True,
        long_term_use_vector=True,
        long_term_embedding_deterministic=True,
        hyde_enabled=True,
        hyde_mode="rule",
        llm_extract_enabled=False,
        llm_confirm_threshold=0.7,
        long_term_recall_top_k=3,
        long_term_vector_min_score=0.1,
        long_term_recall_min_score=0.1,
    )
    memory = MemoryManager(
        rag_store=rag,
        event_store=store,
        checkpoint_path=str(Path(args.checkpoint_path).resolve()),
        policy=policy,
    )
    item_store = memory._item_store
    assert item_store is not None
    writer = MemoryItemWriter(item_store, policy=policy)

    user_id = f"user-p2-{uuid.uuid4().hex[:8]}"
    thread_id = f"thread-p2-{uuid.uuid4().hex[:8]}"
    store.ensure_thread(thread_id, user_id=user_id)
    run_id = _seed_completed_run(
        memory,
        store,
        thread_id,
        goal="check redis stream health for deployment",
        token="Redis stream health details from runbook.",
    )

    vector_thread = f"thread-vector-{uuid.uuid4().hex[:8]}"
    store.ensure_thread(vector_thread, user_id=user_id)
    writer.upsert_candidate(
        user_id=user_id,
        thread_id=vector_thread,
        candidate={
            "scope": MemoryScope.USER,
            "memory_type": MemoryType.FACT,
            "content": "User frequently troubleshoots redis stream health in deployment",
            "importance": 0.8,
            "confidence": 0.95,
            "source_run_id": "manual",
            "pending_confirmation": False,
        },
    )
    recalled = recall_long_term_items(
        store=item_store,
        user_id=user_id,
        thread_id=vector_thread,
        query="redis stream deployment health",
        policy=policy,
    )
    vector_hit = any(row.vector_score > 0 for row in recalled)

    hyde_query = build_hyde_query("redis stream health", policy=policy)
    hyde_ok = "redis stream health" in hyde_query.lower()

    pending_user = f"user-pending-{uuid.uuid4().hex[:8]}"
    pending_thread = f"thread-pending-{uuid.uuid4().hex[:8]}"
    store.ensure_thread(pending_thread, user_id=pending_user)
    pending_policy = MemoryPolicyConfig(
        enabled=True,
        long_term_enabled=True,
        llm_extract_enabled=False,
        llm_confirm_threshold=0.9,
    )
    pending_candidates = extract_memories_for_run(
        goal="maybe prefer shorter answers",
        key_outputs=["uncertain output"],
        outcome="completed",
        run_id="pending-run",
        policy=pending_policy,
    )
    pending_writer = MemoryItemWriter(item_store, policy=pending_policy)
    pending_results = [
        pending_writer.upsert_candidate(user_id=pending_user, thread_id=pending_thread, candidate=candidate)
        for candidate in pending_candidates
    ]
    pending_items = item_store.list_active(user_id=pending_user, thread_id=pending_thread, include_pending=True)
    inject_items = item_store.list_active(user_id=pending_user, thread_id=pending_thread, include_pending=False)
    pending_flag = any(item.pending_confirmation for item in pending_items)
    pending_excluded_from_inject = all(not item.pending_confirmation for item in inject_items)

    compact_thread = f"thread-compact-{uuid.uuid4().hex[:8]}"
    store.ensure_thread(compact_thread, user_id=f"user-compact-{uuid.uuid4().hex[:8]}")
    compact_run = store.create_run(compact_thread)
    compact_run_id = str(compact_run["id"])
    memory.append_event(
        compact_thread,
        compact_run_id,
        CHECKPOINT_COMPACTED_EVENT,
        {"compacted": True, "before_count": 42, "after_count": 8, "reason": "test"},
    )
    compact_events = [
        event
        for event in store.list_run_events(compact_run_id)
        if event.get("type") == CHECKPOINT_COMPACTED_EVENT
    ]

    embed_a = embed_text("redis stream health", use_vector=True, deterministic=True)
    embed_b = embed_text("redis stream health check", use_vector=True, deterministic=True)
    embed_similar = cosine_similarity(embed_a or [], embed_b or []) > 0.3

    checks = {
        "vector_recall_score": vector_hit,
        "hyde_rule_expand": hyde_ok,
        "pending_confirmation_flag": pending_flag,
        "pending_excluded_from_inject": pending_excluded_from_inject or len(inject_items) == 0,
        "checkpoint_compacted_event": len(compact_events) == 1,
        "deterministic_embedding_similarity": embed_similar,
        "run_summary_still_persisted": len(item_store.list_active(user_id=user_id, thread_id=thread_id)) >= 1,
        "confirm_memory_item": memory.confirm_memory_item(pending_items[0].id) is not None if pending_items else True,
    }
    passed = all(checks.values())
    summary = {
        "thread_id": thread_id,
        "run_id": run_id,
        "recalled": [row.as_dict() for row in recalled],
        "hyde_query": hyde_query,
        "pending_results": [result.action for result in pending_results],
        "compact_events": compact_events,
        "checks": checks,
        "memory_production_v2": "PASS" if passed else "FAIL",
    }

    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    for key, value in checks.items():
        print(f"{key}={value}")
    print(f"summary_json={summary_path}")
    print(f"memory_production_v2={summary['memory_production_v2']}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
