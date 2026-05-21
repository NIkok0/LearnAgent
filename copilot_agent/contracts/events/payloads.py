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
    timeout_seconds: float | None = None
    max_retries: int | None = None
    idempotency_key: str | None = None


class ToolEndPayload(BaseModel):
    name: str
    call_id: str
    result: ToolResultAuditEnvelope
    duration_ms: int | None = None
    success: bool = True
    error: str | None = None
    sanitized_result: dict[str, Any] | None = None
    retry_count: int | None = None
    timeout_seconds: float | None = None
    idempotency_key: str | None = None


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
    tenant_id: str | None = None
    user_id: str | None = None
    purpose: str | None = None
    query_hash: str | None = None
    max_classification: str | None = None
    retrieved_chunk_ids: list[str] = Field(default_factory=list)
    allowed_chunk_ids: list[str] = Field(default_factory=list)
    blocked_chunk_ids: list[str] = Field(default_factory=list)
    blocked_count: int = 0
    prefilter_blocked_chunk_ids: list[str] = Field(default_factory=list)
    prefilter_blocked_count: int = 0
    policy_trace_id: str | None = None
    policy_decisions: list[dict[str, Any]] = Field(default_factory=list)
    context_guard: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")


class ContextBuiltPayload(BaseModel):
    user_message_chars: int = 0
    assembled_message_count: int = 0
    budget_max_chars: int = 0
    used_chars: int = 0
    truncated: bool = False
    truncation_steps: list[str] = Field(default_factory=list)
    router_injected: bool = False
    preretrieval_enabled: bool = False
    preretrieval_sources: list[str] = Field(default_factory=list)
    preretrieval_excerpt_chars: int = 0
    memory_inject_chars: int = 0
    checkpoint_compacted: bool = False
    checkpoint_chars: int = 0

    model_config = ConfigDict(extra="forbid")


class CredentialBindingAuditPayload(BaseModel):
    action: Literal["scope_allowed", "scope_denied", "credential_set", "credential_read_denied"]
    binding_id: str
    provider: str = "scenario"
    credential_type: str = "cookie"
    granted_scopes: list[str] = Field(default_factory=list)
    required_scopes: list[str] = Field(default_factory=list)
    tool_name: str = ""
    reason: str = ""
    user_id: str = ""

    model_config = ConfigDict(extra="forbid")


class OutputGuardCheckedPayload(BaseModel):
    guard: str = "private_rag_output_v1"
    safe: bool = True
    action: str = "allow"
    finding_count: int = 0
    findings: list[str] = Field(default_factory=list)
    original_chars: int = 0
    emitted_chars: int = 0

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
