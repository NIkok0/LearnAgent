from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


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


@dataclass(frozen=True)
class MemoryItemRecord:
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
    history: list[dict[str, Any]] = field(default_factory=list)
    embedding: list[float] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "thread_id": self.thread_id,
            "scope": self.scope.value,
            "memory_type": self.memory_type.value,
            "content": self.content,
            "content_hash": self.content_hash,
            "importance": self.importance,
            "confidence": self.confidence,
            "version": self.version,
            "supersedes_id": self.supersedes_id,
            "is_deprecated": self.is_deprecated,
            "pending_confirmation": self.pending_confirmation,
            "expires_at": self.expires_at,
            "access_count": self.access_count,
            "last_accessed_at": self.last_accessed_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "source_run_id": self.source_run_id,
            "history": self.history,
        }


@dataclass
class MemoryWriteResult:
    action: str
    item: MemoryItemRecord | None = None
    superseded_id: str | None = None
    reason: str = ""


@dataclass
class RecalledMemoryItem:
    item: MemoryItemRecord
    score: float
    keyword_score: float
    time_factor: float
    vector_score: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.item.as_dict(),
            "score": round(self.score, 4),
            "keyword_score": round(self.keyword_score, 4),
            "time_factor": round(self.time_factor, 4),
            "vector_score": round(self.vector_score, 4),
        }
