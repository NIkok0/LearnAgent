from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel

from copilot_agent.scenario.loader import LoadedScenario
from copilot_agent.tools.registry import ApprovalRule, ToolRegistry


@dataclass(frozen=True)
class CapabilityContext:
    """Runtime dependencies passed into capability packs (handlers, scenario policy)."""

    scenario: LoadedScenario
    handlers: Any  # ToolHandlers — avoid circular import
    mcp_runtime: Any | None = None  # McpRuntime | None


class CapabilityPack(Protocol):
    """Capability layer contract: declare ToolSpec + bind handlers onto ToolRegistry."""

    name: str

    def register(self, registry: ToolRegistry, ctx: CapabilityContext) -> None: ...


def dangerous_post_approval_rule(scenario: LoadedScenario) -> ApprovalRule:
    dangerous_paths = tuple(path for path in scenario.policy.dangerous_paths if str(path).strip())

    def _requires_approval(args: dict[str, Any]) -> bool:
        if not dangerous_paths:
            return False
        return str(args.get("path", "")).split("?", 1)[0] in dangerous_paths

    return _requires_approval
