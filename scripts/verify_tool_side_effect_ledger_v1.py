#!/usr/bin/env python
"""Verify high-risk write tool side-effect ledger events."""

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

from copilot_agent.contracts.validate import validate_event_rows  # noqa: E402
from copilot_agent.runtime.event_schema import EVENT_TOOL_SIDE_EFFECT_RECORDED  # noqa: E402
from copilot_agent.runtime.event_store import EventStore, RUN_STATUS_RUNNING  # noqa: E402
from copilot_agent.runtime.timeline import TimelineProjector  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402
from copilot_agent.tools.audit import (  # noqa: E402
    audit_payload_has_secret,
    build_tool_end_payload,
    build_tool_side_effect_payload,
    build_tool_start_payload,
)


def _append_tool_pair(
    store: EventStore,
    *,
    thread_id: str,
    run_id: str,
    start_payload: dict[str, Any],
    end_payload: dict[str, Any],
) -> dict[str, Any] | None:
    store.append_event(thread_id, run_id, "tool_start", start_payload)
    store.append_event(thread_id, run_id, "tool_end", end_payload)
    side_effect = build_tool_side_effect_payload(
        tool_start_payload=start_payload,
        tool_end_payload=end_payload,
    )
    if side_effect is not None:
        store.append_event(thread_id, run_id, EVENT_TOOL_SIDE_EFFECT_RECORDED, side_effect)
    return side_effect


