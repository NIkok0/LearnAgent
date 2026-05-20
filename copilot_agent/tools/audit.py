from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from langchain_core.messages import BaseMessage


SENSITIVE_KEY_PARTS = (
    "authorization",
    "cookie",
    "password",
    "secret",
    "set-cookie",
    "token",
)
RAW_SECRET_KEYS = {"_raw_set_cookie_for_store_only"}
MAX_STRING_LENGTH = 2000


@dataclass(frozen=True)
class ToolResult:
    """Stable LearnAgent tool result envelope for audit/timeline use."""

    success: bool
    data: Any = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    sanitized: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def sanitize_tool_payload(value: Any, *, max_string_length: int = MAX_STRING_LENGTH) -> Any:
    """Redact secrets and bound payload size before writing tool audit events."""

    if isinstance(value, BaseMessage):
        return sanitize_tool_payload(
            {
                "type": value.__class__.__name__,
                "content": getattr(value, "content", ""),
                "name": getattr(value, "name", None),
                "tool_call_id": getattr(value, "tool_call_id", None),
                "status": getattr(value, "status", None),
            },
            max_string_length=max_string_length,
        )
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            key_lower = key_text.lower()
            if key_lower in RAW_SECRET_KEYS:
                continue
            if any(part in key_lower for part in SENSITIVE_KEY_PARTS):
                out[key_text] = "***REDACTED***"
                continue
            out[key_text] = sanitize_tool_payload(item, max_string_length=max_string_length)
        return out
    if isinstance(value, list):
        return [sanitize_tool_payload(item, max_string_length=max_string_length) for item in value]
    if isinstance(value, tuple):
        return [sanitize_tool_payload(item, max_string_length=max_string_length) for item in value]
    if isinstance(value, str):
        if len(value) <= max_string_length:
            return value
        return f"{value[:max_string_length]}...(truncated)"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def normalize_tool_result(result: Any, *, success: bool = True, error: str | None = None) -> ToolResult:
    """Wrap arbitrary tool output in the ToolResult audit contract."""

    sanitized = sanitize_tool_payload(result)
    if isinstance(sanitized, dict):
        metadata: dict[str, Any] = {}
        if "status_code" in sanitized:
            metadata["status_code"] = sanitized.get("status_code")
        if "ok" in sanitized:
            success = bool(sanitized.get("ok"))
        if error is None and not success:
            raw_error = sanitized.get("error")
            error = str(raw_error) if raw_error else None
        return ToolResult(success=success, data=sanitized, error=error, metadata=metadata)
    return ToolResult(success=success, data=sanitized, error=error)


def build_tool_start_payload(
    *,
    name: str,
    call_id: str,
    category: str,
    risk_level: str,
    requires_approval: bool,
    arguments: Any,
) -> dict[str, Any]:
    return {
        "name": name,
        "call_id": call_id,
        "category": category,
        "risk_level": risk_level,
        "requires_approval": requires_approval,
        "arguments": sanitize_tool_payload(arguments),
    }


def build_tool_end_payload(
    *,
    name: str,
    call_id: str,
    result: Any,
    duration_ms: int | None,
    success: bool = True,
    error: str | None = None,
) -> dict[str, Any]:
    tool_result = normalize_tool_result(result, success=success, error=error)
    return {
        "name": name,
        "call_id": call_id,
        "result": tool_result.as_dict(),
        "duration_ms": duration_ms,
        "success": tool_result.success,
        "error": tool_result.error,
    }


def audit_payload_has_secret(value: Any) -> bool:
    """Best-effort assertion helper for verification scripts."""

    if isinstance(value, dict):
        for key, item in value.items():
            key_lower = str(key).lower()
            if key_lower in RAW_SECRET_KEYS:
                return True
            if any(part in key_lower for part in SENSITIVE_KEY_PARTS) and item != "***REDACTED***":
                return True
            if audit_payload_has_secret(item):
                return True
        return False
    if isinstance(value, list):
        return any(audit_payload_has_secret(item) for item in value)
    if isinstance(value, str):
        lower = value.lower()
        return "wmsessionid=" in lower or "set-cookie:" in lower
    return False
