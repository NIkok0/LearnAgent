#!/usr/bin/env python
"""Verify strict short-term to long-term conversion and composite eviction policy."""

from __future__ import annotations

import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.memory.item_schema import MemoryScope, MemoryType  # noqa: E402
from copilot_agent.memory.conversion_policy import conversion_skip_reason  # noqa: E402
from copilot_agent.memory.eviction_policy import memory_eviction_score  # noqa: E402
from copilot_agent.memory.item_store import MemoryItemStore  # noqa: E402
from copilot_agent.memory.item_writer import MemoryItemWriter  # noqa: E402
from copilot_agent.memory.policy_config import MemoryPolicyConfig  # noqa: E402
from copilot_agent.runtime.event_store import EventStore  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402


def _candidate(
    content: str,
    *,
    memory_type: MemoryType = MemoryType.FACT,
    importance: float = 0.8,
    confidence: float = 0.9,
    reusable: bool = True,
    source_kind: str = "final_answer",
    ttl_days: int | None = None,
) -> dict[str, object]:
    return {
        "scope": MemoryScope.SESSION,
        "memory_type": memory_type,
        "content": content,
        "importance": importance,
        "confidence": confidence,
        "source_run_id": "verify-run",
        "ttl_days": ttl_days,
        "reusable": reusable,
        "source_kind": source_kind,
        "source_event_ids": [1],
    }


def _touch_item(store: MemoryItemStore, item_id: str, *, days_old: int, access_count: int = 0) -> None:
    past = (datetime.now(UTC) - timedelta(days=days_old)).isoformat()
    with store._lock, store._connect() as conn:
        conn.execute(
            """
            UPDATE memory_items
            SET updated_at = ?, last_accessed_at = ?, access_count = ?
            WHERE id = ?
            """,
            (past, past if access_count else None, access_count, item_id),
        )


def _expire_soon(store: MemoryItemStore, item_id: str) -> None:
    expires = (datetime.now(UTC) + timedelta(hours=3)).isoformat()
    with store._lock, store._connect() as conn:
        conn.execute("UPDATE memory_items SET expires_at = ? WHERE id = ?", (expires, item_id))


