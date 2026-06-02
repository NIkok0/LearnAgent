from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from copilot_agent.contracts.plan import PlanModel, PlanStepModel
from copilot_agent.llm import LLMProvider
from copilot_agent.scenario.router.types import ToolRoute
from copilot_agent.settings import settings
from copilot_agent.tools.registry import ToolRegistry

_ROUTE_KINDS: frozenset[str] = frozenset(
    {"knowledge", "live_status", "troubleshooting", "dangerous_execute", "safety_reject"}
)

_PLANNER_PROMPT = """You are the planner for LearnAgent.

Return ONLY JSON with keys:
- tool_route: {kind, recommended_tools, forbidden_tools, suggested_paths, rationale}
- plan: {goal, route_kind, steps}

Rules:
- kind must be one of knowledge, live_status, troubleshooting, dangerous_execute, safety_reject.
- recommended_tools and forbidden_tools may only use available tool names.
- steps is an array of {id, goal, tool_hint?, status}; status should be pending.
- Do not claim approval or execute tools. SafetyGate and PolicyGate make the final execution decision.
"""


@dataclass(frozen=True)
class LlmPlannerResult:
    route: ToolRoute
    plan: PlanModel


class LlmPlannerError(RuntimeError):
    pass


class LlmPlannerUnavailable(LlmPlannerError):
    pass


async def plan_with_llm(
    *,
    goal: str,
    baseline_route: ToolRoute,
    baseline_plan: PlanModel,
    tool_registry: ToolRegistry,
    llm_provider: LLMProvider,
) -> LlmPlannerResult:
    if not settings.agent_llm_planner_enabled:
        raise LlmPlannerUnavailable("llm_planner_disabled")
    if not settings.openai_api_key.strip():
        raise LlmPlannerUnavailable("openai_api_key_missing")

    available_tools = tool_registry.public_specs()
    payload = {
        "goal": goal,
        "baseline_tool_route": baseline_route.as_dict(),
        "baseline_plan": baseline_plan.as_dict(),
        "available_tools": available_tools,
    }
    model = llm_provider.get_chat_model()
    try:
        response = await asyncio.wait_for(
            model.ainvoke(
                [
                    SystemMessage(content=_PLANNER_PROMPT),
                    HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
                ]
            ),
            timeout=max(1.0, float(settings.agent_llm_planner_timeout_seconds)),
        )
    except Exception as exc:
        raise LlmPlannerError(f"llm_planner_invoke_failed:{exc}") from exc

    content = getattr(response, "content", "")
    if not isinstance(content, str) or not content.strip():
        raise LlmPlannerError("llm_planner_empty_response")
    try:
        parsed = _parse_json_object(content)
        route = _parse_route(parsed.get("tool_route"), baseline_route, tool_registry)
        plan = _parse_plan(parsed.get("plan"), goal=goal, route=route, tool_registry=tool_registry)
    except Exception as exc:
        if isinstance(exc, LlmPlannerError):
            raise
        raise LlmPlannerError(f"llm_planner_parse_failed:{exc}") from exc
    return LlmPlannerResult(route=route, plan=plan)


def _parse_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict):
        raise LlmPlannerError("llm_planner_json_not_object")
    return parsed


def _parse_route(data: object, baseline: ToolRoute, registry: ToolRegistry) -> ToolRoute:
    if not isinstance(data, dict):
        raise LlmPlannerError("llm_planner_tool_route_missing")
    kind = str(data.get("kind") or baseline.kind)
    if kind not in _ROUTE_KINDS:
        raise LlmPlannerError("llm_planner_invalid_route_kind")
    tool_names = set(registry.names())
    recommended = _tool_tuple(data.get("recommended_tools"), tool_names, field="recommended_tools")
    forbidden = _tool_tuple(data.get("forbidden_tools"), tool_names, field="forbidden_tools")
    suggested_paths = tuple(
        str(item).strip()
        for item in (data.get("suggested_paths") or [])
        if str(item).strip()
    )
    return ToolRoute(
        kind=kind,  # type: ignore[arg-type]
        recommended_tools=recommended,
        forbidden_tools=forbidden,
        suggested_paths=suggested_paths,
        rationale=str(data.get("rationale") or "LLM planner route").strip(),
    )


def _parse_plan(data: object, *, goal: str, route: ToolRoute, tool_registry: ToolRegistry) -> PlanModel:
    if not isinstance(data, dict):
        raise LlmPlannerError("llm_planner_plan_missing")
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise LlmPlannerError("llm_planner_steps_missing")
    tool_names = set(tool_registry.names())
    steps: list[PlanStepModel] = []
    for index, raw in enumerate(raw_steps, start=1):
        if not isinstance(raw, dict):
            raise LlmPlannerError("llm_planner_step_not_object")
        tool_hint = raw.get("tool_hint")
        if tool_hint is not None:
            tool_hint = str(tool_hint).strip() or None
            if tool_hint is not None and tool_hint not in tool_names:
                raise LlmPlannerError("llm_planner_invalid_step_tool")
        steps.append(
            PlanStepModel(
                id=str(raw.get("id") or f"step-{index}"),
                goal=str(raw.get("goal") or goal[:120]),
                tool_hint=tool_hint,
                status="pending",
            )
        )
    return PlanModel(goal=str(data.get("goal") or goal), route_kind=route.kind, steps=steps)


def _tool_tuple(value: object, tool_names: set[str], *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise LlmPlannerError(f"llm_planner_{field}_not_list")
    out: list[str] = []
    for item in value:
        name = str(item).strip()
        if not name:
            continue
        if name not in tool_names:
            raise LlmPlannerError(f"llm_planner_invalid_tool:{name}")
        out.append(name)
    return tuple(out)
