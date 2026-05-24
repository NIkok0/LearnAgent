from __future__ import annotations

from copilot_agent.contracts.events.payloads import RetrievalCompletedPayload, RetrievalSourceItem
from copilot_agent.contracts.retrieval import RetrievalResult
from copilot_agent.rag.schema import DocChunk


def section_title_from_chunk(text: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()[:240] or None
    return None


def build_retrieval_completed_payload(
    query: str,
    hits: list[DocChunk],
    *,
    excerpt_chars: int,
    call_id: str | None = None,
    success: bool = True,
    error: str | None = None,
    retrieval_mode: str | None = None,
    retrieval_route: dict[str, object] | None = None,
    policy_result: RetrievalResult | None = None,
    context_guard: dict[str, object] | None = None,
) -> dict[str, object]:
    """Payload for ``retrieval_completed`` EventStore / Timeline rows."""
    sources = []
    for index, chunk in enumerate(hits):
        item = RetrievalSourceItem(
            source_file=chunk.source,
            section_title=chunk.section_title or section_title_from_chunk(chunk.text),
            heading_path=chunk.heading_path or None,
            doc_type=chunk.doc_type if chunk.doc_type and chunk.doc_type != "doc" else None,
            start_line=chunk.start_line,
            chunk_index=chunk.chunk_index if chunk.chunk_index else index,
            authority=int(getattr(chunk, "authority", 50) or 50),
        )
        if chunk.api_endpoint is not None:
            item = item.model_copy(
                update={
                    "http_method": chunk.api_endpoint.method,
                    "http_path": chunk.api_endpoint.path,
                }
            )
        if chunk.request_fields:
            item = item.model_copy(
                update={"request_field_names": [field.name for field in chunk.request_fields if field.name]}
            )
        if chunk.error_codes:
            item = item.model_copy(
                update={"error_codes": [code.code for code in chunk.error_codes if code.code]}
            )
        sources.append(item)
    policy_payload = policy_result.audit_payload() if policy_result is not None else {}
    payload = RetrievalCompletedPayload(
        query=query,
        sources=sources,
        source_count=len(sources),
        excerpt_chars=excerpt_chars,
        success=success,
        call_id=call_id,
        error=error,
        retrieval_mode=retrieval_mode,
        retrieval_route=retrieval_route,
        **policy_payload,
        context_guard=dict(context_guard or {}),
    )
    return payload.model_dump(exclude_none=True)
