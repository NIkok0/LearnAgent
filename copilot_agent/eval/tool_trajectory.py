from __future__ import annotations

from dataclasses import dataclass, field


def normalize_tool_name(spec: str) -> str:
    return str(spec or "").split(":", 1)[0].strip()


def _tool_path(record: dict[str, object]) -> str:
    path = record.get("path")
    return str(path or "").split("?", 1)[0]


@dataclass(frozen=True)
class TrajectoryVerdict:
    passed: bool
    required_tools_ok: bool
    forbidden_tools_ok: bool
    route_order_ok: bool
    rag_before_api_ok: bool
    blocked_ok: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "required_tools_ok": self.required_tools_ok,
            "forbidden_tools_ok": self.forbidden_tools_ok,
            "route_order_ok": self.route_order_ok,
            "rag_before_api_ok": self.rag_before_api_ok,
            "blocked_ok": self.blocked_ok,
            "reasons": list(self.reasons),
        }


def evaluate_trajectory(
    *,
    executed: list[dict[str, object]],
    expected_tools: list[str],
    forbidden_tools: list[str],
    expect_blocked: bool,
    route_recommended_tools: list[str],
    route_kind: str,
    strict_route_order: bool = True,
    strict_tool_order: bool = True,
) -> TrajectoryVerdict:
    executed_names = [normalize_tool_name(str(item.get("name", ""))) for item in executed]
    reasons: list[str] = []

    if expect_blocked:
        blocked_ok = len(executed_names) == 0
        if not blocked_ok:
            reasons.append("expected_no_tools_executed")
        return TrajectoryVerdict(
            passed=blocked_ok,
            required_tools_ok=True,
            forbidden_tools_ok=all(normalize_tool_name(f) not in executed_names for f in forbidden_tools),
            route_order_ok=True,
            rag_before_api_ok=True,
            blocked_ok=blocked_ok,
            reasons=tuple(reasons),
        )

    required_tools_ok = all(normalize_tool_name(exp) in executed_names for exp in expected_tools)
    if not required_tools_ok:
        reasons.append("missing_required_tool")

    forbidden_tools_ok = all(normalize_tool_name(forb) not in executed_names for forb in forbidden_tools)
    if not forbidden_tools_ok:
        reasons.append("forbidden_tool_executed")

    for exp in expected_tools:
        base = normalize_tool_name(exp)
        if ":" not in exp:
            continue
        _, detail = exp.split(":", 1)
        detail = detail.strip()
        if not detail:
            continue
        matched = any(
            normalize_tool_name(str(item.get("name", ""))) == base and _tool_path(item) == detail
            for item in executed
        )
        if not matched:
            required_tools_ok = False
            reasons.append(f"path_mismatch:{exp}")

    route_order_ok = executed_names == list(route_recommended_tools)
    if route_recommended_tools and not route_order_ok and not strict_route_order:
        route_order_ok = all(tool in executed_names for tool in route_recommended_tools)
    if not expected_tools and not expect_blocked:
        route_order_ok = True
    if route_recommended_tools and not route_order_ok and strict_route_order:
        reasons.append("route_order_mismatch")
    elif route_recommended_tools and not route_order_ok:
        reasons.append("route_tools_incomplete")

    rag_before_api_ok = True
    if strict_tool_order and route_kind == "troubleshooting" and "search_docs" in executed_names and "http_get" in executed_names:
        rag_before_api_ok = executed_names.index("search_docs") < executed_names.index("http_get")
        if not rag_before_api_ok:
            reasons.append("search_docs_not_before_http_get")

    passed = required_tools_ok and forbidden_tools_ok and route_order_ok and rag_before_api_ok
    return TrajectoryVerdict(
        passed=passed,
        required_tools_ok=required_tools_ok,
        forbidden_tools_ok=forbidden_tools_ok,
        route_order_ok=route_order_ok,
        rag_before_api_ok=rag_before_api_ok,
        blocked_ok=True,
        reasons=tuple(dict.fromkeys(reasons)),
    )
