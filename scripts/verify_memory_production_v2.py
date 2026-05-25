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
from copilot_agent.memory.item_writer import MemoryItemWriter  # noqa: E402
from copilot_agent.memory.llm_extractor import extract_memories_for_run  # noqa: E402
from copilot_agent.memory.manager import CHECKPOINT_COMPACTED_EVENT  # noqa: E402
from copilot_agent.memory.policy_config import MemoryPolicyConfig  # noqa: E402
from copilot_agent.memory.recall_policy import recall_long_term_items, recall_long_term_items_with_explain  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402
from scripts._memory_verify_helpers import make_memory_fixture, seed_completed_run  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify LearnAgent memory production Phase 2.")
    parser.add_argument("--event-store-path", default=settings.agent_event_store_path)
    parser.add_argument("--checkpoint-path", default=settings.agent_checkpoint_path)
    parser.add_argument(
        "--summary-json",
        default=str(ROOT / "artifacts/runtime/memory-production-v2-summary.json"),
    )
    args = parser.parse_args()

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
        recall_confidence_weight=0.2,
        recall_access_weight=0.2,
    )
    store, memory = make_memory_fixture(
        event_store_path=Path(args.event_store_path).resolve(),
        checkpoint_path=Path(args.checkpoint_path).resolve(),
        policy=policy,
    )
    item_store = memory._item_store
    assert item_store is not None
    writer = MemoryItemWriter(item_store, policy=policy)

    user_id = f"user-p2-{uuid.uuid4().hex[:8]}"
    thread_id = f"thread-p2-{uuid.uuid4().hex[:8]}"
    store.ensure_thread(thread_id, user_id=user_id)
    run_id = seed_completed_run(
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
    writer.upsert_candidate(
        user_id=user_id,
        thread_id=vector_thread,
        candidate={
            "scope": MemoryScope.USER,
            "memory_type": MemoryType.TASK_SUMMARY,
            "content": "redis stream deployment health troubleshooting lesson",
            "importance": 0.8,
            "confidence": 0.95,
            "source_run_id": "manual-task-summary",
            "pending_confirmation": False,
        },
    )
    intent_recalled, intent_dropped = recall_long_term_items_with_explain(
        store=item_store,
        user_id=user_id,
        thread_id=vector_thread,
        query="redis stream deployment health",
        policy=policy,
        route_kind="troubleshooting",
        recommended_tools=("search_docs", "http_get"),
        record_access=False,
    )
    task_summary_boosted = any(
        row.item.memory_type == MemoryType.TASK_SUMMARY and row.type_boost > policy.memory_type_boost_task_summary
        for row in intent_recalled
    )
    aging_thread = f"thread-aging-{uuid.uuid4().hex[:8]}"
    aging_user = f"user-aging-{uuid.uuid4().hex[:8]}"
    store.ensure_thread(aging_thread, user_id=aging_user)
    stale = writer.upsert_candidate(
        user_id=aging_user,
        thread_id=aging_thread,
        candidate={
            "scope": MemoryScope.USER,
            "memory_type": MemoryType.BEHAVIOR,
            "content": "redis stream health aging test archive memory baseline",
            "importance": 0.75,
            "confidence": 0.55,
            "source_run_id": "manual-stale",
        },
    )
    used = writer.upsert_candidate(
        user_id=aging_user,
        thread_id=aging_thread,
        candidate={
            "scope": MemoryScope.USER,
            "memory_type": MemoryType.FACT,
            "content": "redis stream health aging test active memory baseline",
            "importance": 0.75,
            "confidence": 0.95,
            "source_run_id": "manual-used",
        },
    )
    if stale.item and used.item:
        item_store.touch_access([used.item.id, used.item.id, used.item.id])
    aging_recalled, aging_dropped = recall_long_term_items_with_explain(
        store=item_store,
        user_id=aging_user,
        thread_id=aging_thread,
        query="redis stream health aging test memory",
        policy=policy,
        record_access=False,
    )
    used_row = next((row for row in aging_recalled if used.item and row.item.id == used.item.id), None)
    stale_row = next((row for row in aging_recalled if stale.item and row.item.id == stale.item.id), None)
    stale_dropped = next((row for row in aging_dropped if stale.item and row.get("id") == stale.item.id), None)

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
    listed_pending = memory.list_memory_items(pending_thread, status="pending")
    pending_flag = any(item.pending_confirmation for item in pending_items)
    pending_excluded_from_inject = all(not item.pending_confirmation for item in inject_items)
    rejected_item_ok = True
    if pending_items:
        reject_result = memory.reject_memory_item(
            pending_items[0].id,
            thread_id=pending_thread,
            reason="verify_reject",
        )
        rejected_item_ok = bool(reject_result and reject_result.get("is_deprecated"))

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
        "list_pending_memory_items": len(listed_pending) >= 1 if pending_items else True,
        "reject_memory_item": rejected_item_ok,
        "checkpoint_compacted_event": len(compact_events) == 1,
        "deterministic_embedding_similarity": embed_similar,
        "run_summary_still_persisted": len(item_store.list_active(user_id=user_id, thread_id=thread_id)) >= 1,
        "confirm_memory_item": memory.confirm_memory_item(pending_items[-1].id) is not None if len(pending_items) > 1 else True,
        "memory_type_boost": task_summary_boosted,
        "dropped_reason_available": all("reason" in item for item in intent_dropped),
        "aging_access_boost": used_row is not None and (stale_row is None or used_row.score > stale_row.score),
        "aging_factors_exposed": used_row is not None
        and (
            (stale_row is not None and used_row.access_factor > stale_row.access_factor)
            or (isinstance(stale_dropped, dict) and "access_factor" in stale_dropped)
        )
        and (
            (stale_row is not None and used_row.confidence_factor > stale_row.confidence_factor)
            or (isinstance(stale_dropped, dict) and "confidence_factor" in stale_dropped)
        ),
    }
    passed = all(checks.values())
    summary = {
        "thread_id": thread_id,
        "run_id": run_id,
        "recalled": [row.as_dict() for row in recalled],
        "intent_recalled": [row.as_dict() for row in intent_recalled],
        "intent_dropped": intent_dropped,
        "aging_recalled": [row.as_dict() for row in aging_recalled],
        "aging_dropped": aging_dropped,
        "hyde_query": hyde_query,
        "pending_results": [result.action for result in pending_results],
        "listed_pending": listed_pending,
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
