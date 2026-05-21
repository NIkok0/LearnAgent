from __future__ import annotations

from copilot_agent.rag.api_paths import extract_api_paths
from copilot_agent.rag.schema import DocChunk
from copilot_agent.settings import settings


def enrich_retrieval_payload(
    hits: list[DocChunk],
    *,
    query: str,
) -> dict[str, object]:
    """Build RAG tool payload enrichments (path hints, API field metadata)."""
    suggested_api_paths: list[dict[str, object]] = []
    api_field_hints: list[dict[str, object]] = []
    if not settings.agent_retrieval_path_inject:
        return {
            "suggested_api_paths": suggested_api_paths,
            "api_field_hints": api_field_hints,
        }
    suggested_api_paths = [hint.as_dict() for hint in extract_api_paths(hits, query=query)]
    for chunk in hits:
        if not chunk.request_fields and chunk.api_endpoint is None and not chunk.error_codes:
            continue
        api_field_hints.append(
            {
                "source_file": chunk.source,
                "http_method": chunk.api_endpoint.method if chunk.api_endpoint else None,
                "http_path": chunk.api_endpoint.path if chunk.api_endpoint else None,
                "request_fields": [field.name for field in chunk.request_fields],
                "error_codes": [code.code for code in chunk.error_codes],
            }
        )
    return {
        "suggested_api_paths": suggested_api_paths,
        "api_field_hints": api_field_hints,
    }
