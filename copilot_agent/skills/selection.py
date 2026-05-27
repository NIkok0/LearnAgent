from __future__ import annotations

from typing import Any

from copilot_agent.skills.schema import SkillSpec


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
        for keyword in skill.triggers.keywords:
            if _norm(keyword) and _norm(keyword) in normalized_goal:
                reasons.append(f"keyword:{keyword}")
        if route_kind and route_kind in set(skill.triggers.routes):
            reasons.append(f"route:{route_kind}")
        if not reasons:
            continue
        missing_capabilities = [
            cap for cap in skill.required_capabilities if cap.lower() not in enabled
        ]
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
            }
        )
    return selected


def skill_system_message(selected: list[dict[str, Any]]) -> str:
    if not selected:
        return ""
    lines = ["[Skills]"]
    for item in selected:
        lines.append(f"- {item.get('name')}: {item.get('description')}")
        tools = ", ".join(str(tool) for tool in item.get("tool_allowlist") or [])
        if tools:
            lines.append(f"  Recommended tools: {tools}")
        instructions = str(item.get("instructions") or "").strip()
        if instructions:
            lines.append(f"  Instructions: {instructions}")
    return "\n".join(lines)


def public_selected_skill(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": item.get("name"),
        "description": item.get("description"),
        "risk_level": item.get("risk_level"),
        "tool_allowlist": item.get("tool_allowlist") or [],
        "required_capabilities": item.get("required_capabilities") or [],
        "missing_capabilities": item.get("missing_capabilities") or [],
        "trigger_reasons": item.get("trigger_reasons") or [],
    }


def _norm(value: str) -> str:
    return " ".join(str(value or "").lower().split())
