from __future__ import annotations

import re

# (pattern, appended platform terms) — order matters for readability in logs.
_REWRITE_RULES: tuple[tuple[str, str], ...] = (
    (r"排队|队列|积压", "Redis Stream WM_JOBS wm:jobs:stream queue"),
    (r"卡住|不动|一直\s*(QUEUED|PROCESSING)?|排查", "QUEUED PROCESSING worker Redis Stream RUNBOOK"),
    (r"环境变量|自检", "verify-config DEPLOY environment WM_JOBS"),
    (r"生产部署|部署步骤|部署", "deploy DEPLOY-SERVER verify-config"),
    (r"水印任务|任务状态|任务 JSON", "watermark job WM_JOBS queue JSON"),
    (r"Redis|Stream|stream", "Redis Stream wm:jobs:stream WM_JOBS_GROUP"),
    (r"消费者组|worker|Worker", "WM_JOBS_GROUP wm-workers worker"),
    (r"401|未登录|会话|UNAUTHORIZED", "UNAUTHORIZED API-CONTRACT WMSESSIONID"),
    (r"SLO|可用性|availability", "OPERATIONS availability 99.5 SLA"),
    (r"告警|pending|P2", "OPERATIONS pending alert P2"),
    (r"Runbook|巡检|Daily Checks", "RUNBOOK Daily Checks"),
    (r"需求|偏差|checklist|R-\d+", "REQUIREMENTS checklist R-001"),
    (r"算法|DWT|PNG|PDF|algorithmType", "README_ALGORITHM algorithmType DWT"),
    (r"白名单|HTTPS|Cookie", "SECURITY-BASELINE whitelist WMSESSIONID"),
    (r"FAILED|errorCode|ALGORITHM_ERROR", "RUNBOOK FAILED errorCode ALGORITHM_ERROR"),
    (r"POST\s+/api|GET\s+/api|/actuator/", "API-CONTRACT endpoint"),
    (r"权限|管理员", "admin API stats users groups"),
)

_DOC_TYPE_HINT_RULES: tuple[tuple[str, str, float], ...] = (
    (r"/api/|POST\s+|GET\s+|401|UNAUTHORIZED|actuator", "api_contract", 1.18),
    (r"部署|verify-config|环境变量|DEPLOY", "deploy", 1.12),
    (r"Redis|WM_JOBS|Stream|queue|QUEUED|PROCESSING|worker", "tech_selection", 1.12),
    (r"Runbook|FAILED|errorCode|巡检|排查", "runbook", 1.10),
    (r"SLO|SLA|告警|pending|availability", "operations", 1.10),
    (r"HTTPS|白名单|Cookie|SECURITY", "security", 1.10),
    (r"需求|偏差|checklist|R-\d+", "requirements", 1.12),
    (r"algorithm|DWT|PNG|PDF|算法", "algorithm", 1.10),
    (r"wm:jobs:stream|platform|总览", "overview", 1.05),
)


def rewrite_query(query: str) -> str:
    """Append platform terms when colloquial Chinese or vague phrasing is detected."""
    text = query.strip()
    if not text:
        return text
    extras: list[str] = []
    for pattern, expansion in _REWRITE_RULES:
        if re.search(pattern, text, flags=re.IGNORECASE):
            extras.append(expansion)
    if not extras:
        return text
    return f"{text} {' '.join(extras)}"


def query_doc_type_hints(query: str) -> dict[str, float]:
    """Optional per-query doc_type multipliers (merged with static boosts in fusion)."""
    hints: dict[str, float] = {}
    for pattern, doc_type, boost in _DOC_TYPE_HINT_RULES:
        if re.search(pattern, query, flags=re.IGNORECASE):
            hints[doc_type] = max(hints.get(doc_type, 1.0), boost)
    return hints
