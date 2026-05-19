#!/usr/bin/env python
"""Verify Tool Audit v1 contract and sanitizer behavior."""

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

from copilot_agent.runtime.event_store import EventStore  # noqa: E402
from copilot_agent.runtime.timeline import TimelineProjector  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402
from copilot_agent.tools.audit import (  # noqa: E402
    audit_payload_has_secret,
    build_tool_end_payload,
    build_tool_start_payload,
    normalize_tool_result,
    sanitize_tool_payload,
)


def verify(event_store_path: Path, thread_id: str) -> dict[str, Any]:
    store = EventStore(str(event_store_path))
    run = store.create_run(thread_id)
    run_id = str(run["id"])

    raw_arguments = {
        "path": "/api/v1/auth/login",
        "cookie_header": "WMSESSIONID=abc123",
        "json_body": {"username": "demo", "password": "secret-value"},
    }
    raw_result = {
        "ok": True,
        "status_code": 200,
        "body": {"message": "ok"},
        "set-cookie": "WMSESSIONID=abc123; Path=/",
        "_raw_set_cookie_for_store_only": ["WMSESSIONID=abc123; Path=/"],
        "headers": {"Authorization": "Bearer secret-token"},
    }
    failed_result = {
        "ok": False,
        "status_code": 500,
        "error": "backend timeout",
        "body": "x" * 2500,
    }

    start_payload = build_tool_start_payload(
        name="http_post",
        call_id="tool-audit-1",
        category="http",
        risk_level="high",
        requires_approval=True,
        arguments=raw_arguments,
    )
    end_payload = build_tool_end_payload(
        name="http_post",
        call_id="tool-audit-1",
        result=raw_result,
        duration_ms=42,
    )
    failed_payload = build_tool_end_payload(
        name="http_get",
        call_id="tool-audit-2",
        result=failed_result,
        duration_ms=100,
    )

    store.append_event(thread_id, run_id, "run_created", {})
    store.append_event(thread_id, run_id, "run_started", {})
    store.append_event(thread_id, run_id, "tool_start", start_payload)
    store.append_event(thread_id, run_id, "tool_end", end_payload)
    store.append_event(
        thread_id,
        run_id,
        "tool_start",
        build_tool_start_payload(
            name="http_get",
            call_id="tool-audit-2",
            category="http",
            risk_level="medium",
            requires_approval=False,
            arguments={"path": "/actuator/health"},
        ),
    )
    store.append_event(thread_id, run_id, "tool_end", failed_payload)
    store.append_event(thread_id, run_id, "done", {})
    completed = store.complete_run(run_id)

    events = store.list_run_events(run_id)
    timeline = TimelineProjector().project_run(completed, events)
    tool_items = [item for item in timeline["items"] if item.get("kind") == "tool_call"]
    failed_tool = next((item for item in tool_items if item.get("call_id") == "tool-audit-2"), {})
    normalized_failure = normalize_tool_result(failed_result).as_dict()

    start_event = next(event for event in events if event["type"] == "tool_start")
    end_event = next(event for event in events if event["type"] == "tool_end")

    return {
        "event_store_path": str(event_store_path),
        "thread_id": thread_id,
        "run_id": run_id,
        "start_contract": {
            "has_call_id": bool(start_payload.get("call_id")),
            "has_metadata": all(key in start_payload for key in ("category", "risk_level", "requires_approval")),
            "arguments_sanitized": not audit_payload_has_secret(start_payload.get("arguments")),
            "cookie_redacted": start_payload["arguments"].get("cookie_header") == "***REDACTED***",
            "password_redacted": start_payload["arguments"].get("json_body", {}).get("password") == "***REDACTED***",
        },
        "end_contract": {
            "has_result_envelope": all(key in end_payload.get("result", {}) for key in ("success", "data", "error", "metadata", "sanitized")),
            "success": end_payload.get("success"),
            "duration_ms": end_payload.get("duration_ms"),
            "result_sanitized": not audit_payload_has_secret(end_payload.get("result")),
            "raw_cookie_removed": "_raw_set_cookie_for_store_only" not in json.dumps(end_payload, ensure_ascii=False),
        },
        "failure_contract": {
            "success": failed_payload.get("success"),
            "error": failed_payload.get("error"),
            "body_truncated": len(normalized_failure.get("data", {}).get("body", "")) < 2200,
        },
        "timeline": {
            "tool_count": len(tool_items),
            "failed_tool_success": failed_tool.get("success"),
            "warning_codes": [warning["code"] for warning in timeline["warnings"]],
        },
        "persisted": {
            "start_has_secret": audit_payload_has_secret(start_event.get("payload")),
            "end_has_secret": audit_payload_has_secret(end_event.get("payload")),
        },
        "direct_sanitizer": sanitize_tool_payload({"token": "abc", "nested": {"secret": "s"}}),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify LearnAgent Tool Audit v1.")
    parser.add_argument(
        "--event-store-path",
        default=settings.agent_event_store_path,
        help="SQLite event store path.",
    )
    parser.add_argument(
        "--thread-id",
        default=f"tool-audit-{uuid.uuid4().hex[:8]}",
        help="Thread id for verification.",
    )
    parser.add_argument(
        "--summary-json",
        default=str(ROOT / "artifacts/runtime/tool-audit-v1-summary.json"),
        help="Path to write structured verification summary JSON.",
    )
    args = parser.parse_args()

    event_store_path = Path(args.event_store_path).resolve()
    event_store_path.parent.mkdir(parents=True, exist_ok=True)
    summary = verify(event_store_path, args.thread_id)

    ok_start = all(summary["start_contract"].values())
    ok_end = all(summary["end_contract"].values())
    ok_failure = (
        summary["failure_contract"]["success"] is False
        and summary["failure_contract"]["error"] == "backend timeout"
        and summary["failure_contract"]["body_truncated"]
    )
    ok_timeline = (
        summary["timeline"]["tool_count"] == 2
        and summary["timeline"]["failed_tool_success"] is False
        and "tool_failed" in summary["timeline"]["warning_codes"]
    )
    ok_persisted = not summary["persisted"]["start_has_secret"] and not summary["persisted"]["end_has_secret"]
    ok_direct = summary["direct_sanitizer"]["token"] == "***REDACTED***" and summary["direct_sanitizer"]["nested"]["secret"] == "***REDACTED***"
    passed = ok_start and ok_end and ok_failure and ok_timeline and ok_persisted and ok_direct
    summary["tool_audit_v1"] = "PASS" if passed else "FAIL"

    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"event_store_path={summary['event_store_path']}")
    print(f"start_contract={ok_start}")
    print(f"end_contract={ok_end}")
    print(f"failure_contract={ok_failure}")
    print(f"timeline_contract={ok_timeline}")
    print(f"persisted_sanitized={ok_persisted}")
    print(f"summary_json={summary_path}")
    print(f"tool_audit_v1={summary['tool_audit_v1']}")

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
