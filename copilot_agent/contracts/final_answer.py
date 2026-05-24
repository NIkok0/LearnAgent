from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from copilot_agent.contracts.tool_data import CitationItem

__all__ = ["FinalAnswerModel"]


class FinalAnswerModel(BaseModel):
    """Structured final assistant delivery (L7): NL answer + citations + optional metadata."""

    contract_version: int = 2
    answer: str = ""
    answer_format: str = "text"
    citations: list[CitationItem] = Field(default_factory=list)
    route_kind: str | None = None
    tools_used: list[str] = Field(default_factory=list)
    tool_evidence: list[dict[str, Any]] = Field(default_factory=list)
    evidence_count: int = 0
    source_count: int = 0
    citation_required: bool = False
    citation_status: str = "not_required"
    safety_status: str = "unknown"
    output_guard_action: str = "unknown"
    contract_warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
