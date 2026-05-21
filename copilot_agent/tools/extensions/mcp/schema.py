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


class McpServerDefinition(BaseModel):
    name: str
    transport: Literal["mock", "stdio", "sse"] = "mock"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    sse_read_timeout: float = 300.0
    discover_tools: bool = False
    enabled: bool = True
    tools: list[McpToolDefinition] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class McpResourcesConfig(BaseModel):
    servers: list[McpServerDefinition] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")

    def enabled_servers(self) -> list[McpServerDefinition]:
        return [server for server in self.servers if server.enabled]
