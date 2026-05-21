from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

ToolRouteKind = Literal[
    "knowledge",
    "live_status",
    "troubleshooting",
    "dangerous_execute",
    "safety_reject",
]

UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class ToolRoute:
    kind: ToolRouteKind
    recommended_tools: tuple[str, ...]
    forbidden_tools: tuple[str, ...]
    suggested_paths: tuple[str, ...]
    rationale: str

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "recommended_tools": list(self.recommended_tools),
            "forbidden_tools": list(self.forbidden_tools),
            "suggested_paths": list(self.suggested_paths),
            "rationale": self.rationale,
        }


def build_route_system_message(route: ToolRoute) -> str:
    lines = [
        "Tool routing plan for this user turn (follow before choosing tools):",
        f"- Intent: {route.kind}",
    ]
    if route.recommended_tools:
        lines.append(f"- Recommended tool order: {' -> '.join(route.recommended_tools)}")
    else:
        lines.append("- Recommended: respond without calling tools")
    if route.forbidden_tools:
        lines.append(f"- Do not call: {', '.join(route.forbidden_tools)}")
    if route.suggested_paths:
        lines.append(f"- Suggested API paths (http_get whitelist): {', '.join(route.suggested_paths)}")
    lines.append(f"- Rationale: {route.rationale}")
    return "\n".join(lines)


def tool_route_from_mapping(data: object) -> ToolRoute | None:
    if not isinstance(data, dict) or not data.get("kind"):
        return None
    kind = str(data.get("kind", "knowledge"))
    if kind not in {
        "knowledge",
        "live_status",
        "troubleshooting",
        "dangerous_execute",
        "safety_reject",
    }:
        kind = "knowledge"
    return ToolRoute(
        kind=kind,  # type: ignore[arg-type]
        recommended_tools=tuple(str(item) for item in (data.get("recommended_tools") or [])),
        forbidden_tools=tuple(str(item) for item in (data.get("forbidden_tools") or [])),
        suggested_paths=tuple(str(item) for item in (data.get("suggested_paths") or [])),
        rationale=str(data.get("rationale") or ""),
    )


def tool_allowed(route: ToolRoute, tool_name: str) -> bool:
    if tool_name in route.forbidden_tools:
        return False
    if route.kind == "safety_reject":
        return False
    if not route.recommended_tools:
        return False
    if route.kind == "knowledge" and tool_name in {"http_get", "http_post"}:
        return False
    return True
