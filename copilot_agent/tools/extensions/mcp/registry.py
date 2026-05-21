from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from copilot_agent.tools.extensions.mcp.args_schema import build_mcp_args_schema
from copilot_agent.tools.extensions.mcp.handlers import McpToolHandlers
from copilot_agent.tools.extensions.mcp.naming import mcp_registry_tool_name
from copilot_agent.tools.extensions.mcp.schema import McpResourcesConfig, McpServerDefinition
from copilot_agent.tools.registry import ToolRegistry

log = logging.getLogger(__name__)


def load_mcp_config(path: Path) -> McpResourcesConfig:
    if not path.is_file():
        return McpResourcesConfig()
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return McpResourcesConfig()
    return McpResourcesConfig.model_validate(raw)


def register_mcp_tools(
    registry: ToolRegistry,
    *,
    handlers: McpToolHandlers,
    config: McpResourcesConfig,
) -> list[str]:
    """Register MCP tools from config onto ToolRegistry; return registered tool names."""
    registered: list[str] = []
    for server in config.enabled_servers():
        for tool in server.tools:
            registry_name = mcp_registry_tool_name(server.name, tool.name)
            handler = handlers.bind_tool(server=server.name, tool=tool.name)
            args_schema = build_mcp_args_schema(server.name, tool)
            registry.register_async(
                coroutine=handler,
                name=registry_name,
                description=f"[MCP:{server.name}] {tool.description or tool.name}",
                args_schema=args_schema,
                category="mcp",
                risk_level=tool.risk_level,
                requires_approval=tool.requires_approval,
                required_scopes=tuple(tool.required_scopes),
                timeout_seconds=tool.timeout_seconds,
                mcp_server=server.name,
                mcp_tool=tool.name,
            )
            registered.append(registry_name)
            log.info("Registered MCP tool %s (server=%s tool=%s)", registry_name, server.name, tool.name)
    return registered


def default_mock_config() -> McpResourcesConfig:
    return McpResourcesConfig(
        servers=[
            McpServerDefinition(
                name="demo",
                transport="mock",
                tools=[
                    {
                        "name": "echo",
                        "description": "Echo text through mock MCP server",
                        "input_schema": {
                            "type": "object",
                            "properties": {"text": {"type": "string", "description": "Text to echo"}},
                            "required": ["text"],
                        },
                        "risk_level": "low",
                    }
                ],
            )
        ]
    )
