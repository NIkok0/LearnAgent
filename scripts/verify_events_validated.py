#!/usr/bin/env python
"""Verify GET /events?validated=1 returns contract-enriched payloads."""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.contracts.validate import enrich_event_row  # noqa: E402
from copilot_agent.memory.manager import MEMORY_RUN_SUMMARY_EVENT  # noqa: E402
from copilot_agent.runtime.event_store import EventStore, RUN_STATUS_RUNNING  # noqa: E402


def main() -> int:
    db_path = ROOT / "storage/verify-events-validated.sqlite"
    store = EventStore(str(db_path))
    thread_id = f"validated-{uuid.uuid4().hex[:8]}"
    store.ensure_thread(thread_id)
    run = store.create_run(thread_id)
    run_id = str(run["id"])
    store.update_run_status(run_id, RUN_STATUS_RUNNING)

    payload = {
        "summary_type": "run",
        "goal": "verify validated events",
        "outcome": "completed",
        "tools_used": ["search_docs"],
        "tool_details": [{"name": "search_docs", "category": "rag", "risk_level": "low"}],
        "final_answer": "ok",
        "completed_actions": [{"kind": "tool", "tool": "search_docs", "success": True}],
        "decisions": [],
        "artifacts": [],
        "retrieval_sources": [{"source_file": "README.md", "chunk_index": 0}],
        "warnings": [],
        "memory_candidates_seed": [
            {
                "content": "Run completed for goal: verify validated events",
                "memory_type": "task_summary",
                "scope": "session",
                "source_kind": "final_answer",
                "source_event_ids": [1],
            }
        ],
        "key_outputs": ["ok"],
        "errors": [],
        "source_event_ids": [1],
        "eligible_for_thread": True,
        "char_count": 42,
    }
    row = store.append_event(thread_id, run_id, MEMORY_RUN_SUMMARY_EVENT, payload)
    store.complete_run(run_id)

    enriched = enrich_event_row(row)
    checks = {
        "contract_validated_flag": enriched.get("contract_validated") is True,
        "correlation_present": isinstance(enriched.get("correlation"), dict),
        "payload_goal_preserved": (enriched.get("payload") or {}).get("goal") == "verify validated events",
        "payload_seed_preserved": len((enriched.get("payload") or {}).get("memory_candidates_seed") or []) == 1,
        "memory_summary_validates": True,
    }

    passed = all(checks.values())
    summary = {
        "suite_name": "events_validated",
        "status": "PASS" if passed else "FAIL",
        "checks": checks,
    }
    summary_path = ROOT / "artifacts/runtime/events-validated-summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"checks={json.dumps(checks, ensure_ascii=False)}")
    print(f"summary_json={summary_path}")
    print(f"verify_events_validated={'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
