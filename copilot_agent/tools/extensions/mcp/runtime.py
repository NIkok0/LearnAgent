from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from copilot_agent.tools.extensions.mcp.client import McpClient, create_mcp_client
from copilot_agent.tools.extensions.mcp.handlers import McpToolHandlers
from copilot_agent.tools.extensions.mcp.registry import register_mcp_tools
from copilot_agent.tools.extensions.mcp.schema import McpResourcesConfig, McpServerDefinition, McpToolDefinition
from copilot_agent.tools.extensions.mcp.sdk_client import SdkMcpClient, mcp_sdk_available
from copilot_agent.tools.registry import ToolRegistry

log = logging.getLogger(__name__)


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for base in here.parents:
        if (base / "copilot_agent").is_dir() and (base / "docs").is_dir():
            return base
    return here.parents[4]


async def resolve_mcp_config(
    config: McpResourcesConfig,
    clients: dict[str, McpClient],
) -> McpResourcesConfig:
    servers: list[McpServerDefinition] = []
    for server in config.enabled_servers():
        client = clients.get(server.name)
        if client is None:
            continue
        overrides = {tool.name: tool for tool in server.tools}
        should_discover = server.discover_tools or (
            server.transport in {"stdio", "sse"} and not server.tools
        )
        tools: list[McpToolDefinition] = list(server.tools)
        if should_discover:
            discovered = await client.list_tools()
            if discovered:
                merged: list[McpToolDefinition] = []
                for item in discovered:
                    override = overrides.get(item.name)
                    if override is None:
                        merged.append(item)
                        continue
                    merged.append(
                        item.model_copy(
                            update={
                                "description": override.description or item.description,
                                "risk_level": override.risk_level,
                                "requires_approval": override.requires_approval,
                                "timeout_seconds": override.timeout_seconds,
                            }
                        )
                    )
                tools = merged
        servers.append(server.model_copy(update={"tools": tools}))
    return McpResourcesConfig(servers=servers)


def create_mcp_clients(
    config: McpResourcesConfig,
    *,
    repo_root: Path | None = None,
    scenario_root: Path | None = None,
) -> dict[str, McpClient]:
    root = repo_root or _repo_root()
    clients: dict[str, McpClient] = {}
    for server in config.enabled_servers():
        if server.transport in {"stdio", "sse"}:
            if not mcp_sdk_available():
                log.error(
                    "MCP SDK missing; cannot start transport=%s server=%s",
                    server.transport,
                    server.name,
                )
                continue
            clients[server.name] = SdkMcpClient(server, repo_root=root, scenario_root=scenario_root)
        else:
            clients[server.name] = create_mcp_client(server)
    return clients


@dataclass
class McpRuntime:
    handlers: McpToolHandlers
    config: McpResourcesConfig
    clients: dict[str, McpClient]

    async def aclose(self) -> None:
        for client in self.clients.values():
            closer = getattr(client, "aclose", None)
            if callable(closer):
                await closer()

    def register_tools(self, registry: ToolRegistry) -> list[str]:
        return register_mcp_tools(registry, handlers=self.handlers, config=self.config)

    @classmethod
    async def start(
        cls,
        config: McpResourcesConfig | None,
        *,
        repo_root: Path | None = None,
        scenario_root: Path | None = None,
        connect: bool = True,
    ) -> McpRuntime | None:
        if config is None or not config.enabled_servers():
            return None
        clients = create_mcp_clients(config, repo_root=repo_root, scenario_root=scenario_root)
        if connect:
            for client in clients.values():
                connect_fn = getattr(client, "connect", None)
                if callable(connect_fn):
                    await connect_fn()
        resolved = await resolve_mcp_config(config, clients)
        handlers = McpToolHandlers(clients)
        log.info(
            "MCP runtime started servers=%s tools=%d",
            list(clients.keys()),
            sum(len(server.tools) for server in resolved.enabled_servers()),
        )
        return cls(handlers=handlers, config=resolved, clients=clients)
