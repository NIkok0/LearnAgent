from __future__ import annotations

from langchain_core.messages import SystemMessage

from copilot_agent.context.constants import RAG_PRERETRIEVAL_PREFIX
from copilot_agent.context.retrieval import enrich_retrieval_payload
from copilot_agent.rag.request_context import build_retrieval_request, retrieval_defaults_from_scenario
from copilot_agent.memory import MemoryManager
from copilot_agent.rag.context_guard import build_guarded_context
from copilot_agent.rag.schema import DocChunk, dynamic_search_top_k
from copilot_agent.scenario.router.types import ToolRoute
from copilot_agent.settings import settings


def should_preretrieve(route: ToolRoute) -> bool:
    if not settings.context_preretrieval_enabled:
        return False
    if "search_docs" not in route.recommended_tools:
        return False
    if route.kind in {"safety_reject", "dangerous_execute"}:
        return False
    return True


def preretrieve_budget_chars(*, total_budget: int) -> int:
    cap = int(settings.context_preretrieval_budget_chars or 0)
    if cap > 0:
        return min(cap, total_budget)
    return min(total_budget // 2, 4000)


def preretrieve_docs(
    memory: MemoryManager,
    *,
    query: str,
    route: ToolRoute,
    budget_chars: int,
    thread_id: str = "",
    retrieval_defaults: dict[str, object] | None = None,
) -> tuple[list[DocChunk], SystemMessage | None, list[dict[str, object]], dict[str, object]]:
    if not query.strip() or not should_preretrieve(route):
        return [], None, [], {"enabled": False}

    rag_budget = preretrieve_budget_chars(total_budget=budget_chars)
    top_k = dynamic_search_top_k(budget_chars=rag_budget, ceiling=6)
    user_id = memory.resolve_user_id(thread_id) if thread_id else "local_user"
    defaults = dict(retrieval_defaults or {})
    request = build_retrieval_request(
        query=query,
        ctx=defaults,
        user_id=user_id,
        purpose="preretrieval_context",
    )
    result, policy_result = memory.policy_aware_search_docs(request, top_k=top_k)
    hits = list(result.chunks)
    guarded = build_guarded_context(
        hits,
        max_chars=rag_budget,
        require_citations=settings.private_rag_require_citations,
    )
    hits = guarded.chunks
    excerpts = guarded.markdown
    if not excerpts.strip():
        return hits, None, [], {"enabled": True, "hits": len(hits), "excerpt_chars": 0}

    enrichment = enrich_retrieval_payload(hits, query=query)
    retrieved_context = [
        {
            "source": chunk.source,
            "start_line": chunk.start_line,
            "doc_type": chunk.doc_type,
            "heading_path": chunk.heading_path or chunk.section_title,
        }
        for chunk in hits
    ]
    message = SystemMessage(
        content=(
            f"{RAG_PRERETRIEVAL_PREFIX}\n"
            "Pre-retrieved documentation snippets for this turn (prefer over guessing; "
            "you may still call search_docs for follow-up):\n\n"
            f"{excerpts}"
        )
    )
    meta = {
        "enabled": True,
        "hits": len(hits),
        "excerpt_chars": len(excerpts),
        "sources": list({chunk.source for chunk in hits}),
        "retrieval_mode": str(getattr(result.route, "mode", "") or ""),
        "suggested_api_paths": enrichment.get("suggested_api_paths") or [],
        "policy_result": policy_result,
        "context_guard": guarded.audit_payload(),
    }
    return hits, message, retrieved_context, meta
