"""Pydantic models for EventStore payload shapes (post-envelope flattening)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from copilot_agent.contracts.tool_data import ToolResultAuditEnvelope


class LooseEventPayload(BaseModel):
    """Lifecycle / memory / meta events with evolving fields."""

    model_config = ConfigDict(extra="allow")


class TokenPayload(BaseModel):
    text: str = ""


class ToolStartPayload(BaseModel):
    name: str
    call_id: str
    category: str = ""
    risk_level: str = ""
    requires_approval: bool = False
    arguments: dict[str, Any] = Field(default_factory=dict)
    sanitized_args: dict[str, Any] | None = None


class ToolEndPayload(BaseModel):
    name: str
    call_id: str
    result: ToolResultAuditEnvelope
    duration_ms: int | None = None
    success: bool = True
    error: str | None = None
    sanitized_result: dict[str, Any] | None = None


class RetrievalSourceItem(BaseModel):
    source_file: str
    section_title: str | None = None
    heading_path: str | None = None
    doc_type: str | None = None
    start_line: int = 0
    chunk_index: int = 0
    http_method: str | None = None
    http_path: str | None = None
    request_field_names: list[str] = Field(default_factory=list)
    error_codes: list[str] = Field(default_factory=list)


class RetrievalCompletedPayload(BaseModel):
    query: str
    sources: list[RetrievalSourceItem] = Field(default_factory=list)
    source_count: int = 0
    excerpt_chars: int = 0
    success: bool = True
    call_id: str | None = None
    error: str | None = None
    retrieval_mode: str | None = None
    retrieval_route: dict[str, Any] | None = None

    model_config = ConfigDict(extra="forbid")


class ToolDetailItem(BaseModel):
    name: str
    category: str = ""
    risk_level: str = ""


class MemoryRunSummaryPayload(BaseModel):
    summary_type: Literal["run"] = "run"
    goal: str = ""
    outcome: str = ""
    tools_used: list[str] = Field(default_factory=list)
    tool_details: list[ToolDetailItem] = Field(default_factory=list)
    key_outputs: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    source_event_ids: list[int] = Field(default_factory=list)
    eligible_for_thread: bool = False
    char_count: int | None = None

    model_config = ConfigDict(extra="allow")


class MemoryThreadSummaryPayload(BaseModel):
    summary_type: Literal["thread"] = "thread"
    recent_goals: list[str] = Field(default_factory=list)
    recent_outcomes: list[str] = Field(default_factory=list)
    tools_used: list[str] = Field(default_factory=list)
    open_items: list[str] = Field(default_factory=list)
    source_run_ids: list[str] = Field(default_factory=list)
    source_event_ids: list[int] = Field(default_factory=list)

    model_config = ConfigDict(extra="allow")


class ApprovalResolvedPayload(BaseModel):
    approved: bool = False
    model_config = ConfigDict(extra="allow")


class DonePayload(BaseModel):
    assistant_message: dict[str, Any] = Field(default_factory=dict)
    model_config = ConfigDict(extra="allow")


class ErrorPayload(BaseModel):
    error: str = ""
    model_config = ConfigDict(extra="allow")
