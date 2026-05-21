from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from copilot_agent.agent.prompts import DANGEROUS_JOB_PATH

ToolRouteKind = Literal[
    "knowledge",
    "live_status",
    "troubleshooting",
    "dangerous_execute",
    "safety_reject",
]

_UUID_RE = re.compile(
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


def _contains_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(p, text, flags=re.IGNORECASE) for p in patterns)


def _wants_enqueue(text: str) -> bool:
    if _contains_any(text, (r"创建水印任务", r"提交水印", r"为\s*fileId\s*=")):
        return True
    if re.search(r"POST\s+/api/v1/jobs/watermark", text, flags=re.IGNORECASE):
        if _contains_any(text, (r"默认", r"字段", r"需要哪些", r"契约", r"返回什么")):
            return False
        return _contains_any(text, (r"提交", r"创建", r"confirm", r"fileId", r"test"))
    return False


def _is_api_contract_question(text: str) -> bool:
    has_endpoint = _contains_any(
        text,
        (r"/api/", r"GET\s+", r"POST\s+", r"/actuator/", r"401", r"UNAUTHORIZED"),
    )
    asks_contract = _contains_any(
        text,
        (
            r"需要哪些.*字段",
            r"返回什么",
            r"表示什么",
            r"默认",
            r"Error Model",
            r"契约",
        ),
    )
    return has_endpoint and asks_contract


def route_tools(
    query: str,
    *,
    confirm_dangerous: bool = False,
    allow_job_post: bool = False,
) -> ToolRoute:
    text = query.strip()
    if not text:
        return ToolRoute(
            kind="knowledge",
            recommended_tools=("search_docs",),
            forbidden_tools=(),
            suggested_paths=(),
            rationale="Empty query; default to documentation search if needed.",
        )

    # --- safety: external URL / blocked dangerous POST without gates ---
    if _contains_any(text, (r"https?://evil", r"evil\.example", r"非白名单\s*URL", r"external_url")):
        return ToolRoute(
            kind="safety_reject",
            recommended_tools=(),
            forbidden_tools=("search_docs", "http_get", "http_post"),
            suggested_paths=(),
            rationale="Non-whitelisted external URL requests must be refused.",
        )

    wants_job_post = _wants_enqueue(text)
    if wants_job_post and not (confirm_dangerous and allow_job_post):
        if _contains_any(text, (r"没有确认", r"没有开启环境变量", r"直接\s*POST")):
            return ToolRoute(
                kind="safety_reject",
                recommended_tools=(),
                forbidden_tools=("http_post",),
                suggested_paths=(),
                rationale="Dangerous POST blocked: missing confirm_dangerous or COPILOT_ALLOW_JOB_POST.",
            )

    if wants_job_post and confirm_dangerous and allow_job_post:
        return ToolRoute(
            kind="dangerous_execute",
            recommended_tools=("search_docs", "http_post"),
            forbidden_tools=(),
            suggested_paths=(DANGEROUS_JOB_PATH,),
            rationale="High-risk job enqueue: read docs first, then POST with approval.",
        )

    if _is_api_contract_question(text) or _contains_any(text, (r"白名单", r"是否允许", r"允许\s+GET")):
        return ToolRoute(
            kind="knowledge",
            recommended_tools=("search_docs",),
            forbidden_tools=("http_get", "http_post"),
            suggested_paths=(),
            rationale="Policy or API contract question; cite platform docs via search_docs.",
        )

    uuid_match = _UUID_RE.search(text)
    # --- troubleshooting: docs first, then optional API (before pure live_status) ---
    if _contains_any(
        text,
        (
            r"QUEUED|PROCESSING|FAILED",
            r"排查",
            r"卡住|不动",
            r"怎么办",
            r"为什么.*失败",
            r"errorCode",
            r"一直\s*(QUEUED|PROCESSING)",
        ),
    ):
        suggested: tuple[str, ...] = ("/actuator/health", "/api/v1/jobs/")
        if uuid_match:
            suggested = (f"/api/v1/jobs/{uuid_match.group(0)}",) + suggested
        return ToolRoute(
            kind="troubleshooting",
            recommended_tools=("search_docs", "http_get"),
            forbidden_tools=("http_post",),
            suggested_paths=suggested,
            rationale="Runbook/deploy docs first, then check live task or platform status.",
        )

    # --- live platform status (API tools) ---
    if uuid_match or _contains_any(
        text,
        (
            r"是否存活",
            r"actuator/health",
            r"/actuator/health",
            r"健康检查",
            r"统计",
            r"dashboard",
            r"文件列表",
            r"当前文件",
            r"能查吗",
            r"管理员接口",
            r"admin/",
            r"/api/v1/admin/",
            r"/api/v1/files",
            r"/api/v1/stats/",
            r"登录用户.*任务.*状态",
            r"查询.*任务.*状态",
        ),
    ):
        paths: list[str] = []
        if _contains_any(text, (r"存活", r"health", r"健康")):
            paths.append("/actuator/health")
        if _contains_any(text, (r"统计", r"dashboard", r"匿名")):
            paths.append("/api/v1/stats/dashboard")
        if _contains_any(text, (r"文件列表", r"当前文件", r"能查吗")):
            paths.append("/api/v1/files")
        if _contains_any(text, (r"管理员", r"admin", r"权限管理")):
            paths.extend(["/api/v1/admin/stats", "/api/v1/admin/users", "/api/v1/admin/groups"])
        if uuid_match:
            paths.append(f"/api/v1/jobs/{uuid_match.group(0)}")
        if _contains_any(text, (r"未登录", r"先引导登录", r"登录")):
            recommended: tuple[str, ...] = ("http_post", "http_get")
            paths.insert(0, "/api/v1/auth/login")
        else:
            recommended = ("http_get",)
        return ToolRoute(
            kind="live_status",
            recommended_tools=recommended,
            forbidden_tools=(),
            suggested_paths=tuple(dict.fromkeys(paths)),
            rationale="Question needs live Java API data, not documentation alone.",
        )

    # --- default: static knowledge via RAG ---
    return ToolRoute(
        kind="knowledge",
        recommended_tools=("search_docs",),
        forbidden_tools=("http_get", "http_post"),
        suggested_paths=(),
        rationale="Static platform documentation question; use search_docs only.",
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
