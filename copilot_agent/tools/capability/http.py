from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from copilot_agent.tools.capability.base import CapabilityContext, CapabilityPack, dangerous_post_approval_rule
from copilot_agent.tools.registry import ToolRegistry


class HttpGetArgs(BaseModel):
    path: str = Field(description="Path starting with /api/v1/ or /actuator/health")
    cookie_header: Optional[str] = Field(default=None, description="Optional Cookie header")


class HttpPostArgs(BaseModel):
    path: str
    json_body: dict[str, Any]
    cookie_header: Optional[str] = None
    idempotency_key: Optional[str] = None


class HttpCapability:
    name = "http"

    def register(self, registry: ToolRegistry, ctx: CapabilityContext) -> None:
        registry.register_async(
            coroutine=ctx.handlers.http_get,
            name="http_get",
            description="GET from the scenario-configured HTTP API (path allowlist). Optional cookie_header overrides the server-stored session.",
            args_schema=HttpGetArgs,
            category="http",
            risk_level="medium",
            requires_approval=False,
            required_scopes=("http:read",),
            timeout_seconds=60.0,
        )
        registry.register_async(
            coroutine=ctx.handlers.http_post,
            name="http_post",
            description="POST to scenario-configured HTTP paths (allowlist). Dangerous paths require deployment flag and approval.",
            args_schema=HttpPostArgs,
            category="http",
            risk_level="high",
            requires_approval=dangerous_post_approval_rule(ctx.scenario),
            required_scopes=("http:write",),
            timeout_seconds=120.0,
        )
