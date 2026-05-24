from __future__ import annotations

from dataclasses import dataclass

from copilot_agent.scenario.router.types import ToolRoute


@dataclass(frozen=True)
class RouteDecision:
    route: ToolRoute
    matched_rule_id: str | None = None
    used_defaults: bool = False
