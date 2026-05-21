from __future__ import annotations

from typing import Any

from copilot_agent.contracts.envelope import (
    EVENT_SCHEMA_VERSION,
    envelope_payload,
    payload_schema_version,
)

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
EVENT_RUN_FAILED_META = "run_failed_meta"
EVENT_RUN_CONSISTENCY_CHECKED = "run_consistency_checked"
EVENT_THREAD_CHECKPOINT_PURGED = "thread_checkpoint_purged"
EVENT_PLAN_CREATED = "plan_created"
EVENT_CANCEL_REQUESTED = "cancel_requested"
EVENT_CANCELLED = "cancelled"
EVENT_DONE = "done"
EVENT_ERROR = "error"
EVENT_MEMORY_RUN_SUMMARY = "memory_run_summary"
EVENT_MEMORY_THREAD_SUMMARY = "memory_thread_summary"
EVENT_CHECKPOINT_COMPACTED = "checkpoint_compacted"
EVENT_RETRIEVAL_COMPLETED = "retrieval_completed"
EVENT_CONTEXT_BUILT = "context_built"
EVENT_CREDENTIAL_BINDING_AUDIT = "credential_binding_audit"
EVENT_OUTPUT_GUARD_CHECKED = "output_guard_checked"

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
        EVENT_RUN_FAILED_META,
        EVENT_RUN_CONSISTENCY_CHECKED,
        EVENT_THREAD_CHECKPOINT_PURGED,
        EVENT_PLAN_CREATED,
        EVENT_CANCEL_REQUESTED,
        EVENT_CANCELLED,
        EVENT_DONE,
        EVENT_ERROR,
        EVENT_MEMORY_RUN_SUMMARY,
        EVENT_MEMORY_THREAD_SUMMARY,
        EVENT_CHECKPOINT_COMPACTED,
        EVENT_RETRIEVAL_COMPLETED,
        EVENT_CONTEXT_BUILT,
        EVENT_CREDENTIAL_BINDING_AUDIT,
        EVENT_OUTPUT_GUARD_CHECKED,
    }
)

__all__ = [
    "EVENT_SCHEMA_VERSION",
    "KNOWN_EVENT_TYPES",
    "envelope_payload",
    "payload_schema_version",
    "EVENT_OUTPUT_GUARD_CHECKED",
    "EVENT_RUN_FAILED_META",
    "EVENT_RUN_CONSISTENCY_CHECKED",
]
