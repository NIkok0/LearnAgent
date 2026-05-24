"""Typed tool result data shapes (LLM-facing `data` and audit nesting)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ToolResultAuditEnvelope(BaseModel):
    """Nested `result` object inside tool_end EventStore payloads."""

    success: bool
    data: dict[str, Any] | list[Any] | str | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    sanitized: bool = True
    sanitized_args: dict[str, Any] | None = None
    sanitized_result: dict[str, Any] | None = None


class ApiPathHintData(BaseModel):
    method: str
    path: str
    path_template: str = ""
    source_file: str = ""
    heading_path: str = ""
    score: float = 1.0


class CitationItem(BaseModel):
    source_file: str
    heading_path: str | None = None
    start_line: int = 0
    chunk_id: str = ""
    authority: int | None = None


class SearchDocsToolData(BaseModel):
    excerpts_markdown: str | None = None
    sources: list[str] = Field(default_factory=list)
    citations: list[CitationItem] = Field(default_factory=list)
    suggested_api_paths: list[ApiPathHintData] = Field(default_factory=list)


class HttpToolData(BaseModel):
    """HTTP tool body fields vary by path; allow extension keys."""

    model_config = ConfigDict(extra="allow")

    status_code: int | None = None
    body: Any = None
    path: str | None = None
    method: str | None = None
