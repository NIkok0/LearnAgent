#!/usr/bin/env python
"""Verify RuntimeEvent round-trip against EventStore payloads."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.contracts.base import RuntimeEvent  # noqa: E402
from copilot_agent.contracts.events.retrieval import build_retrieval_completed_payload  # noqa: E402
from copilot_agent.rag.schema import DocChunk  # noqa: E402
from copilot_agent.runtime.event_schema import KNOWN_EVENT_TYPES, payload_schema_version  # noqa: E402
from copilot_agent.runtime.event_store import EventStore, RUN_STATUS_RUNNING  # noqa: E402
from copilot_agent.tools.audit import build_tool_end_payload, build_tool_start_payload  # noqa: E402


def verify(event_store_path: Path, thread_id: str) -> dict[str, Any]:
    store = EventStore(str(event_store_path))
    run = store.create_run(thread_id)
    run_id = str(run["id"])
    store.update_run_status(run_id, RUN_STATUS_RUNNING)

    samples: list[tuple[str, dict[str, Any]]] = [
        ("token", {"text": "hello"}),
        (
            "tool_start",
            build_tool_start_payload(
                name="http_get",
                call_id="c1",
                category="http",
                risk_level="medium",
                requires_approval=False,
                arguments={"path": "/actuator/health"},
            ),
        ),
        (
            "tool_end",
            build_tool_end_payload(
                name="http_get",
                call_id="c1",
                result={"success": True, "data": {"status_code": 200}},
                duration_ms=10,
            ),
        ),
        ("approval_required", {"required": True, "reason": "dangerous_tool", "message": "confirm"}),
        (
            "retrieval_completed",
            build_retrieval_completed_payload(
                "Redis Stream key",
                [DocChunk(source="DEPLOY-SERVER.md", start_line=10, text="# Redis\nDefault key")],
                excerpt_chars=64,
            ),
        ),
        (
            "memory_run_summary",
            {
                "summary_type": "run",
                "goal": "contract test",
                "outcome": "completed",
                "tools_used": ["search_docs"],
                "tool_details": [{"name": "search_docs", "category": "rag", "risk_level": "low"}],
                "key_outputs": ["ok"],
                "errors": [],
                "source_event_ids": [1],
                "eligible_for_thread": True,
            },
        ),
        ("done", {"assistant_message": {"content": "done"}}),
    ]

    round_trip_ok = True
    schema_ok = True
    kinds_ok = True
    errors: list[str] = []

    for kind, payload in samples:
        if kind not in KNOWN_EVENT_TYPES:
            kinds_ok = False
            errors.append(f"unknown kind in sample: {kind}")
            continue
        original = RuntimeEvent.from_payload(kind, payload, thread_id=thread_id, run_id=run_id)
        stored = original.to_store_payload()
        if payload_schema_version(stored) != 1:
            schema_ok = False
            errors.append(f"{kind}: schema_version != 1")
        row = store.append_event(thread_id, run_id, kind, stored)
        loaded = RuntimeEvent.from_stored(
            kind=str(row.get("type", kind)),
            payload=row.get("payload") if isinstance(row.get("payload"), dict) else {},
            thread_id=thread_id,
            run_id=run_id,
        )
        if loaded.to_store_payload() != stored:
            round_trip_ok = False
            errors.append(f"{kind}: store round-trip mismatch")

    store.append_event(thread_id, run_id, "done", {"assistant_message": {"content": "x"}})
    store.complete_run(run_id)

    all_rows = store.list_run_events(run_id)
    from copilot_agent.contracts.validate import validate_event_rows  # noqa: PLC0415

    row_validation = validate_event_rows(all_rows)
    model_validate_ok = bool(row_validation.get("model_validate_ok"))
    passed = round_trip_ok and schema_ok and kinds_ok and model_validate_ok
    return {
        "event_store_path": str(event_store_path),
        "thread_id": thread_id,
        "run_id": run_id,
        "sample_count": len(samples),
        "round_trip_ok": round_trip_ok,
        "schema_ok": schema_ok,
        "kinds_ok": kinds_ok,
        "model_validate_ok": model_validate_ok,
        "validated_event_count": row_validation.get("validated_count"),
        "errors": errors + list(row_validation.get("errors") or []),
        "checks": {
            "round_trip_ok": round_trip_ok,
            "schema_ok": schema_ok,
            "kinds_ok": kinds_ok,
            "model_validate_ok": model_validate_ok,
            "contract_schema_ok": passed,
        },
        "contract_events": "PASS" if passed else "FAIL",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify RuntimeEvent contract round-trip.")
    parser.add_argument(
        "--event-store-path",
        default=str(ROOT / "storage/verify-contract-events.sqlite"),
    )
    parser.add_argument("--thread-id", default=f"contract-{uuid.uuid4().hex[:8]}")
    parser.add_argument(
        "--summary-json",
        default=str(ROOT / "artifacts/runtime/contract-events-summary.json"),
    )
    args = parser.parse_args()

    summary = verify(Path(args.event_store_path).resolve(), args.thread_id)
    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"round_trip_ok={summary['round_trip_ok']}")
    print(f"schema_ok={summary['schema_ok']}")
    print(f"model_validate_ok={summary['model_validate_ok']}")
    print(f"contract_schema_ok={(summary.get('checks') or {}).get('contract_schema_ok')}")
    print(f"summary_json={summary_path}")
    print(f"contract_events={summary['contract_events']}")
    if summary["errors"]:
        print(f"errors={summary['errors']}")
    return 0 if summary["contract_events"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