def verify(event_store_path: Path, thread_id: str) -> dict[str, Any]:
    store = EventStore(str(event_store_path))
    run = store.create_run(thread_id)
    run_id = str(run["id"])
    store.update_run_status(run_id, RUN_STATUS_RUNNING)
    store.append_event(thread_id, run_id, "run_created", {})
    store.append_event(thread_id, run_id, "run_started", {})

    confirmed_start = build_tool_start_payload(
        name="http_post",
        call_id="post-confirmed",
        category="http",
        risk_level="high",
        requires_approval=True,
        arguments={
            "path": "/api/jobs?token=secret-token&safe=1",
            "json_body": {"title": "demo", "token": "secret-token"},
            "cookie_header": "WMSESSIONID=secret-cookie",
        },
        idempotency_key="idem-confirmed",
    )
    confirmed_end = build_tool_end_payload(
        name="http_post",
        call_id="post-confirmed",
        result={"ok": True, "status_code": 201, "path": "/api/jobs?token=secret-token&safe=1", "method": "POST", "body": {"id": "j1"}},
        duration_ms=31,
        idempotency_key="idem-confirmed",
    )
    confirmed = _append_tool_pair(
        store,
        thread_id=thread_id,
        run_id=run_id,
        start_payload=confirmed_start,
        end_payload=confirmed_end,
    )

    reused_start = build_tool_start_payload(
        name="http_post",
        call_id="post-reused",
        category="http",
        risk_level="high",
        requires_approval=True,
        arguments={"path": "/api/jobs", "json_body": {"title": "demo"}},
        idempotency_key="idem-reused",
    )
    reused_end = build_tool_end_payload(
        name="http_post",
        call_id="post-reused",
        result={
            "ok": True,
            "status_code": 201,
            "path": "/api/jobs",
            "method": "POST",
            "metadata": {"idempotency_reused": True, "reused_from_event_id": 3},
        },
        duration_ms=1,
        idempotency_key="idem-reused",
    )
    reused = _append_tool_pair(
        store,
        thread_id=thread_id,
        run_id=run_id,
        start_payload=reused_start,
        end_payload=reused_end,
    )

    none_start = build_tool_start_payload(
        name="http_post",
        call_id="post-none",
        category="http",
        risk_level="high",
        requires_approval=True,
        arguments={"path": "/api/jobs", "json_body": {"bad": True}},
        idempotency_key="idem-none",
    )
    none_end = build_tool_end_payload(
        name="http_post",
        call_id="post-none",
        result={"ok": False, "status_code": 400, "path": "/api/jobs", "error": "validation failed"},
        duration_ms=11,
        success=False,
        error="validation failed",
        error_type="ToolExecutionError",
        idempotency_key="idem-none",
    )
    none = _append_tool_pair(
        store,
        thread_id=thread_id,
        run_id=run_id,
        start_payload=none_start,
        end_payload=none_end,
    )

    unknown_start = build_tool_start_payload(
        name="http_post",
        call_id="post-unknown",
        category="http",
        risk_level="high",
        requires_approval=True,
        arguments={"path": "/api/jobs", "json_body": {"maybe": True}},
        idempotency_key="idem-unknown",
    )
    unknown_end = build_tool_end_payload(
        name="http_post",
        call_id="post-unknown",
        result={},
        duration_ms=120000,
        success=False,
        error="tool http_post timed out after 120.0s",
        error_type="ToolExecutionTimeout",
        idempotency_key="idem-unknown",
    )
    unknown = _append_tool_pair(
        store,
        thread_id=thread_id,
        run_id=run_id,
        start_payload=unknown_start,
        end_payload=unknown_end,
    )

    get_start = build_tool_start_payload(
        name="http_get",
        call_id="get-excluded",
        category="http",
        risk_level="medium",
        requires_approval=False,
        arguments={"path": "/api/jobs"},
    )
    get_end = build_tool_end_payload(
        name="http_get",
        call_id="get-excluded",
        result={"ok": True, "status_code": 200, "path": "/api/jobs", "method": "GET"},
        duration_ms=4,
    )
    excluded_get = _append_tool_pair(
        store,
        thread_id=thread_id,
        run_id=run_id,
        start_payload=get_start,
        end_payload=get_end,
    )

    search_start = build_tool_start_payload(
        name="search_docs",
        call_id="search-excluded",
        category="memory",
        risk_level="low",
        requires_approval=False,
        arguments={"query": "deployment"},
    )
    search_end = build_tool_end_payload(
        name="search_docs",
        call_id="search-excluded",
        result={"success": True, "sources": ["DEPLOY-SERVER.md"]},
        duration_ms=3,
    )
    excluded_search = _append_tool_pair(
        store,
        thread_id=thread_id,
        run_id=run_id,
        start_payload=search_start,
        end_payload=search_end,
    )

    blocked_start = build_tool_start_payload(
        name="http_post",
        call_id="post-blocked",
        category="http",
        risk_level="high",
        requires_approval=True,
        arguments={"path": "/api/jobs", "json_body": {"title": "blocked"}},
        idempotency_key="idem-blocked",
    )
    blocked_end = build_tool_end_payload(
        name="http_post",
        call_id="post-blocked",
        result={"success": False, "error": "approval rejected"},
        duration_ms=None,
        success=False,
        error="approval rejected",
        idempotency_key="idem-blocked",
    )
    blocked = build_tool_side_effect_payload(
        tool_start_payload=blocked_start,
        tool_end_payload=blocked_end,
        reason="approval_rejected",
        approval_status="rejected",
    )
    if blocked is not None:
        store.append_event(thread_id, run_id, EVENT_TOOL_SIDE_EFFECT_RECORDED, blocked)

    store.append_event(thread_id, run_id, "done", {})
    completed = store.complete_run(run_id)
    events = store.list_run_events(run_id)
    ledger_events = [event for event in events if event.get("type") == EVENT_TOOL_SIDE_EFFECT_RECORDED]
    timeline = TimelineProjector().project_run(completed, events)
    side_effect_items = [item for item in timeline["items"] if item.get("kind") == "side_effect"]
    side_effect_statuses = {
        str((event.get("payload") or {}).get("call_id")): (event.get("payload") or {}).get("side_effect_status")
        for event in ledger_events
    }
    approval_statuses = {
        str((event.get("payload") or {}).get("call_id")): (event.get("payload") or {}).get("approval_status")
        for event in ledger_events
    }
    paths = {
        str((event.get("payload") or {}).get("call_id")): (event.get("payload") or {}).get("path")
        for event in ledger_events
    }
    encoded_ledger = json.dumps([event.get("payload") for event in ledger_events], ensure_ascii=False)
    row_validation = validate_event_rows(events)

    return {
        "event_store_path": str(event_store_path),
        "thread_id": thread_id,
        "run_id": run_id,
        "direct_payloads": {
            "confirmed": confirmed,
            "reused": reused,
            "none": none,
            "unknown": unknown,
            "blocked": blocked,
            "excluded_get": excluded_get,
            "excluded_search": excluded_search,
        },
        "ledger_count": len(ledger_events),
        "side_effect_statuses": side_effect_statuses,
        "approval_statuses": approval_statuses,
        "paths": paths,
        "timeline": {
            "side_effect_count": len(side_effect_items),
            "warning_codes": [warning["code"] for warning in timeline["warnings"]],
            "debugger_side_effect_count": (timeline.get("debugger") or {}).get("side_effect_count"),
            "debugger_unknown_side_effect_count": (timeline.get("debugger") or {}).get(
                "unknown_side_effect_count"
            ),
        },
        "audit_safety": {
            "ledger_has_secret": audit_payload_has_secret([event.get("payload") for event in ledger_events]),
            "ledger_mentions_json_body": "json_body" in encoded_ledger,
            "ledger_mentions_cookie": "WMSESSIONID=" in encoded_ledger or "cookie_header" in encoded_ledger,
            "ledger_mentions_token_query": "secret-token" in encoded_ledger or "?token=" in encoded_ledger,
            "ledger_mentions_raw_body": "raw response" in encoded_ledger.lower(),
        },
        "validation": row_validation,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify LearnAgent Tool Side Effect Ledger v1.")
    parser.add_argument(
        "--event-store-path",
        default=str(ROOT / "storage/verify-tool-side-effect-ledger-events.sqlite"),
    )
    parser.add_argument("--thread-id", default=f"side-effect-{uuid.uuid4().hex[:8]}")
    parser.add_argument(
        "--summary-json",
        default=str(ROOT / "artifacts/runtime/tool-side-effect-ledger-v1-summary.json"),
    )
    args = parser.parse_args()

    event_store_path = Path(args.event_store_path).resolve()
    event_store_path.parent.mkdir(parents=True, exist_ok=True)
    summary = verify(event_store_path, args.thread_id)
    statuses = summary["side_effect_statuses"]
    approval_statuses = summary["approval_statuses"]
    paths = summary["paths"]
    timeline = summary["timeline"]
    audit = summary["audit_safety"]
    validation = summary["validation"]
    checks = {
        "confirmed_recorded": statuses.get("post-confirmed") == "confirmed",
        "reused_recorded": statuses.get("post-reused") == "reused",
        "none_recorded": statuses.get("post-none") == "none",
        "unknown_recorded": statuses.get("post-unknown") == "unknown",
        "blocked_recorded": statuses.get("post-blocked") == "blocked",
        "approval_statuses": approval_statuses.get("post-confirmed") == "pending"
        and approval_statuses.get("post-blocked") == "rejected",
        "path_canonicalized": paths.get("post-confirmed") == "/api/jobs",
        "read_tools_excluded": summary["direct_payloads"]["excluded_get"] is None
        and summary["direct_payloads"]["excluded_search"] is None,
        "ledger_count": summary["ledger_count"] == 5,
        "timeline_projected": timeline["side_effect_count"] == 5
        and timeline["debugger_side_effect_count"] == 5,
        "unknown_warned": "side_effect_unknown" in timeline["warning_codes"]
        and timeline["debugger_unknown_side_effect_count"] == 1,
        "payload_sanitized": not audit["ledger_has_secret"]
        and not audit["ledger_mentions_json_body"]
        and not audit["ledger_mentions_cookie"]
        and not audit["ledger_mentions_token_query"]
        and not audit["ledger_mentions_raw_body"],
        "contract_schema_ok": bool(validation.get("model_validate_ok")),
    }
    passed = all(checks.values())
    summary["checks"] = checks
    summary["tool_side_effect_ledger_v1"] = "PASS" if passed else "FAIL"

    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    for key, value in checks.items():
        print(f"{key}={value}")
    print(f"summary_json={summary_path}")
    print(f"tool_side_effect_ledger_v1={summary['tool_side_effect_ledger_v1']}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
