#!/usr/bin/env python
"""Verify deterministic short-term memory formation and long-term seed handoff."""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.contracts.events.registry import validate_payload_for_kind  # noqa: E402
from copilot_agent.memory import MemoryManager  # noqa: E402
from copilot_agent.memory.item_schema import MemoryType  # noqa: E402
from copilot_agent.memory.policy_config import MemoryPolicyConfig  # noqa: E402
from copilot_agent.rag.retriever import RagStore  # noqa: E402
from copilot_agent.rag.schema import DocChunk  # noqa: E402
from copilot_agent.runtime.event_schema import EVENT_MEMORY_RUN_SUMMARY  # noqa: E402
from copilot_agent.runtime.event_store import EventStore, RUN_STATUS_FAILED, RUN_STATUS_RUNNING  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402
from copilot_agent.tools.audit import audit_payload_has_secret  # noqa: E402


def _make_memory(event_store_path: Path, checkpoint_path: Path) -> tuple[EventStore, MemoryManager]:
    store = EventStore(str(event_store_path))
    memory = MemoryManager(
        rag_store=RagStore([DocChunk(source="README.md", start_line=1, text="short memory verify")]),
        event_store=store,
        checkpoint_path=str(checkpoint_path),
        policy=MemoryPolicyConfig(
            enabled=True,
            long_term_enabled=True,
            llm_extract_enabled=False,
            long_term_importance_min=0.5,
            write_gate_enabled=True,
            write_require_reusable=True,
        ),
    )
    return store, memory


def _seed_completed_run(store: EventStore, memory: MemoryManager, thread_id: str) -> str:
    run = store.create_run(thread_id)
    run_id = str(run["id"])
    store.update_run_status(run_id, RUN_STATUS_RUNNING)
    memory.append_event(thread_id, run_id, "plan_created", {"goal": "ship memory summary builder"})
    memory.append_event(thread_id, run_id, "token", {"text": "noisy streamed token should not win"})
    memory.append_event(
        thread_id,
        run_id,
        "tool_start",
        {
            "name": "http_post",
            "call_id": "call-1",
            "category": "http",
            "risk_level": "high",
            "requires_approval": True,
            "arguments": {"path": "/api/jobs?token=secret-token", "json_body": {"password": "secret"}},
        },
    )
    memory.append_event(
        thread_id,
        run_id,
        "tool_end",
        {
            "name": "http_post",
            "call_id": "call-1",
            "success": True,
            "result": {
                "success": True,
                "data": {"path": "/api/jobs?token=secret-token", "status_code": 201},
                "metadata": {"status_code": 201},
            },
        },
    )
    memory.append_event(
        thread_id,
        run_id,
        "tool_side_effect_recorded",
        {
            "tool_name": "http_post",
            "call_id": "call-1",
            "path": "/api/jobs",
            "method": "POST",
            "risk_level": "high",
            "requires_approval": True,
            "approval_status": "approved",
            "side_effect_status": "confirmed",
            "success": True,
            "status_code": 201,
            "idempotency_key": "idem-1",
            "idempotency_reused": False,
            "compensatable": False,
            "reason": "success",
        },
    )
    memory.append_event(
        thread_id,
        run_id,
        "retrieval_completed",
        {
            "query": "memory builder",
            "sources": [{"source_file": "memory.md", "chunk_index": 2, "http_path": "/docs?cookie=secret"}],
            "source_count": 1,
            "excerpt_chars": 100,
        },
    )
    memory.append_event(
        thread_id,
        run_id,
        "rag_document_ingested",
        {
            "doc_id": "doc-1",
            "source_file": "memory.md",
            "tenant_id": "tenant-a",
            "classification": "internal",
            "pii_level": "low",
            "source_hash": "hash-1",
            "chunk_count": 3,
        },
    )
    memory.append_event(
        thread_id,
        run_id,
        "done",
        {
            "assistant_message": {"content": "fallback assistant message"},
            "final_answer": {
                "answer": "Implemented deterministic short-term memory seed builder.",
                "citations": [],
                "tool_evidence": [],
            },
        },
    )
    store.complete_run(run_id)
    return run_id


