"""Map event kind -> payload model and validate stored JSON."""

from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from copilot_agent.contracts.events.payloads import (
    ApprovalResolvedPayload,
    ContextBuiltPayload,
    CredentialBindingAuditPayload,
    DonePayload,
    ErrorPayload,
    LlmGenerationPayload,
    LooseEventPayload,
    MemoryRunSummaryPayload,
    MemoryThreadSummaryPayload,
    OutputGuardCheckedPayload,
    RetrievalCompletedPayload,
    TokenPayload,
    ToolEndPayload,
    ToolStartPayload,
)
from copilot_agent.runtime.event_schema import (
    EVENT_APPROVAL_REQUIRED,
    EVENT_APPROVAL_RESOLVED,
    EVENT_ASSISTANT_STATE,
    EVENT_CANCEL_REQUESTED,
    EVENT_CANCELLED,
    EVENT_CHECKPOINT_CONSISTENCY_CHECKED,
    EVENT_CONTEXT_BUILT,
    EVENT_CREDENTIAL_BINDING_AUDIT,
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_LLM_GENERATION,
    EVENT_MEMORY_RUN_SUMMARY,
    EVENT_MEMORY_THREAD_SUMMARY,
    EVENT_OUTPUT_GUARD_CHECKED,
    EVENT_PLAN_CREATED,
    EVENT_RAG_DOCUMENT_DELETE_PROOF,
    EVENT_RAG_DOCUMENT_DELETED,
    EVENT_RAG_DOCUMENT_INGESTED,
    EVENT_RETRIEVAL_COMPLETED,
    EVENT_RUN_CHECKPOINT_META,
    EVENT_RUN_CONSISTENCY_CHECKED,
    EVENT_RUN_COMPLETED_META,
    EVENT_RUN_CREATED,
    EVENT_RUN_FAILED_META,
    EVENT_RUN_STARTED,
    EVENT_THREAD_CHECKPOINT_PURGED,
    EVENT_TOKEN,
    EVENT_TOOL_END,
    EVENT_TOOL_SIDE_EFFECT_RECORDED,
    EVENT_TOOL_START,
)

T = TypeVar("T", bound=BaseModel)


class PayloadValidationError(ValueError):
    """Raised when a stored event payload fails Pydantic validation."""


_LOOSE_KINDS = frozenset(
    {
        EVENT_RUN_CREATED,
        EVENT_RUN_STARTED,
        EVENT_ASSISTANT_STATE,
        EVENT_APPROVAL_REQUIRED,
        EVENT_RUN_CHECKPOINT_META,
        EVENT_RUN_CONSISTENCY_CHECKED,
        EVENT_CHECKPOINT_CONSISTENCY_CHECKED,
        EVENT_RUN_COMPLETED_META,
        EVENT_RUN_FAILED_META,
        EVENT_THREAD_CHECKPOINT_PURGED,
        EVENT_PLAN_CREATED,
        EVENT_RAG_DOCUMENT_INGESTED,
        EVENT_RAG_DOCUMENT_DELETED,
        EVENT_RAG_DOCUMENT_DELETE_PROOF,
        EVENT_TOOL_SIDE_EFFECT_RECORDED,
        EVENT_CANCEL_REQUESTED,
        EVENT_CANCELLED,
    }
)

_STRICT_MODELS: dict[str, type[BaseModel]] = {
    EVENT_TOKEN: TokenPayload,
    EVENT_TOOL_START: ToolStartPayload,
    EVENT_TOOL_END: ToolEndPayload,
    EVENT_RETRIEVAL_COMPLETED: RetrievalCompletedPayload,
    EVENT_CONTEXT_BUILT: ContextBuiltPayload,
    EVENT_CREDENTIAL_BINDING_AUDIT: CredentialBindingAuditPayload,
    EVENT_OUTPUT_GUARD_CHECKED: OutputGuardCheckedPayload,
    EVENT_LLM_GENERATION: LlmGenerationPayload,
    EVENT_MEMORY_RUN_SUMMARY: MemoryRunSummaryPayload,
    EVENT_MEMORY_THREAD_SUMMARY: MemoryThreadSummaryPayload,
    EVENT_APPROVAL_RESOLVED: ApprovalResolvedPayload,
    EVENT_DONE: DonePayload,
    EVENT_ERROR: ErrorPayload,
}


def strip_schema_version(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if k != "schema_version"}


def validate_payload_for_kind(kind: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    """Validate and normalize payload; re-attach schema_version if present in input."""
    raw = dict(payload or {})
    schema_version = raw.get("schema_version")
    body = strip_schema_version(raw)

    model: type[BaseModel]
    if kind in _STRICT_MODELS:
        model = _STRICT_MODELS[kind]
    elif kind in _LOOSE_KINDS:
        model = LooseEventPayload
    else:
        raise PayloadValidationError(f"no payload validator registered for kind: {kind}")

    try:
        parsed = model.model_validate(body)
    except ValidationError as exc:
        raise PayloadValidationError(f"{kind}: {exc}") from exc

    out = parsed.model_dump(exclude_none=True)
    if schema_version is not None:
        out["schema_version"] = schema_version
    return out


def payload_model_for_kind(kind: str) -> type[BaseModel] | None:
    if kind in _STRICT_MODELS:
        return _STRICT_MODELS[kind]
    if kind in _LOOSE_KINDS:
        return LooseEventPayload
    return None
