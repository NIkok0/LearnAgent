from __future__ import annotations

from pathlib import Path

import yaml

from copilot_agent.scenario.router.engine import RouterEngine
from copilot_agent.scenario.router.schema import RouterRulesConfig
from copilot_agent.scenario.router.types import ToolRoute, ToolRouteKind, build_route_system_message, tool_allowed

__all__ = [
    "RouterEngine",
    "RouterRulesConfig",
    "ToolRoute",
    "ToolRouteKind",
    "build_route_system_message",
    "load_router_rules",
    "route_tools",
    "tool_allowed",
]


def load_router_rules(path: Path) -> RouterRulesConfig:
    if not path.is_file():
        return RouterRulesConfig()
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return RouterRulesConfig()
    return RouterRulesConfig.model_validate(raw)


def route_tools(
    query: str,
    *,
    rules: RouterRulesConfig | None = None,
    engine: RouterEngine | None = None,
    confirm_dangerous: bool = False,
    allow_job_post: bool = False,
) -> ToolRoute:
    """Route a user query to a tool plan using Scenario declarative rules."""
    if engine is not None:
        return engine.route(
            query,
            confirm_dangerous=confirm_dangerous,
            allow_job_post=allow_job_post,
        )
    if rules is not None:
        return RouterEngine(rules).route(
            query,
            confirm_dangerous=confirm_dangerous,
            allow_job_post=allow_job_post,
        )
    from copilot_agent.scenario import load_scenario

    return load_scenario().router_engine.route(
        query,
        confirm_dangerous=confirm_dangerous,
        allow_job_post=allow_job_post,
    )
