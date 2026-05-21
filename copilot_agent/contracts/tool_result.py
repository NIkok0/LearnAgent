from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from copilot_agent.contracts.tool_data import HttpToolData, SearchDocsToolData, ToolResultAuditEnvelope
from copilot_agent.tools.sanitize import sanitize_tool_payload

# Keys kept for LLM-facing data but stripped from audit sanitized_result duplicates.
_HTTP_INTERNAL_KEYS = frozenset({"ok", "_raw_set_cookie_for_store_only"})

__all__ = ["ToolResultAuditEnvelope", "ToolResultModel"]


class ToolResultModel(BaseModel):
    """Unified tool result envelope for LLM, audit, and timeline."""

    success: bool
    data: dict[str, Any] | list[Any] | str | None = None
    error: str | None = None
    duration_ms: int | None = None
    sanitized_args: dict[str, Any] | None = None
    sanitized_result: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    sanitized: bool = True

    def as_audit_dict(self) -> dict[str, Any]:
        """Shape stored under tool_end.payload.result (stable keys for audit contract)."""
        return ToolResultAuditEnvelope.model_validate(
            {
                "success": self.success,
                "data": self.data,
                "error": self.error,
                "metadata": self.metadata,
                "sanitized": self.sanitized,
                "sanitized_args": self.sanitized_args,
                "sanitized_result": self.sanitized_result,
            }
        ).model_dump(exclude_unset=False)

    def to_audit_envelope(self) -> ToolResultAuditEnvelope:
        return ToolResultAuditEnvelope.model_validate(self.as_audit_dict())

    def to_llm_dict(self) -> dict[str, Any]:
        """Return value for LangChain ToolNode / LLM tool message content."""
        out: dict[str, Any] = {"success": self.success}
        if self.error:
            out["error"] = self.error
        if self.data is not None:
            out["data"] = self.data
        if self.metadata:
            out["metadata"] = self.metadata
        return out

    @classmethod
    def from_http_legacy(
        cls,
        raw: dict[str, Any],
        *,
        duration_ms: int | None = None,
        sanitized_args: dict[str, Any] | None = None,
    ) -> ToolResultModel:
        """Map WatermarkHttpTools {ok, status_code, body, ...} to contract."""
        sanitized = sanitize_tool_payload(raw)
        if not isinstance(sanitized, dict):
            sanitized = {"value": sanitized}

        success = bool(raw.get("success", raw.get("ok", False)))
        error = raw.get("error")
        error_text = str(error) if error is not None and not success else None

        metadata: dict[str, Any] = {}
        if "status_code" in sanitized:
            metadata["status_code"] = sanitized.get("status_code")

        data_fields: dict[str, Any] = {}
        for key, value in sanitized.items():
            if key in _HTTP_INTERNAL_KEYS:
                continue
            if key in ("success", "error"):
                continue
            data_fields[key] = value

        http_data = HttpToolData.model_validate(data_fields)

        return cls(
            success=success,
            data=http_data.model_dump(exclude_none=True) or None,
            error=error_text,
            duration_ms=duration_ms,
            sanitized_args=sanitized_args,
            sanitized_result=dict(sanitized),
            metadata=metadata,
        )

    @classmethod
    def from_search_docs(
        cls,
        raw: dict[str, Any],
        *,
        duration_ms: int | None = None,
    ) -> ToolResultModel:
        sanitized = sanitize_tool_payload(raw)
        if not isinstance(sanitized, dict):
            sanitized = {"value": sanitized}
        typed = SearchDocsToolData.model_validate(
            {
                "excerpts_markdown": sanitized.get("excerpts_markdown"),
                "sources": sanitized.get("sources") or [],
                "suggested_api_paths": sanitized.get("suggested_api_paths") or [],
            }
        )
        metadata = {
            "sources": typed.sources,
            "excerpt_chars": len(str(typed.excerpts_markdown or "")),
            "suggested_api_paths": [item.model_dump(exclude_none=True) for item in typed.suggested_api_paths],
        }
        return cls(
            success=True,
            data=typed.model_dump(exclude_none=True),
            duration_ms=duration_ms,
            sanitized_result=dict(sanitized),
            metadata=metadata,
        )

    @classmethod
    def from_any(
        cls,
        value: Any,
        *,
        success: bool = True,
        error: str | None = None,
        duration_ms: int | None = None,
        sanitized_args: dict[str, Any] | None = None,
    ) -> ToolResultModel:
        """Normalize tool output from handlers, legacy dicts, or prior envelopes."""
        if isinstance(value, cls):
            updates: dict[str, Any] = {}
            if duration_ms is not None:
                updates["duration_ms"] = duration_ms
            if sanitized_args is not None:
                updates["sanitized_args"] = sanitized_args
            if error is not None:
                updates["error"] = error
                updates["success"] = False
            if updates:
                return value.model_copy(update=updates)
            if not success and value.success:
                return value.model_copy(update={"success": False, "error": error})
            return value

        if isinstance(value, dict):
            if "success" in value or "ok" in value:
                if "excerpts_markdown" in value or (
                    "sources" in value and "status_code" not in value and "body" not in value
                ):
                    model = cls.from_search_docs(value, duration_ms=duration_ms)
                else:
                    model = cls.from_http_legacy(value, duration_ms=duration_ms, sanitized_args=sanitized_args)
                if error is not None:
                    return model.model_copy(update={"success": False, "error": error})
                return model
            sanitized = sanitize_tool_payload(value)
            if isinstance(sanitized, dict):
                return cls(
                    success=success,
                    data=sanitized,
                    error=error,
                    duration_ms=duration_ms,
                    sanitized_args=sanitized_args,
                    sanitized_result=dict(sanitized),
                )

        sanitized = sanitize_tool_payload(value)
        return cls(
            success=success,
            data=sanitized if isinstance(sanitized, dict) else {"value": sanitized},
            error=error,
            duration_ms=duration_ms,
            sanitized_args=sanitized_args,
            sanitized_result=sanitized if isinstance(sanitized, dict) else {"value": sanitized},
        )

    @classmethod
    def failure(
        cls,
        error: str,
        *,
        duration_ms: int | None = None,
        sanitized_args: dict[str, Any] | None = None,
    ) -> ToolResultModel:
        return cls(
            success=False,
            error=error,
            duration_ms=duration_ms,
            sanitized_args=sanitized_args,
            sanitized_result={},
        )
