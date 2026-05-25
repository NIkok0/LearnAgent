#!/usr/bin/env python
"""Verify long-term memory quality gates for prompt injection."""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.memory import MemoryManager  # noqa: E402
from copilot_agent.memory.injection_render import MEMORY_CONTEXT_PREFIX  # noqa: E402
from copilot_agent.memory.item_schema import MemoryScope, MemoryType  # noqa: E402
from copilot_agent.memory.item_writer import MemoryItemWriter  # noqa: E402
from copilot_agent.memory.policy_config import MemoryPolicyConfig  # noqa: E402
from copilot_agent.rag.schema import DocChunk  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402
from scripts._memory_verify_helpers import make_memory_fixture, unique_sqlite_path  # noqa: E402


def main() -> int:
    event_store_path = unique_sqlite_path("verify-memory-quality")
    policy = MemoryPolicyConfig(
        enabled=True,
        long_term_enabled=True,
        long_term_recall_top_k=2,
        long_term_recall_min_score=0.1,
        long_term_inject_min_score=0.3,
        long_term_max_per_type=1,
        thread_summary_max_chars=700,
        long_term_budget_chars=260,
        write_gate_enabled=True,
    )
    store, memory = make_memory_fixture(
        event_store_path=event_store_path,
        checkpoint_path=Path(settings.agent_checkpoint_path).resolve(),
        policy=policy,
        chunks=[DocChunk(source="RUNBOOK.md", start_line=1, text="queued task runbook")],
    )
    item_store = memory._item_store
    assert item_store is not None
    writer = MemoryItemWriter(item_store, policy=policy)
    user_id = f"user-quality-{uuid.uuid4().hex[:8]}"
    thread_id = f"thread-quality-{uuid.uuid4().hex[:8]}"
    store.ensure_thread(thread_id, user_id=user_id)

    relevant = writer.upsert_candidate(
        user_id=user_id,
        thread_id=thread_id,
        candidate={
            "scope": MemoryScope.SESSION,
            "memory_type": MemoryType.FACT,
            "content": "QUEUED watermark task usually requires checking Redis stream and worker health.",
            "importance": 0.9,
            "confidence": 0.95,
            "source_run_id": "manual-relevant",
        },
    )
    writer.upsert_candidate(
        user_id=user_id,
        thread_id=thread_id,
        candidate={
            "scope": MemoryScope.SESSION,
            "memory_type": MemoryType.FACT,
            "content": "Lunch preference is noodles on Friday.",
            "importance": 0.9,
            "confidence": 0.95,
            "source_run_id": "manual-unrelated",
        },
    )
    pending = writer.upsert_candidate(
        user_id=user_id,
        thread_id=thread_id,
        candidate={
            "scope": MemoryScope.SESSION,
            "memory_type": MemoryType.FACT,
            "content": "QUEUED watermark task should never check Redis stream.",
            "importance": 0.9,
            "confidence": 0.95,
            "pending_confirmation": True,
            "source_run_id": "manual-pending",
        },
    )

    bundle = memory.get_memory_preview(
        thread_id,
        goal="QUEUED watermark task 怎么排查 Redis stream worker?",
        route_context={"kind": "troubleshooting", "recommended_tools": ["search_docs", "http_get"]},
        record_access=False,
    )
    prompt = bundle.inject_preview
    dropped_reasons = {str(item.get("reason")) for item in bundle.dropped_long_term if isinstance(item, dict)}
    checks = {
        "memory_prompt_structured": prompt.startswith(MEMORY_CONTEXT_PREFIX)
        and "Relevant facts:" in prompt
        and "Rules:" in prompt,
        "prompt_under_budget": len(prompt) <= policy.thread_summary_max_chars,
        "relevant_memory_injected": relevant.item is not None and relevant.item.id in bundle.sources.get("memory_item_ids", []),
        "unrelated_memory_dropped": "low_score" in dropped_reasons,
        "pending_memory_not_injected": pending.item is not None and pending.item.id not in bundle.sources.get("memory_item_ids", []),
        "current_user_message_not_replaced": "QUEUED" not in prompt or len(prompt) < policy.thread_summary_max_chars,
    }
    passed = all(checks.values())
    summary = {
        "suite_name": "memory_quality",
        "status": "PASS" if passed else "FAIL",
        "checks": checks,
        "prompt_chars": len(prompt),
        "recalled_long_term": bundle.recalled_long_term,
        "dropped_long_term": bundle.dropped_long_term,
    }
    summary_path = ROOT / "artifacts/runtime/memory-quality-summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"checks={json.dumps(checks, ensure_ascii=False)}")
    print(f"summary_json={summary_path}")
    print(f"verify_memory_quality={'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
