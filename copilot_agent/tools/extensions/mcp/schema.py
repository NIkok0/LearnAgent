from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class McpToolDefinition(BaseModel):
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    risk_level: str = "medium"
    requires_approval: bool = False
    required_scopes: list[str] = Field(default_factory=list)
    timeout_seconds: float = 60.0

    model_config = ConfigDict(extra="forbid")


class McpResourceDefinition(BaseModel):
    """MCP Resource — a readable data source exposed by an MCP server."""

    name: str
    description: str = ""
    uri: str
    mime_type: str | None = Field(default=None, alias="mimeType")

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class PromptArgument(BaseModel):
    """An argument accepted by an MCP Prompt template."""

    name: str
    description: str = ""
    required: bool = False

    model_config = ConfigDict(extra="forbid")


class McpPromptDefinition(BaseModel):
    """MCP Prompt — a pre-defined prompt template exposed by an MCP server."""

    name: str
    description: str = ""
    arguments: list[PromptArgument] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class McpReconnectConfig(BaseModel):
    """Automatic reconnection policy for MCP server connections."""

    enabled: bool = True
    max_retries: int = 10
    backoff_base_s: float = 1.0
    backoff_max_s: float = 60.0
    health_check_interval_s: float = 15.0

    model_config = ConfigDict(extra="forbid")


class McpServerDefinition(BaseModel):
    name: str
    transport: Literal["mock", "stdio", "sse", "streamable-http"] = "mock"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    sse_read_timeout: float = 300.0
    discover_tools: bool = False
    discover_resources: bool = False
    discover_prompts: bool = False
    enabled: bool = True
    tools: list[McpToolDefinition] = Field(default_factory=list)
    resources: list[McpResourceDefinition] = Field(default_factory=list)
    prompts: list[McpPromptDefinition] = Field(default_factory=list)
    reconnect: McpReconnectConfig = Field(default_factory=McpReconnectConfig)

    model_config = ConfigDict(extra="forbid")


class McpResourcesConfig(BaseModel):
    servers: list[McpServerDefinition] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")

    def enabled_servers(self) -> list[McpServerDefinition]:
        return [server for server in self.servers if server.enabled]
