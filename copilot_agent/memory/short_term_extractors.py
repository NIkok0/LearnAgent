from __future__ import annotations

from typing import Any

from copilot_agent.memory.item_schema import MemoryType
from copilot_agent.memory.short_term_seed import (
    MAX_ITEMS_PER_SECTION,
    MAX_TEXT_CHARS,
    bounded_text,
    event_ids,
    int_or_none,
    seed,
)
from copilot_agent.tools.audit import canonicalize_side_effect_path
from copilot_agent.tools.sanitize import sanitize_tool_payload


def final_answer_from_done(payload: dict[str, Any]) -> str:
    final_answer = payload.get("final_answer")
    if isinstance(final_answer, dict):
        answer = str(final_answer.get("answer") or "").strip()
        if answer:
            return bounded_text(answer, 1200)
    assistant_message = payload.get("assistant_message")
    if isinstance(assistant_message, dict):
        content = str(assistant_message.get("content") or "").strip()
        if content:
            return bounded_text(content, 1200)
    return ""


def tool_action(payload: dict[str, Any], *, event_id: int | None) -> dict[str, Any] | None:
    name = str(payload.get("name") or "").strip()
    if not name:
        return None
    success = bool(payload.get("success", True))
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    path = canonicalize_side_effect_path(str(data.get("path") or metadata.get("path") or ""))
    action = {
        "kind": "tool",
        "tool": name,
        "success": success,
        "status_code": int_or_none(metadata.get("status_code") or data.get("status_code")),
        "path": path,
        "source_event_ids": event_ids(event_id),
    }
    if not success:
        action["error_type"] = str(payload.get("error_type") or "")
    return sanitize_tool_payload(action, max_string_length=MAX_TEXT_CHARS)


def tool_seed(action: dict[str, Any], *, event_id: int | None) -> dict[str, Any] | None:
    if action.get("success") is not True:
        return None
    tool = str(action.get("tool") or "")
    path = str(action.get("path") or "")
    if tool == "search_docs":
        return None
    if not path and tool not in {"http_get", "http_post"}:
        return None
    content = f"Tool {tool} succeeded"
    if path:
        content += f" for {path}"
    return seed(
        content=content,
        memory_type=MemoryType.FACT,
        importance=0.62,
        confidence=0.76,
        source_kind="tool_end",
        source_event_ids=event_ids(event_id),
        reusable=bool(path),
        ttl_days=30,
    )


