from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

from copilot_agent.tools.extensions.mcp.client import McpClient, create_mcp_client
from copilot_agent.tools.extensions.mcp.handlers import McpToolHandlers
from copilot_agent.tools.extensions.mcp.registry import register_mcp_tools
from copilot_agent.tools.extensions.mcp.schema import (
    McpPromptDefinition,
    McpResourceDefinition,
    McpResourcesConfig,
    McpServerDefinition,
    McpToolDefinition,
)
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


async def _discover_resources(
    server: McpServerDefinition,
    client: McpClient,
) -> list[McpResourceDefinition]:
    overrides = {res.uri: res for res in server.resources}
    should_discover = server.discover_resources or (
        server.transport not in {"mock"} and not server.resources
    )
    if not should_discover:
        return list(server.resources)
    try:
        discovered = await client.list_resources()
    except Exception:
        log.warning("MCP server %s: list_resources failed", server.name, exc_info=True)
        return list(server.resources)
    merged: list[McpResourceDefinition] = []
    seen: set[str] = set()
    for item in discovered:
        override = overrides.get(item.uri)
        if override is None:
            merged.append(item)
        else:
            merged.append(
                item.model_copy(
                    update={
                        "description": override.description or item.description,
                        "mime_type": override.mime_type or item.mime_type,
                    }
                )
            )
        seen.add(item.uri)
    for uri, override in overrides.items():
        if uri not in seen:
            merged.append(override)
    return merged


async def _discover_prompts(
    server: McpServerDefinition,
    client: McpClient,
) -> list[McpPromptDefinition]:
    overrides = {prompt.name: prompt for prompt in server.prompts}
    should_discover = server.discover_prompts or (
        server.transport not in {"mock"} and not server.prompts
    )
    if not should_discover:
        return list(server.prompts)
    try:
        discovered = await client.list_prompts()
    except Exception:
        log.warning("MCP server %s: list_prompts failed", server.name, exc_info=True)
        return list(server.prompts)
    merged: list[McpPromptDefinition] = []
    seen: set[str] = set()
    for item in discovered:
        override = overrides.get(item.name)
        if override is None:
            merged.append(item)
        else:
            merged.append(
                item.model_copy(
                    update={
                        "description": override.description or item.description,
                    }
                )
            )
        seen.add(item.name)
    for name, override in overrides.items():
        if name not in seen:
            merged.append(override)
    return merged


@dataclass
class McpResourceStore:
    """In-memory cache of MCP resources indexed by URI."""

    by_uri: dict[str, McpResourceDefinition] = field(default_factory=dict)
    by_server: dict[str, list[str]] = field(default_factory=dict)  # server_name -> [uri, ...]

    def add(self, server_name: str, resources: list[McpResourceDefinition]) -> None:
        uris: list[str] = []
        for res in resources:
            self.by_uri[res.uri] = res
            uris.append(res.uri)
        self.by_server[server_name] = uris

    def remove(self, server_name: str) -> None:
        for uri in self.by_server.pop(server_name, []):
            self.by_uri.pop(uri, None)

    def list_all(self) -> list[McpResourceDefinition]:
        return list(self.by_uri.values())


@dataclass
class McpPromptStore:
    """In-memory cache of MCP prompts indexed by name."""

    by_name: dict[str, McpPromptDefinition] = field(default_factory=dict)
    by_server: dict[str, list[str]] = field(default_factory=dict)  # server_name -> [prompt_name, ...]

    def add(self, server_name: str, prompts: list[McpPromptDefinition]) -> None:
        names: list[str] = []
        for prompt in prompts:
            self.by_name[prompt.name] = prompt
            names.append(prompt.name)
        self.by_server[server_name] = names

    def remove(self, server_name: str) -> None:
        for name in self.by_server.pop(server_name, []):
            self.by_name.pop(name, None)

    def list_all(self) -> list[McpPromptDefinition]:
        return list(self.by_name.values())


def create_mcp_clients(
    config: McpResourcesConfig,
    *,
    repo_root: Path | None = None,
    scenario_root: Path | None = None,
) -> dict[str, McpClient]:
    root = repo_root or _repo_root()
    clients: dict[str, McpClient] = {}
    for server in config.enabled_servers():
        if server.transport in {"stdio", "sse", "streamable-http"}:
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
    resource_store: McpResourceStore = field(default_factory=McpResourceStore)
    prompt_store: McpPromptStore = field(default_factory=McpPromptStore)

    async def aclose(self) -> None:
        for client in self.clients.values():
            closer = getattr(client, "aclose", None)
            if callable(closer):
                await closer()

    def register_tools(self, registry: ToolRegistry) -> list[str]:
        return register_mcp_tools(registry, handlers=self.handlers, config=self.config)

    async def add_server(self, server: McpServerDefinition, *, repo_root: Path | None = None, scenario_root: Path | None = None) -> None:
        """Dynamically add and connect a new MCP server at runtime."""
        root = repo_root or _repo_root()
        client = create_mcp_client(server, repo_root=root, scenario_root=scenario_root)
        connect_fn = getattr(client, "connect", None)
        if callable(connect_fn):
            await connect_fn()
        self.clients[server.name] = client
        existing = [item for item in self.config.servers if item.name != server.name]
        self.config = self.config.model_copy(update={"servers": [*existing, server]})
        self.handlers = McpToolHandlers(self.clients)
        # discover capabilities
        resources = await _discover_resources(server, client)
        self.resource_store.add(server.name, resources)
        prompts = await _discover_prompts(server, client)
        self.prompt_store.add(server.name, prompts)
        log.info(
            "MCP server added name=%s tools=%d resources=%d prompts=%d",
            server.name, len(server.tools), len(resources), len(prompts),
        )

    async def remove_server(self, server_name: str) -> bool:
        """Dynamically disconnect and remove an MCP server at runtime."""
        client = self.clients.pop(server_name, None)
        if client is None:
            return False
        closer = getattr(client, "aclose", None)
        if callable(closer):
            await closer()
        self.config = self.config.model_copy(
            update={"servers": [server for server in self.config.servers if server.name != server_name]}
        )
        self.resource_store.remove(server_name)
        self.prompt_store.remove(server_name)
        self.handlers = McpToolHandlers(self.clients)
        log.info("MCP server removed name=%s", server_name)
        return True

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

        # Parallel discovery of resources and prompts
        resource_store = McpResourceStore()
        prompt_store = McpPromptStore()
        for server in resolved.enabled_servers():
            client = clients.get(server.name)
            if client is None:
                continue
            resources_task = _discover_resources(server, client)
            prompts_task = _discover_prompts(server, client)
            server_resources, server_prompts = await asyncio.gather(resources_task, prompts_task)
            resource_store.add(server.name, server_resources)
            prompt_store.add(server.name, server_prompts)

        handlers = McpToolHandlers(clients)
        total_resources = sum(len(v) for v in resource_store.by_server.values())
        total_prompts = sum(len(v) for v in prompt_store.by_server.values())
        log.info(
            "MCP runtime started servers=%s tools=%d resources=%d prompts=%d",
            list(clients.keys()),
            sum(len(server.tools) for server in resolved.enabled_servers()),
            total_resources,
            total_prompts,
        )
        return cls(
            handlers=handlers,
            config=resolved,
            clients=clients,
            resource_store=resource_store,
            prompt_store=prompt_store,
        )
