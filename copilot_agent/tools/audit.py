from __future__ import annotations

from typing import Any

from copilot_agent.contracts.events.payloads import ToolEndPayload, ToolStartPayload
from copilot_agent.contracts.tool_data import ToolResultAuditEnvelope
from copilot_agent.contracts.tool_result import ToolResultModel
from copilot_agent.tools.sanitize import audit_payload_has_secret, sanitize_tool_payload

ToolResult = ToolResultModel

__all__ = [
    "ToolResult",
    "ToolResultModel",
    "audit_payload_has_secret",
    "build_tool_end_payload",
    "build_tool_start_payload",
    "normalize_tool_result",
]


def normalize_tool_result(
    result: Any,
    *,
    success: bool = True,
    error: str | None = None,
    duration_ms: int | None = None,
    sanitized_args: dict[str, Any] | None = None,
) -> ToolResultModel:
    """Wrap arbitrary tool output in the ToolResult audit contract."""
    return ToolResultModel.from_any(
        result,
        success=success,
        error=error,
        duration_ms=duration_ms,
        sanitized_args=sanitized_args,
    )


def build_tool_start_payload(
    *,
    name: str,
    call_id: str,
    category: str,
    risk_level: str,
    requires_approval: bool,
    arguments: Any,
    timeout_seconds: float | None = None,
    max_retries: int | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    sanitized_arguments = sanitize_tool_payload(arguments)
    payload = ToolStartPayload(
        name=name,
        call_id=call_id,
        category=category,
        risk_level=risk_level,
        requires_approval=requires_approval,
        arguments=sanitized_arguments if isinstance(sanitized_arguments, dict) else {},
        sanitized_args=sanitized_arguments if isinstance(sanitized_arguments, dict) else None,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        idempotency_key=idempotency_key,
    )
    return payload.model_dump(exclude_none=True)


def build_tool_end_payload(
    *,
    name: str,
    call_id: str,
    result: Any,
    duration_ms: int | None,
    success: bool = True,
    error: str | None = None,
    sanitized_args: dict[str, Any] | None = None,
    retry_count: int | None = None,
    timeout_seconds: float | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    tool_result = normalize_tool_result(
        result,
        success=success,
        error=error,
        duration_ms=duration_ms,
        sanitized_args=sanitized_args,
    )
    audit_result = tool_result.as_audit_dict()
    payload = ToolEndPayload(
        name=name,
        call_id=call_id,
        result=ToolResultAuditEnvelope.model_validate(audit_result),
        duration_ms=duration_ms if duration_ms is not None else tool_result.duration_ms,
        success=tool_result.success,
        error=tool_result.error,
        sanitized_result=tool_result.sanitized_result,
        retry_count=retry_count,
        timeout_seconds=timeout_seconds,
        idempotency_key=idempotency_key,
    )
    out = payload.model_dump(exclude_none=True)
    # Keep full audit key set in nested result (exclude_none would drop error=None).
    out["result"] = audit_result
    return out