def retrieval_sources(payload: dict[str, Any], *, event_id: int | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    sources = payload.get("sources")
    if not isinstance(sources, list):
        return rows
    for source in sources[:MAX_ITEMS_PER_SECTION]:
        if not isinstance(source, dict):
            continue
        rows.append(
            {
                "source_file": str(source.get("source_file") or ""),
                "chunk_index": int_or_none(source.get("chunk_index")),
                "http_path": canonicalize_side_effect_path(str(source.get("http_path") or "")),
                "source_event_ids": event_ids(event_id),
            }
        )
    return rows


def policy_decision(payload: dict[str, Any], *, event_id: int | None) -> dict[str, Any] | None:
    scope = str(payload.get("scope") or "").strip()
    decision = str(payload.get("decision") or "").strip()
    if not scope or not decision:
        return None
    return sanitize_tool_payload(
        {
            "scope": scope,
            "source": str(payload.get("source") or ""),
            "decision": decision,
            "action": str(payload.get("action") or ""),
            "resource": canonicalize_side_effect_path(str(payload.get("resource") or "")),
            "reason": str(payload.get("reason") or ""),
            "risk_level": str(payload.get("risk_level") or ""),
            "requires_approval": bool(payload.get("requires_approval", False)),
            "policy_trace_id": str(payload.get("policy_trace_id") or ""),
            "source_event_ids": event_ids(event_id),
        },
        max_string_length=MAX_TEXT_CHARS,
    )


def policy_seed(decision: dict[str, Any], *, outcome: str, event_id: int | None) -> dict[str, Any] | None:
    decision_value = str(decision.get("decision") or "")
    if decision_value not in {"deny", "block", "redact"}:
        return None
    resource = str(decision.get("resource") or "")
    reason = str(decision.get("reason") or decision_value)
    content = f"Policy {decision_value} decision"
    if resource:
        content += f" for {resource}"
    if reason:
        content += f": {reason}"
    return seed(
        content=content,
        memory_type=MemoryType.FACT,
        importance=0.7,
        confidence=0.82,
        source_kind="policy_decision",
        source_event_ids=event_ids(event_id),
        reusable=True,
        ttl_days=30 if outcome == "completed" else 14,
    )


def side_effect_action(payload: dict[str, Any], *, event_id: int | None) -> dict[str, Any] | None:
    status = str(payload.get("side_effect_status") or "").strip()
    if not status:
        return None
    return sanitize_tool_payload(
        {
            "kind": "side_effect",
            "tool": str(payload.get("tool_name") or ""),
            "status": status,
            "approval_status": str(payload.get("approval_status") or ""),
            "path": canonicalize_side_effect_path(str(payload.get("path") or "")),
            "status_code": int_or_none(payload.get("status_code")),
            "idempotency_reused": bool(payload.get("idempotency_reused", False)),
            "source_event_ids": event_ids(event_id),
        },
        max_string_length=MAX_TEXT_CHARS,
    )


def side_effect_seed(action: dict[str, Any], *, event_id: int | None) -> dict[str, Any] | None:
    status = str(action.get("status") or "")
    if status not in {"confirmed", "reused", "blocked"}:
        return None
    content = f"Side effect {status}"
    path = str(action.get("path") or "")
    if path:
        content += f" for {path}"
    return seed(
        content=content,
        memory_type=MemoryType.FACT,
        importance=0.72 if status == "blocked" else 0.64,
        confidence=0.84,
        source_kind="tool_side_effect",
        source_event_ids=event_ids(event_id),
        reusable=status == "blocked",
        ttl_days=30,
    )


def rag_artifact(event_type: str, payload: dict[str, Any], *, event_id: int | None) -> dict[str, Any] | None:
    doc_id = str(payload.get("doc_id") or "").strip()
    source_file = str(payload.get("source_file") or "").strip()
    if not doc_id and not source_file:
        return None
    return sanitize_tool_payload(
        {
            "kind": event_type,
            "doc_id": doc_id,
            "source_file": source_file,
            "tenant_id": str(payload.get("tenant_id") or ""),
            "classification": str(payload.get("classification") or ""),
            "pii_level": str(payload.get("pii_level") or ""),
            "chunk_count": int_or_none(payload.get("chunk_count") or payload.get("deleted_chunk_count")),
            "source_hash": str(payload.get("source_hash") or ""),
            "source_event_ids": event_ids(event_id),
        },
        max_string_length=MAX_TEXT_CHARS,
    )


def rag_seed(artifact: dict[str, Any], *, event_id: int | None) -> dict[str, Any] | None:
    kind = str(artifact.get("kind") or "")
    source_file = str(artifact.get("source_file") or artifact.get("doc_id") or "")
    if not source_file:
        return None
    if kind == "rag_document_ingested":
        content = f"RAG document ingested: {source_file}"
    elif kind in {"rag_document_deleted", "rag_document_delete_proof"}:
        content = f"RAG document deletion recorded: {source_file}"
    else:
        return None
    return seed(
        content=content,
        memory_type=MemoryType.FACT,
        importance=0.66,
        confidence=0.82,
        source_kind=kind,
        source_event_ids=event_ids(event_id),
        reusable=True,
        ttl_days=30,
    )


__all__ = [
    "final_answer_from_done",
    "policy_decision",
    "policy_seed",
    "rag_artifact",
    "rag_seed",
    "retrieval_sources",
    "side_effect_action",
    "side_effect_seed",
    "tool_action",
    "tool_seed",
]