def _seed_failed_run(store: EventStore, memory: MemoryManager, thread_id: str) -> str:
    run = store.create_run(thread_id)
    run_id = str(run["id"])
    store.update_run_status(run_id, RUN_STATUS_RUNNING)
    memory.append_event(thread_id, run_id, "plan_created", {"goal": "failed scratch task"})
    memory.append_event(thread_id, run_id, "token", {"text": "temporary failure output"})
    memory.append_event(thread_id, run_id, "error", {"error": "network timeout"})
    store.update_run_status(run_id, RUN_STATUS_FAILED, error="network timeout", completed=True)
    return run_id


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        print(
            "deprecated_wrapper=verify_short_term_memory_formation_v1.py; "
            "use=scripts/verify_memory_domain.py --case short_term"
        )
    event_store_path = Path(settings.agent_event_store_path).with_name(
        f"verify-short-term-memory-{uuid.uuid4().hex[:8]}.sqlite"
    )
    checkpoint_path = Path(settings.agent_checkpoint_path).with_name(
        f"verify-short-term-memory-{uuid.uuid4().hex[:8]}.sqlite"
    )
    event_store_path.parent.mkdir(parents=True, exist_ok=True)
    store, memory = _make_memory(event_store_path, checkpoint_path)

    thread_id = f"short-memory-{uuid.uuid4().hex[:8]}"
    user_id = f"user-short-memory-{uuid.uuid4().hex[:8]}"
    store.ensure_thread(thread_id, user_id=user_id)
    run_id = _seed_completed_run(store, memory, thread_id)
    summary = memory.summarize_run(thread_id, run_id, fallback_goal="fallback goal")
    memory.update_thread_summary(thread_id, run_id)
    validated = validate_payload_for_kind(EVENT_MEMORY_RUN_SUMMARY, summary)

    item_store = memory._item_store
    assert item_store is not None
    active_items = item_store.list_active(user_id=user_id, thread_id=thread_id)

    failed_thread = f"short-memory-failed-{uuid.uuid4().hex[:8]}"
    failed_user = f"user-short-memory-failed-{uuid.uuid4().hex[:8]}"
    store.ensure_thread(failed_thread, user_id=failed_user)
    failed_run_id = _seed_failed_run(store, memory, failed_thread)
    failed_summary = memory.summarize_run(failed_thread, failed_run_id, fallback_goal="failed scratch task")
    failed_items = item_store.list_active(user_id=failed_user, thread_id=failed_thread)

    encoded_summary = json.dumps(summary, ensure_ascii=False)
    seed_event_ids = [
        event_id
        for seed in summary.get("memory_candidates_seed") or []
        for event_id in seed.get("source_event_ids", [])
        if isinstance(seed, dict)
    ]
    checks = {
        "structured_summary_fields": all(
            key in summary
            for key in (
                "final_answer",
                "completed_actions",
                "decisions",
                "artifacts",
                "retrieval_sources",
                "memory_candidates_seed",
            )
        ),
        "final_answer_preferred": summary.get("final_answer") == "Implemented deterministic short-term memory seed builder.",
        "token_fallback_not_used": "noisy streamed token" not in str(summary.get("final_answer")),
        "seed_written": len(summary.get("memory_candidates_seed") or []) >= 2,
        "seed_source_traceable": all(isinstance(event_id, int) for event_id in seed_event_ids) and bool(seed_event_ids),
        "summary_sanitized": "secret-token" not in encoded_summary
        and "?token=" not in encoded_summary
        and "?cookie=" not in encoded_summary
        and not audit_payload_has_secret(summary),
        "event_contract_valid": validated.get("summary_type") == "run",
        "long_term_uses_seed": any(
            item.memory_type == MemoryType.TASK_SUMMARY
            and "deterministic short-term memory seed builder" in item.content
            for item in active_items
        ),
        "failed_summary_not_thread_eligible": failed_summary.get("eligible_for_thread") is False,
        "failed_run_no_fact_written": not any(item.memory_type == MemoryType.FACT for item in failed_items),
    }
    passed = all(checks.values())
    out = {
        "suite_name": "short_term_memory_formation_v1",
        "status": "PASS" if passed else "FAIL",
        "thread_id": thread_id,
        "run_id": run_id,
        "failed_run_id": failed_run_id,
        "checks": checks,
        "summary": summary,
        "failed_summary": failed_summary,
        "active_memory_items": [item.as_dict() for item in active_items],
    }
    out_path = ROOT / "artifacts/runtime/short-term-memory-formation-v1-summary.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"checks={json.dumps(checks, ensure_ascii=False)}")
    print(f"summary_json={out_path}")
    print(f"short_term_memory_formation_v1={'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
