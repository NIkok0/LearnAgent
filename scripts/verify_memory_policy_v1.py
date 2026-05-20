#!/usr/bin/env python
"""Verify MemoryManager policy: recall, budget, eligibility, conflict."""

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
from copilot_agent.memory.policy import MemoryPolicyConfig  # noqa: E402
from copilot_agent.rag.retriever import RagStore  # noqa: E402
from copilot_agent.rag.schema import DocChunk  # noqa: E402
from copilot_agent.runtime.event_store import EventStore, RUN_STATUS_CANCELLING, RUN_STATUS_RUNNING  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402


def _seed_run(
    memory: MemoryManager,
    store: EventStore,
    thread_id: str,
    *,
    goal: str,
    token: str,
    terminal: str = "done",
) -> str:
    run = store.create_run(thread_id)
    run_id = str(run["id"])
    store.update_run_status(run_id, RUN_STATUS_RUNNING)
    memory.append_event(thread_id, run_id, "plan_created", {"goal": goal})
    memory.append_event(thread_id, run_id, "token", {"text": token})
    if terminal == "done":
        memory.append_event(thread_id, run_id, "done", {})
        store.complete_run(run_id)
    elif terminal == "error":
        memory.append_event(thread_id, run_id, "error", {"error": "boom"})
        store.complete_run(run_id, error="boom")
    elif terminal == "cancelled":
        memory.append_event(thread_id, run_id, "cancel_requested", {})
        store.update_run_status(run_id, RUN_STATUS_CANCELLING)
        memory.append_event(thread_id, run_id, "cancelled", {})
        store.update_run_status(run_id, "cancelled", completed=True)
    memory.summarize_run(thread_id, run_id, fallback_goal=goal)
    memory.update_thread_summary(thread_id, run_id)
    return run_id


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify LearnAgent memory policy v1.")
    parser.add_argument("--event-store-path", default=settings.agent_event_store_path)
    parser.add_argument("--checkpoint-path", default=settings.agent_checkpoint_path)
    parser.add_argument(
        "--summary-json",
        default=str(ROOT / "artifacts/runtime/memory-policy-v1-summary.json"),
    )
    args = parser.parse_args()

    event_store_path = Path(args.event_store_path).resolve()
    event_store_path.parent.mkdir(parents=True, exist_ok=True)
    store = EventStore(str(event_store_path))
    rag = RagStore([DocChunk(source="README.md", start_line=1, text="Redis stream deployment guide")])
    policy = MemoryPolicyConfig(
        enabled=True,
        thread_summary_max_runs=5,
        thread_summary_max_chars=1200,
        episodic_recall_top_k=2,
        include_failed_runs=False,
        include_cancelled_runs=False,
        key_output_max_chars=800,
    )
    memory = MemoryManager(
        rag_store=rag,
        event_store=store,
        checkpoint_path=str(Path(args.checkpoint_path).resolve()),
        policy=policy,
    )

    recall_thread = f"policy-recall-{uuid.uuid4().hex[:8]}"
    run1 = _seed_run(
        memory,
        store,
        recall_thread,
        goal="check redis stream status",
        token="Redis stream status should be checked with docs.",
    )
    run2 = _seed_run(
        memory,
        store,
        recall_thread,
        goal="check redis stream health",
        token="Need redis stream health details.",
    )
    recall_preview = memory.get_memory_preview(
        recall_thread,
        goal="check redis stream health",
        current_run_id=run2,
    )

    conflict_thread = f"policy-conflict-{uuid.uuid4().hex[:8]}"
    _seed_run(memory, store, conflict_thread, goal="build mobile app in Swift", token="Swift mobile app")
    conflict_run = _seed_run(memory, store, conflict_thread, goal="build web dashboard in React", token="React dashboard")
    conflict_preview = memory.get_memory_preview(
        conflict_thread,
        goal="build web dashboard in React",
        current_run_id=conflict_run,
    )

    budget_thread = f"policy-budget-{uuid.uuid4().hex[:8]}"
    budget_policy = MemoryPolicyConfig(
        enabled=True,
        thread_summary_max_runs=5,
        thread_summary_max_chars=400,
        episodic_recall_top_k=2,
        include_failed_runs=False,
        include_cancelled_runs=False,
        key_output_max_chars=800,
    )
    budget_memory = MemoryManager(
        rag_store=rag,
        event_store=store,
        checkpoint_path=str(Path(args.checkpoint_path).resolve()),
        policy=budget_policy,
    )
    long_token = "x" * 2000
    budget_run = _seed_run(
        budget_memory,
        store,
        budget_thread,
        goal="budget test",
        token=long_token,
    )
    budget_preview = budget_memory.get_memory_preview(budget_thread, goal="budget test", current_run_id=budget_run)

    failed_thread = f"policy-failed-{uuid.uuid4().hex[:8]}"
    _seed_run(memory, store, failed_thread, goal="failed run goal", token="fail", terminal="error")
    ok_run = _seed_run(memory, store, failed_thread, goal="ok run goal", token="ok")
    failed_summary = memory.get_thread_summary(failed_thread) or {}

    rag_text = " ".join(chunk.text for chunk in rag.chunks)
    rag_isolated = "memory_run_summary" not in rag_text and "memory_thread_summary" not in rag_text

    checks = {
        "cross_run_recall": any(
            "redis" in str(item.get("goal", "")).lower() for item in recall_preview.recalled_runs
        ),
        "conflict_drop": len(conflict_preview.dropped_conflicts) >= 1 or len(conflict_preview.recalled_runs) == 0,
        "budget_cap": len(budget_preview.inject_preview) <= budget_policy.thread_summary_max_chars,
        "failed_excluded": "failed run goal" not in (failed_summary.get("recent_goals") or []),
        "ok_included": "ok run goal" in (failed_summary.get("recent_goals") or []),
        "rag_isolation": rag_isolated,
        "run1_eligible": bool(memory.get_eligible_run_summaries(recall_thread)),
    }
    passed = all(checks.values())
    summary = {
        "recall_thread": recall_thread,
        "run_ids": {"run1": run1, "run2": run2},
        "recalled_runs": recall_preview.recalled_runs,
        "dropped_conflicts": conflict_preview.dropped_conflicts,
        "budget_used_chars": budget_preview.budget_applied.get("used_chars"),
        "failed_summary_goals": failed_summary.get("recent_goals"),
        "checks": checks,
        "memory_policy_v1": "PASS" if passed else "FAIL",
    }

    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    for key, value in checks.items():
        print(f"{key}={value}")
    print(f"summary_json={summary_path}")
    print(f"memory_policy_v1={summary['memory_policy_v1']}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
