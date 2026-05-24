from __future__ import annotations

from copilot_agent.contracts.tool_data import CitationItem
from copilot_agent.rag.schema import DocChunk


def citations_from_chunks(chunks: list[DocChunk]) -> list[CitationItem]:
    items: list[CitationItem] = []
    for chunk in chunks:
        items.append(
            CitationItem(
                source_file=chunk.source,
                heading_path=chunk.heading_path or chunk.section_title or "",
                start_line=int(chunk.start_line),
                chunk_id=chunk.chunk_id,
                authority=int(getattr(chunk, "authority", 50) or 50),
            )
        )
    return items
