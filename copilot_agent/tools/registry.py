from __future__ import annotations

import asyncio
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
    required_scopes: tuple[str, ...] = ()
    timeout_seconds: float = 60.0
    max_retries: int = 0
    idempotency_key_field: str | None = None
    audit_enabled: bool = True
    mcp_server: str | None = None
    mcp_tool: str | None = None

    def requires_approval_for(self, args: dict[str, Any] | None = None) -> bool:
        if callable(self.requires_approval):
            return bool(self.requires_approval(args or {}))
        return bool(self.requires_approval)

    def public_dict(self, args: dict[str, Any] | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "risk_level": self.risk_level,
            "requires_approval": self.requires_approval_for(args),
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "idempotency_key_field": self.idempotency_key_field,
            "audit_enabled": self.audit_enabled,
        }
        if self.mcp_server:
            out["mcp_server"] = self.mcp_server
        if self.mcp_tool:
            out["mcp_tool"] = self.mcp_tool
        if self.required_scopes:
            out["required_scopes"] = list(self.required_scopes)
        return out


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
        required_scopes: tuple[str, ...] | list[str] = (),
        timeout_seconds: float = 60.0,
        max_retries: int = 0,
        idempotency_key_field: str | None = None,
        audit_enabled: bool = True,
        mcp_server: str | None = None,
        mcp_tool: str | None = None,
    ) -> None:
        spec = ToolSpec(
            name=name,
            description=description,
            args_schema=args_schema,
            category=category,
            risk_level=risk_level,
            requires_approval=requires_approval,
            required_scopes=tuple(required_scopes),
            timeout_seconds=timeout_seconds,
            max_retries=max(0, int(max_retries)),
            idempotency_key_field=idempotency_key_field,
            audit_enabled=audit_enabled,
            mcp_server=mcp_server,
            mcp_tool=mcp_tool,
        )
        self._specs[name] = spec
        wrapped_coroutine = _wrap_tool_coroutine(coroutine, spec)
        self._tools.append(
            StructuredTool.from_function(
                coroutine=wrapped_coroutine,
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
            description="Keyword search over platform docs (API, deploy, runbook, security, requirements, algorithms).",
            args_schema=search_docs_args_schema,
            category="memory",
            risk_level="low",
            requires_approval=False,
            timeout_seconds=30.0,
            max_retries=1,
        )
        registry.register_async(
            coroutine=http_get,
            name="http_get",
            description="GET from the scenario-configured HTTP API (path allowlist). Optional cookie_header overrides the server-stored session.",
            args_schema=http_get_args_schema,
            category="http",
            risk_level="medium",
            requires_approval=False,
            required_scopes=("http:read",),
            timeout_seconds=60.0,
            max_retries=1,
        )
        registry.register_async(
            coroutine=http_post,
            name="http_post",
            description="POST to scenario-configured HTTP paths (allowlist). Dangerous paths require deployment flag and approval.",
            args_schema=http_post_args_schema,
            category="http",
            risk_level="high",
            requires_approval=dangerous_post_requires_approval,
            required_scopes=("http:write",),
            timeout_seconds=120.0,
            max_retries=0,
            idempotency_key_field="idempotency_key",
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


def _wrap_tool_coroutine(coroutine: Callable[..., Any], spec: ToolSpec) -> Callable[..., Any]:
    async def wrapped(**kwargs: Any) -> Any:
        attempts = max(1, spec.max_retries + 1)
        last_error: BaseException | None = None
        for attempt in range(1, attempts + 1):
            try:
                return await asyncio.wait_for(coroutine(**kwargs), timeout=spec.timeout_seconds)
            except asyncio.TimeoutError as exc:
                last_error = ToolExecutionTimeout(
                    tool_name=spec.name,
                    timeout_seconds=spec.timeout_seconds,
                    attempt=attempt,
                    max_attempts=attempts,
                )
            except Exception as exc:
                last_error = exc
            if attempt >= attempts:
                break
            await asyncio.sleep(min(0.1 * attempt, 0.5))
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"tool execution failed without error: {spec.name}")

    return wrapped


class ToolExecutionTimeout(TimeoutError):
    def __init__(self, *, tool_name: str, timeout_seconds: float, attempt: int, max_attempts: int) -> None:
        self.tool_name = tool_name
        self.timeout_seconds = timeout_seconds
        self.attempt = attempt
        self.max_attempts = max_attempts
        super().__init__(
            f"tool {tool_name} timed out after {timeout_seconds}s "
            f"(attempt {attempt}/{max_attempts})"
        )
