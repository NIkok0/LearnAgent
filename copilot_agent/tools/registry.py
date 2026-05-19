from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel

ApprovalRule = bool | Callable[[dict[str, Any]], bool]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_schema: type[BaseModel]
    category: str
    risk_level: str
    requires_approval: ApprovalRule = False
    timeout_seconds: float = 60.0
    audit_enabled: bool = True

    def requires_approval_for(self, args: dict[str, Any] | None = None) -> bool:
        if callable(self.requires_approval):
            return bool(self.requires_approval(args or {}))
        return bool(self.requires_approval)

    def public_dict(self, args: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "risk_level": self.risk_level,
            "requires_approval": self.requires_approval_for(args),
            "timeout_seconds": self.timeout_seconds,
            "audit_enabled": self.audit_enabled,
        }


class ToolRegistry:
    """Thin LangChain tool registry adapter."""

    def __init__(self) -> None:
        self._tools: list[StructuredTool] = []
        self._specs: dict[str, ToolSpec] = {}

    def register_async(
        self,
        *,
        name: str,
        description: str,
        coroutine: Callable[..., Any],
        args_schema: type[BaseModel],
        category: str,
        risk_level: str,
        requires_approval: ApprovalRule = False,
        timeout_seconds: float = 60.0,
        audit_enabled: bool = True,
    ) -> None:
        spec = ToolSpec(
            name=name,
            description=description,
            args_schema=args_schema,
            category=category,
            risk_level=risk_level,
            requires_approval=requires_approval,
            timeout_seconds=timeout_seconds,
            audit_enabled=audit_enabled,
        )
        self._specs[name] = spec
        self._tools.append(
            StructuredTool.from_function(
                coroutine=coroutine,
                func=None,
                name=name,
                description=description,
                args_schema=args_schema,
            )
        )

    @classmethod
    def from_agent_tools(
        cls,
        *,
        search_docs: Callable[..., Any],
        http_get: Callable[..., Any],
        http_post: Callable[..., Any],
        search_docs_args_schema: type[BaseModel],
        http_get_args_schema: type[BaseModel],
        http_post_args_schema: type[BaseModel],
        dangerous_post_requires_approval: ApprovalRule,
    ) -> "ToolRegistry":
        registry = cls()
        registry.register_async(
            coroutine=search_docs,
            name="search_docs",
            description="Keyword search over DEPLOY-SERVER.md, REQUIREMENTS-CHECKLIST-AND-TEST-CASES.md, watermark-java-backend-tech-selection.md.",
            args_schema=search_docs_args_schema,
            category="memory",
            risk_level="low",
            requires_approval=False,
            timeout_seconds=30.0,
        )
        registry.register_async(
            coroutine=http_get,
            name="http_get",
            description="GET from the Watermark Java API (whitelist only). Optional cookie_header overrides the server-stored session for this conversation.",
            args_schema=http_get_args_schema,
            category="http",
            risk_level="medium",
            requires_approval=False,
            timeout_seconds=60.0,
        )
        registry.register_async(
            coroutine=http_post,
            name="http_post",
            description="POST login or (if enabled) enqueue watermark job. Paths strictly whitelisted.",
            args_schema=http_post_args_schema,
            category="http",
            risk_level="high",
            requires_approval=dangerous_post_requires_approval,
            timeout_seconds=120.0,
        )
        return registry

    def tools(self) -> list[StructuredTool]:
        return list(self._tools)

    def names(self) -> list[str]:
        return [tool.name for tool in self._tools]

    def specs(self) -> list[ToolSpec]:
        return [self._specs[name] for name in self.names()]

    def public_specs(self) -> list[dict[str, Any]]:
        return [spec.public_dict() for spec in self.specs()]

    def get_spec(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)
