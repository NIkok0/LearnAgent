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
    "build_tool_side_effect_payload",
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
        idempotency_key_present=bool(idempotency_key),
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
    error_type: str | None = None,
    attempt: int | None = None,
    max_attempts: int | None = None,
    idempotency_reused: bool = False,
) -> dict[str, Any]:
    tool_result = normalize_tool_result(
        result,
        success=success,
        error=error,
        duration_ms=duration_ms,
        sanitized_args=sanitized_args,
    )
    audit_result = tool_result.as_audit_dict()
    result_metadata = audit_result.get("metadata") if isinstance(audit_result.get("metadata"), dict) else {}
    resolved_retry_count = retry_count
    if resolved_retry_count is None and result_metadata.get("retry_count") is not None:
        try:
            resolved_retry_count = int(result_metadata.get("retry_count"))
        except (TypeError, ValueError):
            resolved_retry_count = retry_count
    resolved_attempt = attempt
    if resolved_attempt is None and result_metadata.get("attempt") is not None:
        try:
            resolved_attempt = int(result_metadata.get("attempt"))
        except (TypeError, ValueError):
            resolved_attempt = attempt
    resolved_max_attempts = max_attempts
    if resolved_max_attempts is None and result_metadata.get("max_attempts") is not None:
        try:
            resolved_max_attempts = int(result_metadata.get("max_attempts"))
        except (TypeError, ValueError):
            resolved_max_attempts = max_attempts
    resolved_idempotency_reused = bool(idempotency_reused or result_metadata.get("idempotency_reused"))
    payload = ToolEndPayload(
        name=name,
        call_id=call_id,
        result=ToolResultAuditEnvelope.model_validate(audit_result),
        duration_ms=duration_ms if duration_ms is not None else tool_result.duration_ms,
        success=tool_result.success,
        error=tool_result.error,
        error_type=error_type,
        sanitized_result=tool_result.sanitized_result,
        attempt=resolved_attempt,
        max_attempts=resolved_max_attempts,
        retry_count=resolved_retry_count,
        timeout_seconds=timeout_seconds,
        idempotency_key=idempotency_key,
        idempotency_key_present=bool(idempotency_key),
        idempotency_reused=resolved_idempotency_reused,
    )
    out = payload.model_dump(exclude_none=True)
    # Keep full audit key set in nested result (exclude_none would drop error=None).
    out["result"] = audit_result
    return out


def build_tool_side_effect_payload(
    *,
    tool_start_payload: dict[str, Any] | None,
    tool_end_payload: dict[str, Any] | None = None,
    reason: str | None = None,
    approval_status: str | None = None,
) -> dict[str, Any] | None:
    """Build a first-class audit ledger event for high-risk write tools."""
    start = tool_start_payload if isinstance(tool_start_payload, dict) else {}
    end = tool_end_payload if isinstance(tool_end_payload, dict) else {}
    tool_name = str(end.get("name") or start.get("name") or "")
    category = str(start.get("category") or "")
    risk_level = str(start.get("risk_level") or "")
    idempotency_key = str(end.get("idempotency_key") or start.get("idempotency_key") or "").strip() or None
    if tool_name != "http_post":
        return None
    if not ((category == "http" and risk_level == "high") or idempotency_key):
        return None

    arguments = start.get("arguments") if isinstance(start.get("arguments"), dict) else {}
    result = end.get("result") if isinstance(end.get("result"), dict) else {}
    result_data = result.get("data") if isinstance(result.get("data"), dict) else {}
    result_metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    method = str(result_data.get("method") or "POST").upper()
    path = str(result_data.get("path") or arguments.get("path") or "")
    status_code = _int_or_none(result_metadata.get("status_code") or result_data.get("status_code"))
    success = bool(end.get("success", False))
    idempotency_reused = bool(end.get("idempotency_reused") or result_metadata.get("idempotency_reused"))
    error_type = str(end.get("error_type") or "")
    error_text = str(end.get("error") or result.get("error") or "")
    resolved_reason = reason or _side_effect_reason(
        success=success,
        idempotency_reused=idempotency_reused,
        error_type=error_type,
        error_text=error_text,
    )
    status = _side_effect_status(
        success=success,
        idempotency_reused=idempotency_reused,
        error_type=error_type,
        error_text=error_text,
        reason=resolved_reason,
    )

    return {
        "tool_name": tool_name,
        "call_id": str(end.get("call_id") or start.get("call_id") or ""),
        "path": path,
        "method": method,
        "risk_level": risk_level,
        "requires_approval": bool(start.get("requires_approval", False)),
        "approval_status": approval_status or ("required" if start.get("requires_approval") else "not_required"),
        "side_effect_status": status,
        "success": success,
        "status_code": status_code,
        "idempotency_key": idempotency_key,
        "idempotency_reused": idempotency_reused,
        "compensatable": False,
        "reason": resolved_reason,
    }


def _side_effect_status(
    *,
    success: bool,
    idempotency_reused: bool,
    error_type: str,
    error_text: str,
    reason: str,
) -> str:
    if reason in {"approval_rejected", "policy_blocked"}:
        return "blocked"
    if idempotency_reused:
        return "reused"
    if success:
        return "confirmed"
    if _ambiguous_error(error_type=error_type, error_text=error_text):
        return "unknown"
    return "none"


def _side_effect_reason(*, success: bool, idempotency_reused: bool, error_type: str, error_text: str) -> str:
    if idempotency_reused:
        return "idempotency_reused"
    if success:
        return "http_post_success"
    if _ambiguous_error(error_type=error_type, error_text=error_text):
        return "ambiguous_write_result"
    return "tool_failed_before_confirmed_side_effect"


def _ambiguous_error(*, error_type: str, error_text: str) -> bool:
    text = f"{error_type} {error_text}".lower()
    markers = (
        "timeout",
        "timed out",
        "network",
        "connection",
        "connect",
        "readerror",
        "writeerror",
        "transport",
        "did not produce a result event",
    )
    return any(marker in text for marker in markers)


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
