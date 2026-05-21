from __future__ import annotations

from copilot_agent.tools.capability.base import CapabilityContext, CapabilityPack
from copilot_agent.tools.extensions.mcp.registry import register_mcp_tools
from copilot_agent.tools.registry import ToolRegistry


class McpCapability:
    name = "mcp"

    def register(self, registry: ToolRegistry, ctx: CapabilityContext) -> None:
        if ctx.mcp_runtime is None:
            raise ValueError("mcp capability requires McpRuntime from server bootstrap")
        register_mcp_tools(
            registry,
            handlers=ctx.mcp_runtime.handlers,
            config=ctx.mcp_runtime.config,
        )
