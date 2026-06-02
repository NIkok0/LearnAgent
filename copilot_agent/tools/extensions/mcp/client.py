from __future__ import annotations

import logging
from typing import Any, Protocol

from copilot_agent.tools.extensions.mcp.schema import (
    McpPromptDefinition,
    McpResourceDefinition,
    McpServerDefinition,
    McpToolDefinition,
)

log = logging.getLogger(__name__)


class McpClient(Protocol):
    server_name: str

    async def list_tools(self) -> list[McpToolDefinition]: ...

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...

    async def list_resources(self) -> list[McpResourceDefinition]: ...

    async def read_resource(self, uri: str) -> dict[str, Any]: ...

    async def list_prompts(self) -> list[McpPromptDefinition]: ...

    async def get_prompt(self, name: str, arguments: dict[str, str] | None = None) -> dict[str, Any]: ...


class MockMcpClient:
    """In-process MCP server stub for tests and PoC without external SDK."""

    def __init__(self, server: McpServerDefinition) -> None:
        self.server_name = server.name
        self._tools = {tool.name: tool for tool in server.tools}
        self._resources = {res.uri: res for res in server.resources}
        self._prompts = {prompt.name: prompt for prompt in server.prompts}

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

    async def list_resources(self) -> list[McpResourceDefinition]:
        return list(self._resources.values())

    async def read_resource(self, uri: str) -> dict[str, Any]:
        if uri not in self._resources:
            return {"success": False, "error": f"unknown resource: {uri}"}
        resource = self._resources[uri]
        return {
            "success": True,
            "uri": uri,
            "name": resource.name,
            "mime_type": resource.mime_type,
            "text": f"[mock content of {resource.name}]",
        }

    async def list_prompts(self) -> list[McpPromptDefinition]:
        return list(self._prompts.values())

    async def get_prompt(self, name: str, arguments: dict[str, str] | None = None) -> dict[str, Any]:
        if name not in self._prompts:
            return {"success": False, "error": f"unknown prompt: {name}"}
        prompt = self._prompts[name]
        args_desc = ", ".join(
            f"{a.name}={arguments.get(a.name, '?')}" if arguments else f"{a.name}=?"
            for a in prompt.arguments
        )
        return {
            "success": True,
            "name": name,
            "description": prompt.description,
            "messages": [
                {
                    "role": "user",
                    "content": f"[Mock prompt '{name}' with {args_desc}]",
                }
            ],
        }


def create_mcp_client(
    server: McpServerDefinition,
    *,
    repo_root=None,
    scenario_root=None,
) -> McpClient:
    if server.transport == "mock":
        return MockMcpClient(server)
    if server.transport in {"stdio", "sse", "streamable-http"}:
        from copilot_agent.tools.extensions.mcp.sdk_client import SdkMcpClient, mcp_sdk_available

        if not mcp_sdk_available():
            raise RuntimeError(
                f"MCP transport {server.transport} requires the mcp package (pip install mcp>=1.6.0)"
            )
        return SdkMcpClient(server, repo_root=repo_root, scenario_root=scenario_root)
    log.warning("unsupported MCP transport %s for server=%s; using mock", server.transport, server.name)
    return MockMcpClient(server)
