from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class MemoryScope(StrEnum):
    USER = "user"
    SESSION = "session"
    GLOBAL = "global"


class MemoryType(StrEnum):
    FACT = "fact"
    PREFERENCE = "preference"
    BEHAVIOR = "behavior"
    TASK_SUMMARY = "task_summary"


LONG_TERM_MEMORY_PREFIX = "[LongTermMemory]"


class MemoryItemRecord(BaseModel):
    id: str
    user_id: str
    thread_id: str | None
    scope: MemoryScope
    memory_type: MemoryType
    content: str
    content_hash: str
    importance: float
    confidence: float
    version: int
    supersedes_id: str | None
    is_deprecated: bool
    expires_at: str | None
    access_count: int
    last_accessed_at: str | None
    created_at: str
    updated_at: str
    source_run_id: str | None
    pending_confirmation: bool = False
    history: list[dict[str, Any]] = Field(default_factory=list)
    embedding: list[float] | None = None

    model_config = ConfigDict(extra="forbid")

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"embedding"})


class MemoryWriteResult(BaseModel):
    action: str
    item: MemoryItemRecord | None = None
    superseded_id: str | None = None
    reason: str = ""
    pending_reason: str = ""

    model_config = ConfigDict(extra="forbid")

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class RecalledMemoryItem(BaseModel):
    item: MemoryItemRecord
    score: float
    keyword_score: float
    time_factor: float
    vector_score: float = 0.0
    type_boost: float = 0.0
    route_kind: str = ""
    aging_factor: float = 1.0
    confidence_factor: float = 1.0
    access_factor: float = 1.0

    model_config = ConfigDict(extra="forbid")

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.item.as_dict(),
            "score": round(self.score, 4),
            "keyword_score": round(self.keyword_score, 4),
            "time_factor": round(self.time_factor, 4),
            "vector_score": round(self.vector_score, 4),
            "type_boost": round(self.type_boost, 4),
            "route_kind": self.route_kind,
            "aging_factor": round(self.aging_factor, 4),
            "confidence_factor": round(self.confidence_factor, 4),
            "access_factor": round(self.access_factor, 4),
        }


class EpisodicInjectBundle(BaseModel):
    thread_summary: dict[str, Any] | None
    recalled_runs: list[dict[str, Any]]
    dropped_conflicts: list[dict[str, Any]] = Field(default_factory=list)
    dropped_long_term: list[dict[str, Any]] = Field(default_factory=list)
    recalled_long_term: list[dict[str, Any]] = Field(default_factory=list)
    inject_preview: str = ""
    budget_applied: dict[str, Any] = Field(default_factory=dict)
    sources: dict[str, list[Any]] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class MemoryContext(BaseModel):
    working: dict[str, Any]
    semantic: dict[str, Any]
    episodic: dict[str, Any]

    model_config = ConfigDict(extra="forbid")

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class CheckpointSummarySection(BaseModel):
    title: str
    items: list[str] = Field(default_factory=list)
    dropped_count: int = 0

    model_config = ConfigDict(extra="forbid")


class CheckpointCompactionSummary(BaseModel):
    summary_type: Literal["checkpoint_compaction"] = "checkpoint_compaction"
    format_version: Literal["structured_text_v1"] = "structured_text_v1"
    task_context: CheckpointSummarySection = Field(default_factory=lambda: CheckpointSummarySection(title="Task Context"))
    decisions_made: CheckpointSummarySection = Field(
        default_factory=lambda: CheckpointSummarySection(title="Decisions Made")
    )
    important_facts: CheckpointSummarySection = Field(
        default_factory=lambda: CheckpointSummarySection(title="Important Facts")
    )
    tool_results: CheckpointSummarySection = Field(default_factory=lambda: CheckpointSummarySection(title="Tool Results"))
    open_questions: CheckpointSummarySection = Field(
        default_factory=lambda: CheckpointSummarySection(title="Open Questions")
    )
    do_not_carry_forward: CheckpointSummarySection = Field(
        default_factory=lambda: CheckpointSummarySection(title="Do Not Carry Forward")
    )
    source_message_count: int = 0
    kept_recent_turns: int = 0
    summary_chars: int = 0

    model_config = ConfigDict(extra="forbid")

    def render_for_prompt(self, max_chars: int) -> str:
        sections = [
            self.task_context,
            self.decisions_made,
            self.important_facts,
            self.tool_results,
            self.open_questions,
            self.do_not_carry_forward,
        ]
        lines = ["Earlier conversation summary (structured):"]
        for section in sections:
            lines.append(f"{section.title}:")
            if section.items:
                lines.extend(f"- {item}" for item in section.items)
            else:
                lines.append("- none")
        return _truncate("\n".join(lines), max_chars)

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def _truncate(value: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."
