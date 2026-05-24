from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from copilot_agent.contracts.plan import PlanModel, PlanStepModel, PlanStepStatus
from copilot_agent.scenario.router.types import ToolRoute

log = logging.getLogger(__name__)


def build_plan_from_route(route: ToolRoute, *, goal: str) -> PlanModel:
    steps: list[PlanStepModel] = []
    for index, tool_name in enumerate(route.recommended_tools, start=1):
        steps.append(
            PlanStepModel(
                id=f"step-{index}",
                goal=f"Execute {tool_name} for: {goal[:120]}",
                tool_hint=tool_name,
                status="pending",
            )
        )
    if not steps:
        steps.append(
            PlanStepModel(
                id="step-1",
                goal=f"Answer without tools: {goal[:120]}",
                status="pending",
            )
        )
    return PlanModel(goal=goal, route_kind=route.kind, steps=steps)


def _tool_names(messages: list[BaseMessage]) -> set[str]:
    names: set[str] = set()
    for message in messages:
        if isinstance(message, ToolMessage):
            name = str(getattr(message, "name", "") or "").strip()
            if name:
                names.add(name)
    return names


def update_plan_outcomes(plan: PlanModel, messages: list[BaseMessage]) -> PlanModel:
    tool_names = _tool_names(messages)
    updated_steps: list[PlanStepModel] = []
    for step in plan.steps:
        hint = str(step.tool_hint or "").strip()
        if hint and hint in tool_names and step.status == "pending":
            updated_steps.append(
                step.model_copy(
                    update={
                        "status": "completed",
                        "outcome": f"{hint} executed",
                    }
                )
            )
        else:
            updated_steps.append(step)
    return plan.model_copy(update={"steps": updated_steps})


def maybe_replan_troubleshooting(plan: PlanModel, route_kind: str, messages: list[BaseMessage]) -> PlanModel | None:
    if route_kind != "troubleshooting":
        return None
    tool_names = _tool_names(messages)
    if not {"search_docs", "http_get"}.issubset(tool_names):
        return None
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            content = str(getattr(message, "content", "") or "").strip()
            if content and not getattr(message, "tool_calls", None):
                return None
            break
    if any(step.id == "step-replan-summary" for step in plan.steps):
        return None
    extra = PlanStepModel(
        id="step-replan-summary",
        goal="Summarize troubleshooting findings from docs and live API checks",
        status="pending",
    )
    return plan.model_copy(update={"steps": [*plan.steps, extra]})


def plan_step_statuses(plan: PlanModel) -> list[PlanStepStatus]:
    return [step.status for step in plan.steps]
