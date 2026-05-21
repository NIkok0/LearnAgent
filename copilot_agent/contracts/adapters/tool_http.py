from __future__ import annotations

from typing import Any

from copilot_agent.contracts.tool_result import ToolResultModel


class HttpResponseAdapter:
    """Convert HTTP tool response dicts to ToolResultModel."""

    @staticmethod
    def to_tool_result(
        raw: dict[str, Any],
        *,
        duration_ms: int | None = None,
        sanitized_args: dict[str, Any] | None = None,
    ) -> ToolResultModel:
        return ToolResultModel.from_http_result(
            raw,
            duration_ms=duration_ms,
            sanitized_args=sanitized_args,
        )
