from __future__ import annotations

import logging
from typing import Any, Protocol

from copilot_agent.tools.extensions.mcp.schema import McpServerDefinition, McpToolDefinition

log = logging.getLogger(__name__)


class McpClient(Protocol):
    server_name: str

    async def list_tools(self) -> list[McpToolDefinition]: ...

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class MockMcpClient:
    """In-process MCP server stub for tests and PoC without external SDK."""

    def __init__(self, server: McpServerDefinition) -> None:
        self.server_name = server.name
        self._tools = {tool.name: tool for tool in server.tools}

    async def list_tools(self) -> list[McpToolDefinition]:
        return list(self._tools.values())

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name not in self._tools:
            return {"success": False, "error": f"unknown tool: {tool_name}"}
        if tool_name == "echo":
            text = str(arguments.get("text", ""))
            return {"success": True, "echo": text, "server": self.server_name}
        return {
            "success": True,
            "tool": tool_name,
            "arguments": arguments,
            "server": self.server_name,
        }


def create_mcp_client(
    server: McpServerDefinition,
    *,
    repo_root=None,
    scenario_root=None,
) -> McpClient:
    if server.transport == "mock":
        return MockMcpClient(server)
    if server.transport in {"stdio", "sse"}:
        from copilot_agent.tools.extensions.mcp.sdk_client import SdkMcpClient, mcp_sdk_available

        if not mcp_sdk_available():
            raise RuntimeError(
                f"MCP transport {server.transport} requires the mcp package (pip install mcp>=1.6.0)"
            )
        return SdkMcpClient(server, repo_root=repo_root, scenario_root=scenario_root)
    log.warning("unsupported MCP transport %s for server=%s; using mock", server.transport, server.name)
    return MockMcpClient(server)