def main(argv: list[str] | None = None) -> int:
    db_path = Path(settings.agent_event_store_path).with_name(
        f"verify-memory-conversion-eviction-{uuid.uuid4().hex[:8]}.sqlite"
    )
    db_path.parent.mkdir(parents=True, exist_ok=True)
    EventStore(str(db_path))
    store = MemoryItemStore(str(db_path))
    policy = MemoryPolicyConfig(
        enabled=True,
        long_term_enabled=True,
        llm_extract_enabled=False,
        long_term_importance_min=0.5,
        long_term_max_items_per_user=4,
        long_term_protected_importance=0.95,
        write_gate_enabled=True,
        write_min_confidence=0.7,
        write_require_reusable=True,
    )
    writer = MemoryItemWriter(store, policy=policy)

    conversion_user = f"user-conversion-{uuid.uuid4().hex[:8]}"
    conversion_thread = f"thread-conversion-{uuid.uuid4().hex[:8]}"
    completed_seed = _candidate("redis stream endpoint /api/tasks is reusable for deployment checks")
    completed_results = writer.persist_run_memories(
        user_id=conversion_user,
        thread_id=conversion_thread,
        goal="completed conversion",
        key_outputs=["no fallback should be needed"],
        outcome="completed",
        run_id="completed-run",
        memory_candidates_seed=[completed_seed],
    )
    failed_fact = _candidate("failed transient http 500 should not become durable fact")
    failed_results = writer.persist_run_memories(
        user_id=conversion_user,
        thread_id=conversion_thread,
        goal="failed conversion",
        key_outputs=[],
        outcome="failed",
        run_id="failed-run",
        memory_candidates_seed=[failed_fact],
    )
    failed_pref = _candidate(
        "prefer concise deployment summaries",
        memory_type=MemoryType.PREFERENCE,
        source_kind="user_preference",
    )
    failed_pref_result = writer.persist_run_memories(
        user_id=conversion_user,
        thread_id=conversion_thread,
        goal="failed but user preference explicit",
        key_outputs=[],
        outcome="failed",
        run_id="failed-pref-run",
        memory_candidates_seed=[failed_pref],
    )
    failed_policy = _candidate(
        "Policy block for /api/admin prevents unsafe writes",
        source_kind="policy_decision",
    )
    failed_policy_result = writer.persist_run_memories(
        user_id=conversion_user,
        thread_id=conversion_thread,
        goal="failed policy",
        key_outputs=[],
        outcome="failed",
        run_id="failed-policy-run",
        memory_candidates_seed=[failed_policy],
    )
    low_confidence_reason = conversion_skip_reason(
        _candidate("low confidence durable-looking fact", confidence=0.4),
        outcome="completed",
        policy=policy,
    )
    low_importance_reason = conversion_skip_reason(
        _candidate("low importance durable-looking fact", importance=0.2),
        outcome="completed",
        policy=policy,
    )
    non_reusable_reason = conversion_skip_reason(
        _candidate("one-off scratch result", reusable=False, memory_type=MemoryType.FACT),
        outcome="completed",
        policy=policy,
    )

    eviction_user = f"user-eviction-{uuid.uuid4().hex[:8]}"
    eviction_thread = f"thread-eviction-{uuid.uuid4().hex[:8]}"
    protected = writer.upsert_candidate(
        user_id=eviction_user,
        thread_id=eviction_thread,
        candidate=_candidate(
            "protected deployment preference",
            memory_type=MemoryType.PREFERENCE,
            importance=0.97,
            confidence=0.96,
        ),
    )
    used = writer.upsert_candidate(
        user_id=eviction_user,
        thread_id=eviction_thread,
        candidate=_candidate("redis stream active memory used often", importance=0.72, confidence=0.95),
    )
    old_fact = writer.upsert_candidate(
        user_id=eviction_user,
        thread_id=eviction_thread,
        candidate=_candidate("old low confidence transient fact", importance=0.55, confidence=0.52),
    )
    expiring = writer.upsert_candidate(
        user_id=eviction_user,
        thread_id=eviction_thread,
        candidate=_candidate("soon expiring temporary fact", importance=0.6, confidence=0.75, ttl_days=1),
    )
    pending = writer.upsert_candidate(
        user_id=eviction_user,
        thread_id=eviction_thread,
        candidate={**_candidate("pending unconfirmed fact", importance=0.7, confidence=0.8), "pending_confirmation": True},
    )
    if used.item is not None:
        _touch_item(store, used.item.id, days_old=1, access_count=6)
    if old_fact.item is not None:
        _touch_item(store, old_fact.item.id, days_old=90, access_count=0)
    if expiring.item is not None:
        _expire_soon(store, expiring.item.id)

    before_items = store.list_items(user_id=eviction_user, thread_id=eviction_thread, status="all", limit=20)
    before_scores = {item.id: memory_eviction_score(item) for item in before_items}
    removed = store.evict_lowest_score(
        user_id=eviction_user,
        keep_count=3,
        protected_importance=policy.long_term_protected_importance,
    )
    after_items = store.list_items(user_id=eviction_user, thread_id=eviction_thread, status="all", limit=20)
    after_by_id = {item.id: item for item in after_items}
    removed_items = [after_by_id[item_id] for item_id in removed if item_id in after_by_id]
    active_ids = {item.id for item in store.list_active(user_id=eviction_user, thread_id=eviction_thread, include_pending=True)}
    removed_reasons = [
        entry
        for item in removed_items
        for entry in item.history
        if isinstance(entry, dict) and entry.get("action") == "evicted"
    ]

    checks = {
        "completed_reusable_seed_inserted": any(result.action in {"insert", "dedup_skip"} for result in completed_results),
        "failed_fact_skipped": any(result.reason == "unstable_outcome" for result in failed_results),
        "failed_preference_allowed": any(result.action in {"insert", "dedup_skip"} for result in failed_pref_result),
        "failed_policy_fact_allowed": any(result.action in {"insert", "dedup_skip"} for result in failed_policy_result),
        "low_confidence_reason": low_confidence_reason == "low_confidence",
        "low_importance_reason": low_importance_reason == "below_importance_threshold",
        "non_reusable_reason": non_reusable_reason == "non_reusable",
        "eviction_removed_two": len(removed) == 2,
        "protected_not_evicted": protected.item is not None and protected.item.id in active_ids,
        "used_item_survives": used.item is not None and used.item.id in active_ids,
        "pending_or_expiring_evicted": (
            (pending.item is not None and pending.item.id in removed)
            or (expiring.item is not None and expiring.item.id in removed)
            or (old_fact.item is not None and old_fact.item.id in removed)
        ),
        "eviction_history_v2": bool(removed_reasons)
        and all(entry.get("reason") == "capacity_limit_v2" for entry in removed_reasons)
        and all(entry.get("eviction_score") is not None for entry in removed_reasons),
    }
    passed = all(checks.values())
    summary = {
        "suite_name": "memory_conversion_eviction_v1",
        "status": "PASS" if passed else "FAIL",
        "checks": checks,
        "conversion": {
            "completed": [result.as_dict() for result in completed_results],
            "failed_fact": [result.as_dict() for result in failed_results],
            "failed_preference": [result.as_dict() for result in failed_pref_result],
            "failed_policy": [result.as_dict() for result in failed_policy_result],
        },
        "eviction": {
            "removed": removed,
            "before_scores": before_scores,
            "active_ids": sorted(active_ids),
            "removed_history": removed_reasons,
        },
    }
    out_path = ROOT / "artifacts/runtime/memory-conversion-eviction-v1-summary.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"checks={json.dumps(checks, ensure_ascii=False)}")
    print(f"summary_json={out_path}")
    print(f"memory_conversion_eviction_v1={'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
