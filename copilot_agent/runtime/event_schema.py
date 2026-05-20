from __future__ import annotations

from typing import Any

EVENT_SCHEMA_VERSION = 1

EVENT_RUN_CREATED = "run_created"
EVENT_RUN_STARTED = "run_started"
EVENT_TOKEN = "token"
EVENT_ASSISTANT_STATE = "assistant_state"
EVENT_TOOL_START = "tool_start"
EVENT_TOOL_END = "tool_end"
EVENT_APPROVAL_REQUIRED = "approval_required"
EVENT_APPROVAL_RESOLVED = "approval_resolved"
EVENT_RUN_CHECKPOINT_META = "run_checkpoint_meta"
EVENT_RUN_COMPLETED_META = "run_completed_meta"
EVENT_THREAD_CHECKPOINT_PURGED = "thread_checkpoint_purged"
EVENT_PLAN_CREATED = "plan_created"
EVENT_CANCEL_REQUESTED = "cancel_requested"
EVENT_CANCELLED = "cancelled"
EVENT_DONE = "done"
EVENT_ERROR = "error"
EVENT_MEMORY_RUN_SUMMARY = "memory_run_summary"
EVENT_MEMORY_THREAD_SUMMARY = "memory_thread_summary"

KNOWN_EVENT_TYPES = frozenset(
    {
        EVENT_RUN_CREATED,
        EVENT_RUN_STARTED,
        EVENT_TOKEN,
        EVENT_ASSISTANT_STATE,
        EVENT_TOOL_START,
        EVENT_TOOL_END,
        EVENT_APPROVAL_REQUIRED,
        EVENT_APPROVAL_RESOLVED,
        EVENT_RUN_CHECKPOINT_META,
        EVENT_RUN_COMPLETED_META,
        EVENT_THREAD_CHECKPOINT_PURGED,
        EVENT_PLAN_CREATED,
        EVENT_CANCEL_REQUESTED,
        EVENT_CANCELLED,
        EVENT_DONE,
        EVENT_ERROR,
        EVENT_MEMORY_RUN_SUMMARY,
        EVENT_MEMORY_THREAD_SUMMARY,
    }
)


def envelope_payload(event_type: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    data = dict(payload or {})
    if "schema_version" not in data:
        data["schema_version"] = EVENT_SCHEMA_VERSION
    return data


def payload_schema_version(payload: dict[str, Any] | None) -> int:
    if not isinstance(payload, dict):
        return 0
    raw = payload.get("schema_version")
    try:
        return int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return 0
