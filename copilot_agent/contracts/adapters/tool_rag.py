from __future__ import annotations

from typing import Any

from copilot_agent.contracts.events.retrieval import build_retrieval_completed_payload
from copilot_agent.contracts.retrieval import RetrievalResult
from copilot_agent.contracts.tool_result import ToolResultModel
from copilot_agent.rag.schema import DocChunk


class RagSearchAdapter:
    """Convert search_docs handler output to ToolResultModel and retrieval events."""

    @staticmethod
    def to_tool_result(
        raw: dict[str, Any],
        *,
        duration_ms: int | None = None,
    ) -> ToolResultModel:
        return ToolResultModel.from_search_docs(raw, duration_ms=duration_ms)

    @staticmethod
    def to_retrieval_completed_payload(
        query: str,
        hits: list[DocChunk],
        *,
        excerpt_chars: int,
        call_id: str | None = None,
        retrieval_mode: str | None = None,
        retrieval_route: dict[str, object] | None = None,
        policy_result: RetrievalResult | None = None,
        context_guard: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        return build_retrieval_completed_payload(
            query,
            hits,
            excerpt_chars=excerpt_chars,
            call_id=call_id,
            retrieval_mode=retrieval_mode,
            retrieval_route=retrieval_route,
            policy_result=policy_result,
            context_guard=context_guard,
        )
