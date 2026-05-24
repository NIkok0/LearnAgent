from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from copilot_agent.contracts.final_answer import FinalAnswerModel
from copilot_agent.contracts.tool_data import CitationItem


def _parse_tool_payload(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    if isinstance(content, str):
        text = content.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _citations_from_tool_message(message: ToolMessage) -> list[CitationItem]:
    payload = _parse_tool_payload(message.content)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    raw_items = (
        (data.get("citations") if isinstance(data, dict) else None)
        or metadata.get("citations")
        or []
    )
    items: list[CitationItem] = []
    if not isinstance(raw_items, list):
        return items
    seen: set[str] = set()
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        source = str(entry.get("source_file") or "").strip()
        chunk_id = str(entry.get("chunk_id") or "").strip()
        key = chunk_id or source
        if not key or key in seen:
            continue
        seen.add(key)
        try:
            items.append(CitationItem.model_validate(entry))
        except Exception:
            if source:
                items.append(CitationItem(source_file=source))
    return items


def build_final_answer(
    *,
    answer: str,
    messages: list[BaseMessage] | None = None,
    route_kind: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> FinalAnswerModel:
    """Collect citations and tool usage from checkpoint messages for L7 output."""
    citations: list[CitationItem] = []
    tools_used: list[str] = []
    tool_evidence: list[dict[str, Any]] = []
    seen_citation: set[str] = set()
    seen_tools: set[str] = set()

    for message in messages or []:
        if isinstance(message, ToolMessage):
            name = str(getattr(message, "name", "") or "").strip()
            if name and name not in seen_tools:
                seen_tools.add(name)
                tools_used.append(name)
                tool_evidence.append(_tool_evidence_from_message(message, name=name))
            if name == "search_docs":
                for item in _citations_from_tool_message(message):
                    key = item.chunk_id or item.source_file
                    if key in seen_citation:
                        continue
                    seen_citation.add(key)
                    citations.append(item)

    merged_metadata = dict(metadata or {})
    citation_required = bool(merged_metadata.get("citation_required", route_kind in {"knowledge", "troubleshooting"}))
    evidence_count = len(citations)
    source_count = len({item.source_file for item in citations if item.source_file})
    citation_status = _citation_status(required=citation_required, evidence_count=evidence_count)
    warnings = list(merged_metadata.get("contract_warnings") or [])
    if citation_status == "missing":
        warnings.append("citation_required_but_missing")
    if not str(answer or "").strip():
        warnings.append("empty_answer")
    merged_metadata.setdefault("evidence_count", evidence_count)
    merged_metadata.setdefault("source_count", source_count)
    merged_metadata.setdefault("citation_required", citation_required)
    merged_metadata.setdefault("citation_status", citation_status)
    merged_metadata.setdefault("tool_evidence_count", len(tool_evidence))
    safety_status = str(merged_metadata.get("safety_status") or "unknown")
    output_guard_action = str(merged_metadata.get("output_guard_action") or "unknown")

    return FinalAnswerModel(
        contract_version=2,
        answer=str(answer or "").strip(),
        answer_format=str(merged_metadata.get("answer_format") or "text"),
        citations=citations,
        route_kind=route_kind,
        tools_used=tools_used,
        tool_evidence=tool_evidence,
        evidence_count=evidence_count,
        source_count=source_count,
        citation_required=citation_required,
        citation_status=citation_status,
        safety_status=safety_status,
        output_guard_action=output_guard_action,
        contract_warnings=warnings,
        metadata=merged_metadata,
    )


def extract_final_answer_text(messages: list[BaseMessage]) -> str:
    for message in reversed(messages or []):
        if isinstance(message, AIMessage):
            content = getattr(message, "content", "")
            if isinstance(content, str) and content.strip():
                return content.strip()
    return ""


def _tool_evidence_from_message(message: ToolMessage, *, name: str) -> dict[str, Any]:
    payload = _parse_tool_payload(message.content)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    citations = _citations_from_tool_message(message)
    return {
        "tool": name,
        "tool_call_id": str(getattr(message, "tool_call_id", "") or ""),
        "success": bool(payload.get("success", True)),
        "source_count": len({item.source_file for item in citations if item.source_file}),
        "citation_count": len(citations),
        "metadata": {
            key: metadata.get(key)
            for key in ("call_id", "status_code", "path", "method", "policy_trace_id")
            if key in metadata
        },
        "data_keys": sorted(data.keys()) if isinstance(data, dict) else [],
    }


def _citation_status(*, required: bool, evidence_count: int) -> str:
    if not required:
        return "not_required"
    if evidence_count > 0:
        return "satisfied"
    return "missing"
