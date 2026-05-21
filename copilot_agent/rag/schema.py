from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from pydantic import BaseModel, Field


def chunk_content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class ApiEndpoint(BaseModel):
    method: str
    path: str


class ApiField(BaseModel):
    name: str
    field_type: str = "string"
    required: bool = False
    description: str = ""


class ApiErrorCode(BaseModel):
    http_status: int
    code: str
    meaning: str = ""


class DocChunk(BaseModel):
    source: str
    start_line: int
    text: str
    section_title: str = ""
    heading_path: str = ""
    doc_type: str = "doc"
    chunk_index: int = 0
    updated_at: str = ""
    api_endpoint: ApiEndpoint | None = None
    request_fields: list[ApiField] = Field(default_factory=list)
    response_fields: list[ApiField] = Field(default_factory=list)
    error_codes: list[ApiErrorCode] = Field(default_factory=list)

    @property
    def key(self) -> tuple[str, int]:
        return (self.source, self.start_line)

    @property
    def chunk_id(self) -> str:
        return f"{self.source}:{self.start_line}:{chunk_content_hash(self.text)}"


def format_chunks_for_prompt(parts: list[DocChunk], max_chars: int = 12000) -> str:
    from copilot_agent.rag.fusion import dedup_chunks

    selected = select_chunks_for_budget(parts, max_chars=max_chars)
    blocks = []
    n = 0
    for p in selected:
        path = p.heading_path or p.section_title
        if path:
            location = f" | {path}"
        else:
            location = f" (line ~{p.start_line})"
        dtype = f" [{p.doc_type}]" if p.doc_type and p.doc_type != "doc" else ""
        endpoint = ""
        if p.api_endpoint is not None:
            endpoint = f" | {p.api_endpoint.method} {p.api_endpoint.path}"
        header = f"--- {p.source}{location}{dtype}{endpoint} ---\n"
        block = header + p.text
        if n + len(block) > max_chars:
            break
        blocks.append(block)
        n += len(block)
    return "\n\n".join(blocks)


def estimate_chunk_prompt_chars(chunk: DocChunk) -> int:
    path = chunk.heading_path or chunk.section_title
    if path:
        location = f" | {path}"
    else:
        location = f" (line ~{chunk.start_line})"
    dtype = f" [{chunk.doc_type}]" if chunk.doc_type and chunk.doc_type != "doc" else ""
    endpoint = ""
    if chunk.api_endpoint is not None:
        endpoint = f" | {chunk.api_endpoint.method} {chunk.api_endpoint.path}"
    header = f"--- {chunk.source}{location}{dtype}{endpoint} ---\n"
    return len(header) + len(chunk.text) + 2


def select_chunks_for_budget(parts: list[DocChunk], *, max_chars: int) -> list[DocChunk]:
    """Dynamic top-k: pack chunks until the context budget is reached."""
    from copilot_agent.rag.fusion import dedup_chunks

    selected: list[DocChunk] = []
    total = 0
    for chunk in dedup_chunks(parts):
        block_len = estimate_chunk_prompt_chars(chunk)
        if selected and total + block_len > max_chars:
            break
        if block_len > max_chars and not selected:
            selected.append(chunk)
            break
        selected.append(chunk)
        total += block_len
    return selected


def dynamic_search_top_k(*, budget_chars: int, avg_chunk_chars: int = 1400, ceiling: int = 8) -> int:
    if avg_chunk_chars <= 0:
        return max(1, min(ceiling, 6))
    return max(1, min(ceiling, budget_chars // avg_chunk_chars))
