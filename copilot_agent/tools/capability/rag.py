from __future__ import annotations

from pydantic import BaseModel, Field

from copilot_agent.tools.capability.base import CapabilityContext, CapabilityPack
from copilot_agent.tools.registry import ToolRegistry


class SearchDocsArgs(BaseModel):
    query: str = Field(description="Natural language or keywords")


class RagCapability:
    name = "rag"

    def register(self, registry: ToolRegistry, ctx: CapabilityContext) -> None:
        registry.register_async(
            coroutine=ctx.handlers.search_docs,
            name="search_docs",
            description="Keyword search over platform docs (API, deploy, runbook, security, requirements, algorithms).",
            args_schema=SearchDocsArgs,
            category="memory",
            risk_level="low",
            requires_approval=False,
            timeout_seconds=30.0,
        )
