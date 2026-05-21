from copilot_agent.contracts.tool_result import ToolResultModel


def adapt_mcp_tool_result(
    raw: dict,
    *,
    server: str,
    tool: str,
    duration_ms: int | None = None,
    sanitized_args: dict | None = None,
) -> ToolResultModel:
    """Contract adapter entry for MCP handler outputs."""
    return ToolResultModel.from_mcp(
        raw,
        server=server,
        tool=tool,
        duration_ms=duration_ms,
        sanitized_args=sanitized_args,
    )
