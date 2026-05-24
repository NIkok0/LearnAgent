#!/usr/bin/env python
"""Verify production-grade long-term memory: dedup, version, TTL, recall, scope."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.memory import MemoryManager  # noqa: E402
from copilot_agent.memory.item_schema import MemoryScope, MemoryType  # noqa: E402
from copilot_agent.memory.item_store import MemoryItemStore, content_hash  # noqa: E402
from copilot_agent.memory.item_writer import MemoryItemWriter, recall_long_term_items  # noqa: E402
from copilot_agent.memory.policy import MemoryPolicyConfig  # noqa: E402
from copilot_agent.rag.retriever import RagStore  # noqa: E402
from copilot_agent.rag.schema import DocChunk  # noqa: E402
from copilot_agent.runtime.event_store import EventStore, RUN_STATUS_RUNNING  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402


def _seed_completed_run(
    memory: MemoryManager,
    store: EventStore,
    thread_id: str,
    *,
    goal: str,
    token: str,
) -> str:
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
    parser = argparse.ArgumentParser(description="Verify LearnAgent memory production Phase 1.")
    parser.add_argument("--event-store-path", default=settings.agent_event_store_path)
    parser.add_argument("--checkpoint-path", default=settings.agent_checkpoint_path)
    parser.add_argument(
        "--summary-json",
        default=str(ROOT / "artifacts/runtime/memory-production-v1-summary.json"),
    )
    args = parser.parse_args()

    event_store_path = Path(args.event_store_path).resolve()
    event_store_path.parent.mkdir(parents=True, exist_ok=True)
    store = EventStore(str(event_store_path))
    rag = RagStore([DocChunk(source="README.md", start_line=1, text="redis stream guide")])
    policy = MemoryPolicyConfig(
        enabled=True,
        long_term_enabled=True,
        llm_extract_enabled=False,
        long_term_recall_top_k=3,
        long_term_recall_min_score=0.2,
        long_term_importance_min=0.5,
        long_term_max_items_per_user=5,
        long_term_protected_importance=0.95,
        thread_summary_max_chars=1200,
        episodic_recall_top_k=2,
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

    demo_user_id = f"user-demo-{uuid.uuid4().hex[:8]}"
    thread_a = f"ltm-a-{uuid.uuid4().hex[:8]}"
    store.ensure_thread(thread_a, user_id=demo_user_id)
    run1 = _seed_completed_run(
        memory,
        store,
        thread_a,
        goal="check redis stream health for deployment",
        token="Redis stream health details from runbook.",
    )

    preview = memory.get_memory_preview(
        thread_a,
        goal="redis stream health",
        current_run_id=run1,
    )
    recalled_ids = preview.sources.get("memory_item_ids") or []

    dedup_thread = f"ltm-dedup-{uuid.uuid4().hex[:8]}"
    store.ensure_thread(dedup_thread, user_id="user-dedup")
    r1 = _seed_completed_run(memory, store, dedup_thread, goal="same goal text", token="output one")
    r2 = _seed_completed_run(memory, store, dedup_thread, goal="same goal text", token="output two")
    active_after_dedup = item_store.list_active(user_id="user-dedup", thread_id=dedup_thread)
    task_summaries = [item for item in active_after_dedup if item.memory_type == MemoryType.TASK_SUMMARY]

    user_id = f"user-conflict-{uuid.uuid4().hex[:8]}"
    pref_a = f"用户偏好简洁回答，不要长段落 ({uuid.uuid4().hex[:6]})"
    pref_b = f"用户偏好详细回答，需要长段落 ({uuid.uuid4().hex[:6]})"
    conflict_thread = f"ltm-conflict-{uuid.uuid4().hex[:8]}"
    store.ensure_thread(conflict_thread, user_id=user_id)
    first = writer.upsert_candidate(
        user_id=user_id,
        thread_id=conflict_thread,
        candidate={
            "scope": MemoryScope.USER,
            "memory_type": MemoryType.PREFERENCE,
            "content": pref_a,
            "importance": 0.85,
            "confidence": 0.9,
            "source_run_id": "manual",
        },
    )
    second = writer.upsert_candidate(
        user_id=user_id,
        thread_id=conflict_thread,
        candidate={
            "scope": MemoryScope.USER,
            "memory_type": MemoryType.PREFERENCE,
            "content": pref_b,
            "importance": 0.86,
            "confidence": 0.9,
            "source_run_id": "manual",
        },
    )
    old_item = item_store.get(first.item.id) if first.item else None
    new_item = second.item

    ttl_thread = f"ltm-ttl-{uuid.uuid4().hex[:8]}"
    store.ensure_thread(ttl_thread, user_id="user-ttl")
    ttl_item = writer.upsert_candidate(
        user_id="user-ttl",
        thread_id=ttl_thread,
        candidate={
            "scope": MemoryScope.SESSION,
            "memory_type": MemoryType.FACT,
            "content": "temporary fact about redis ttl test",
            "importance": 0.6,
            "confidence": 0.8,
            "source_run_id": "manual",
            "ttl_days": 1,
        },
    )
    deleted = 0
    if ttl_item.item is not None:
        expired = MemoryItemStore(str(event_store_path))
        with expired._lock, expired._connect() as conn:
            past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
            conn.execute("UPDATE memory_items SET expires_at = ? WHERE id = ?", (past, ttl_item.item.id))
        deleted = expired.delete_expired()

    cross_thread = f"ltm-cross-{uuid.uuid4().hex[:8]}"
    store.ensure_thread(cross_thread, user_id=user_id)
    cross_preview = memory.get_memory_preview(
        cross_thread,
        goal=second.item.content if second.item else pref_b,
    )
    cross_recall = cross_preview.recalled_long_term

    low_importance = writer.upsert_candidate(
        user_id="user-low",
        thread_id=conflict_thread,
        candidate={
            "scope": MemoryScope.SESSION,
            "memory_type": MemoryType.FACT,
            "content": "low value noise",
            "importance": 0.2,
            "confidence": 0.5,
            "source_run_id": "manual",
        },
    )

    pref_thread = f"ltm-pref-{uuid.uuid4().hex[:8]}"
    store.ensure_thread(pref_thread, user_id="user-pref")
    _seed_completed_run(
        memory,
        store,
        pref_thread,
        goal="我不喜欢带表情符号的回答",
        token="Please avoid emoji.",
    )
    pref_items = item_store.list_active(user_id="user-pref", thread_id=pref_thread)
    pref_hit = any(item.memory_type == MemoryType.PREFERENCE for item in pref_items)

    checks = {
        "run_summary_persisted": len(item_store.list_active(user_id=demo_user_id, thread_id=thread_a)) >= 1,
        "long_term_recall_in_preview": len(recalled_ids) >= 1 or len(preview.recalled_long_term) >= 1,
        "dedup_identical_goal": len(task_summaries) == 1,
        "conflict_supersede": second.action == "supersede" and old_item is not None and old_item.is_deprecated,
        "conflict_version_bump": new_item is not None and new_item.version >= 2,
        "ttl_deleted": deleted >= 1,
        "cross_thread_user_scope": len(cross_recall) >= 1,
        "importance_filter": low_importance.action == "skip",
        "preference_rule_extract": pref_hit,
        "resolve_user_id": memory.resolve_user_id(thread_a) == demo_user_id,
        "content_hash_stable": content_hash(" Hello ") == content_hash("hello"),
    }
    passed = all(checks.values())
    summary = {
        "thread_a": thread_a,
        "demo_user_id": demo_user_id,
        "run1": run1,
        "recalled_long_term": preview.recalled_long_term,
        "conflict_actions": {"first": first.action, "second": second.action},
        "checks": checks,
        "memory_production_v1": "PASS" if passed else "FAIL",
    }

    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    for key, value in checks.items():
        print(f"{key}={value}")
    print(f"summary_json={summary_path}")
    print(f"memory_production_v1={summary['memory_production_v1']}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
