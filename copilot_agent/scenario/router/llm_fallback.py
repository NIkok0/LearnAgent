from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from copilot_agent.scenario.router.types import ToolRoute, ToolRouteKind
from copilot_agent.settings import settings

log = logging.getLogger(__name__)

RouteClassifier = Callable[[str, ToolRoute], Awaitable[dict[str, Any]]]

_ROUTE_KINDS: frozenset[str] = frozenset(
    {"knowledge", "live_status", "troubleshooting", "dangerous_execute", "safety_reject"}
)

_CLASSIFY_PROMPT = """Classify the user question into one tool-routing intent for a watermark platform copilot.

Return ONLY JSON with keys:
- kind: one of knowledge, live_status, troubleshooting, dangerous_execute, safety_reject
- recommended_tools: array of tool names from search_docs, http_get, http_post
- suggested_paths: optional array of API path hints for http_get
- rationale: short string

Rules:
- Prefer knowledge + search_docs for static documentation questions.
- Use live_status + http_get when the user needs current platform/API data.
- Use troubleshooting when runbook + live checks are needed.
- Never choose safety_reject unless the user asks for clearly disallowed external URLs.
"""


def _forbidden_for_kind(kind: ToolRouteKind) -> tuple[str, ...]:
    if kind == "knowledge":
        return ("http_get", "http_post")
    if kind == "troubleshooting":
        return ("http_post",)
    if kind == "live_status":
        return ()
    if kind == "dangerous_execute":
        return ()
    return ("search_docs", "http_get", "http_post")


def _merge_llm_route(baseline: ToolRoute, parsed: dict[str, Any]) -> ToolRoute:
    kind = str(parsed.get("kind") or baseline.kind)
    if kind not in _ROUTE_KINDS:
        return baseline
    recommended_raw = parsed.get("recommended_tools")
    recommended = (
        tuple(str(item) for item in recommended_raw if str(item).strip())
        if isinstance(recommended_raw, list) and recommended_raw
        else baseline.recommended_tools
    )
    paths_raw = parsed.get("suggested_paths")
    suggested_paths = (
        tuple(str(item) for item in paths_raw if str(item).strip())
        if isinstance(paths_raw, list) and paths_raw
        else baseline.suggested_paths
    )
    rationale = str(parsed.get("rationale") or baseline.rationale).strip()
    if rationale and rationale != baseline.rationale:
        rationale = f"{rationale} [LLM fallback]"
    else:
        rationale = f"{baseline.rationale} [LLM fallback]"
    return ToolRoute(
        kind=kind,  # type: ignore[arg-type]
        recommended_tools=recommended,
        forbidden_tools=_forbidden_for_kind(kind),  # type: ignore[arg-type]
        suggested_paths=suggested_paths,
        rationale=rationale,
    )


def _parse_classifier_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    parsed = json.loads(cleaned)
    return parsed if isinstance(parsed, dict) else {}


async def _default_llm_classify(query: str, baseline: ToolRoute) -> dict[str, Any]:
    from copilot_agent.llm import LLMProvider

    if not settings.openai_api_key.strip():
        return {}
    model = LLMProvider().get_chat_model()
    response = await model.ainvoke(
        [
            SystemMessage(content=_CLASSIFY_PROMPT),
            HumanMessage(
                content=(
                    f"Baseline rule route: {baseline.as_dict()}\n"
                    f"User question: {query}\n"
                    "If baseline is generic documentation-only, refine when live API data is clearly needed."
                )
            ),
        ]
    )
    content = getattr(response, "content", "")
    if not isinstance(content, str) or not content.strip():
        return {}
    return _parse_classifier_json(content)


async def refine_route_with_llm(
    query: str,
    baseline: ToolRoute,
    *,
    classifier: RouteClassifier | None = None,
) -> ToolRoute:
    """Rules-first route refinement when declarative rules fall back to defaults."""
    if not settings.agent_tool_route_llm_fallback:
        return baseline
    classify = classifier or _default_llm_classify
    try:
        parsed = await classify(query, baseline)
    except Exception as exc:
        log.warning("LLM route fallback failed: %s", exc)
        return baseline
    if not parsed:
        return baseline
    refined = _merge_llm_route(baseline, parsed)
    if refined.kind == baseline.kind and refined.recommended_tools == baseline.recommended_tools:
        return baseline
    return refined
