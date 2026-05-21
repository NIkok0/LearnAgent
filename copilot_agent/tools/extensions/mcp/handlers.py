from __future__ import annotations

import logging
import time
from typing import Any

from copilot_agent.contracts.adapters.tool_mcp import adapt_mcp_tool_result
from copilot_agent.tools.extensions.mcp.client import McpClient

log = logging.getLogger(__name__)


class McpToolHandlers:
    """Dispatch MCP tool calls through registered in-process or future remote clients."""

    def __init__(self, clients: dict[str, McpClient]) -> None:
        self._clients = clients

    def client_for(self, server_name: str) -> McpClient | None:
        return self._clients.get(server_name)

    async def invoke(self, *, server: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        client = self._clients.get(server)
        if client is None:
            result = adapt_mcp_tool_result(
                {"success": False, "error": f"MCP server not registered: {server}"},
                server=server,
                tool=tool,
            )
            return result.to_llm_dict()
        t0 = time.perf_counter()
        try:
            raw = await client.call_tool(tool, arguments)
        except Exception as exc:
            log.exception("MCP tool call failed server=%s tool=%s", server, tool)
            raw = {"success": False, "error": str(exc)}
        duration_ms = int((time.perf_counter() - t0) * 1000)
        result = adapt_mcp_tool_result(
            raw,
            server=server,
            tool=tool,
            duration_ms=duration_ms,
            sanitized_args=arguments,
        )
        return result.to_llm_dict()

    def bind_tool(self, *, server: str, tool: str):
        async def _handler(**kwargs: Any) -> dict[str, Any]:
            return await self.invoke(server=server, tool=tool, arguments=kwargs)

        return _handler
