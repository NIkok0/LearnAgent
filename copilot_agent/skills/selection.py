from __future__ import annotations

from typing import Any

from copilot_agent.skills.schema import SkillSpec

KEYWORD_SCORE = 2.0
ROUTE_SCORE = 0.75
MIN_SELECTION_SCORE = 1.0


def select_skills(
    skills: list[SkillSpec],
    *,
    goal: str,
    route_kind: str = "",
    enabled_capabilities: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    normalized_goal = _norm(goal)
    enabled = {item.lower() for item in enabled_capabilities}
    selected: list[dict[str, Any]] = []
    for skill in skills:
        reasons: list[str] = []
        score = 0.0
        for keyword in skill.triggers.keywords:
            if _norm(keyword) and _norm(keyword) in normalized_goal:
                reasons.append(f"keyword:{keyword}")
                score += KEYWORD_SCORE
        if route_kind and route_kind in set(skill.triggers.routes):
            reasons.append(f"route:{route_kind}")
            score += ROUTE_SCORE
        if not reasons or score < MIN_SELECTION_SCORE:
            continue
        missing_capabilities = [
            cap for cap in skill.required_capabilities if cap.lower() not in enabled
        ]
        injected = not missing_capabilities
        selected.append(
            {
                "name": skill.name,
                "description": skill.description,
                "risk_level": skill.risk_level,
                "instructions": skill.instructions,
                "tool_allowlist": list(skill.tool_allowlist),
                "required_capabilities": list(skill.required_capabilities),
                "missing_capabilities": missing_capabilities,
                "trigger_reasons": reasons,
                "selection_score": round(score, 3),
                "injected": injected,
            }
        )
    return selected


def skill_system_message(selected: list[dict[str, Any]]) -> str:
    if not selected:
        return ""
    lines = ["[Skills]"]
    for item in selected:
        if not item.get("injected", True):
            continue
        lines.append(f"- {item.get('name')}: {item.get('description')}")
        tools = ", ".join(str(tool) for tool in item.get("tool_allowlist") or [])
        if tools:
            lines.append(f"  Recommended tools: {tools}")
        instructions = str(item.get("instructions") or "").strip()
        if instructions:
            lines.append(f"  Instructions: {instructions}")
    return "\n".join(lines) if len(lines) > 1 else ""


def public_selected_skill(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": item.get("name"),
        "description": item.get("description"),
        "risk_level": item.get("risk_level"),
        "tool_allowlist": item.get("tool_allowlist") or [],
        "required_capabilities": item.get("required_capabilities") or [],
        "missing_capabilities": item.get("missing_capabilities") or [],
        "trigger_reasons": item.get("trigger_reasons") or [],
        "selection_score": item.get("selection_score") or 0,
        "injected": bool(item.get("injected", True)),
    }


def _norm(value: str) -> str:
    return " ".join(str(value or "").lower().split())
