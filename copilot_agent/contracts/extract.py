from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from copilot_agent.rag.schema import ApiField


class ExtractedField(BaseModel):
    name: str
    field_type: str = "string"
    required: bool = False
    description: str = ""

    model_config = ConfigDict(extra="forbid")


class ExtractedRecord(BaseModel):
    """Unified extraction output for RAG API parse and Memory LLM/rule extractors."""

    source: Literal["rag_api", "memory_llm", "memory_rule"]
    record_type: Literal["api_fields", "memory_item"]
    fields: list[ExtractedField] = Field(default_factory=list)
    content: str | None = None
    memory_type: str | None = None
    scope: str | None = None
    importance: float | None = None
    confidence: float | None = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("importance", "confidence")
    @classmethod
    def _clamp_unit_float(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return max(0.0, min(1.0, float(value)))


class ExtractValidationError(ValueError):
    """Raised when an extracted payload fails contract validation."""


def validate_api_fields(fields: list[ApiField], *, endpoint_path: str | None = None) -> ExtractedRecord:
    converted = [
        ExtractedField(
            name=field.name,
            field_type=field.field_type,
            required=field.required,
            description=field.description,
        )
        for field in fields
        if field.name
    ]
    record = ExtractedRecord(
        source="rag_api",
        record_type="api_fields",
        fields=converted,
        content=endpoint_path,
    )
    try:
        return ExtractedRecord.model_validate(record.model_dump())
    except ValidationError as exc:
        raise ExtractValidationError(str(exc)) from exc


def validate_memory_candidate(raw: dict[str, Any], *, extractor: str = "rule") -> ExtractedRecord:
    source: Literal["memory_llm", "memory_rule"] = "memory_llm" if extractor == "llm" else "memory_rule"
    content = str(raw.get("content", "")).strip()
    if not content:
        raise ExtractValidationError("memory candidate content is required")
    memory_type = _parse_memory_type(raw.get("memory_type") or raw.get("type"))
    scope = _parse_scope(raw.get("scope"))
    importance = _optional_float(raw.get("importance"), default=0.6)
    confidence = _optional_float(raw.get("confidence"), default=0.75)
    record = ExtractedRecord(
        source=source,
        record_type="memory_item",
        content=content[:400],
        memory_type=memory_type,
        scope=scope,
        importance=importance,
        confidence=confidence,
    )
    try:
        return ExtractedRecord.model_validate(record.model_dump())
    except ValidationError as exc:
        raise ExtractValidationError(str(exc)) from exc


def _optional_float(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, parsed))


def _parse_memory_type(value: Any) -> str:
    raw = str(value or "fact").strip().lower()
    mapping = {
        "fact": "fact",
        "preference": "preference",
        "behavior": "behavior",
        "task_summary": "task_summary",
        "task": "task_summary",
    }
    return mapping.get(raw, "fact")


def _parse_scope(value: Any) -> str:
    raw = str(value or "session").strip().lower()
    return "user" if raw == "user" else "session"
