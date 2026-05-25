from __future__ import annotations

import hashlib
import uuid
from typing import Any

from copilot_agent.runtime.event_schema import EVENT_POLICY_DECISION_RECORDED
from copilot_agent.tools.audit import canonicalize_side_effect_path
from copilot_agent.tools.sanitize import sanitize_tool_payload

POLICY_SCOPES = {"tool", "route", "credential", "rag", "output_guard"}
POLICY_DECISIONS = {"allow", "ask", "deny", "block", "redact"}


def build_policy_decision_payload(
    *,
    scope: str,
    source: str,
    action: str,
    decision: str,
    reason: str = "",
    subject: str = "",
    resource: str = "",
    risk_level: str = "",
    requires_approval: bool = False,
    related_call_id: str | None = None,
    related_event_id: int | None = None,
    policy_trace_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_scope = scope if scope in POLICY_SCOPES else "tool"
    resolved_decision = decision if decision in POLICY_DECISIONS else "deny"
    return {
        "policy_trace_id": policy_trace_id or _new_trace_id(resolved_scope),
        "scope": resolved_scope,
        "source": str(source or "policy"),
        "subject": str(subject or ""),
        "action": str(action or ""),
        "resource": _safe_resource(resource),
        "decision": resolved_decision,
        "reason": str(reason or ""),
        "risk_level": str(risk_level or ""),
        "requires_approval": bool(requires_approval),
        "related_call_id": related_call_id,
        "related_event_id": related_event_id,
        "metadata": _safe_metadata(metadata or {}),
    }


def build_tool_policy_decision_payload(
    *,
    tool_name: str,
    call_id: str = "",
    decision: str,
    reason: str,
    source: str,
    risk_level: str = "",
    requires_approval: bool = False,
    path: str = "",
    metadata: dict[str, Any] | None = None,
    policy_trace_id: str | None = None,
) -> dict[str, Any]:
    safe_path = canonicalize_side_effect_path(path)
    safe_metadata = dict(metadata or {})
    if safe_path:
        safe_metadata.setdefault("path", safe_path)
    return build_policy_decision_payload(
        scope="tool",
        source=source,
        subject=str(tool_name or ""),
        action="tool_call",
        resource=safe_path,
        decision=decision,
        reason=reason,
        risk_level=risk_level,
        requires_approval=requires_approval,
        related_call_id=call_id or None,
        metadata=safe_metadata,
        policy_trace_id=policy_trace_id,
    )


def build_rag_policy_decision_payloads(
    retrieval_payload: dict[str, Any],
    *,
    related_event_id: int | None = None,
) -> list[dict[str, Any]]:
    trace_id = str(retrieval_payload.get("policy_trace_id") or "")
    out: list[dict[str, Any]] = []
    decisions = retrieval_payload.get("policy_decisions")
    if not isinstance(decisions, list):
        return out
    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        if decision.get("allowed") is True:
            continue
        metadata = {
            "tenant_id": decision.get("tenant_id") or retrieval_payload.get("tenant_id"),
            "doc_id": decision.get("doc_id"),
            "chunk_id": decision.get("chunk_id"),
            "classification": decision.get("classification"),
            "pii_level": decision.get("pii_level"),
            "purpose": retrieval_payload.get("purpose"),
            "query_hash": retrieval_payload.get("query_hash"),
            "max_classification": retrieval_payload.get("max_classification"),
        }
        out.append(
            build_policy_decision_payload(
                scope="rag",
                source="rag_policy_filter",
                subject=str(retrieval_payload.get("user_id") or ""),
                action="retrieve_chunk",
                resource=str(decision.get("chunk_id") or decision.get("doc_id") or ""),
                decision="block",
                reason=str(decision.get("reason") or "rag_policy_blocked"),
                risk_level=str(decision.get("classification") or ""),
                requires_approval=False,
                related_call_id=str(retrieval_payload.get("call_id") or "") or None,
                related_event_id=related_event_id,
                policy_trace_id=trace_id or None,
                metadata=metadata,
            )
        )
    return out


def build_output_guard_policy_decision_payload(
    guard_payload: dict[str, Any],
    *,
    related_event_id: int | None = None,
    policy_trace_id: str | None = None,
) -> dict[str, Any] | None:
    if guard_payload.get("safe") is True:
        return None
    action = str(guard_payload.get("action") or "")
    decision = "redact" if action in {"degrade", "audit_only"} else "block"
    return build_policy_decision_payload(
        scope="output_guard",
        source=str(guard_payload.get("guard") or "output_guard"),
        subject="assistant_output",
        action="emit_final_answer",
        resource="final_answer",
        decision=decision,
        reason="sensitive_output_detected",
        risk_level="high",
        requires_approval=False,
        related_event_id=related_event_id,
        policy_trace_id=policy_trace_id,
        metadata={
            "guard_action": action,
            "finding_count": guard_payload.get("finding_count"),
            "findings": guard_payload.get("findings") if isinstance(guard_payload.get("findings"), list) else [],
            "original_chars": guard_payload.get("original_chars"),
            "emitted_chars": guard_payload.get("emitted_chars"),
        },
    )


def build_policy_read_model(run: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    decisions: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for event in _ordered_events(events):
        if str(event.get("type") or "") != EVENT_POLICY_DECISION_RECORDED:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        item = _policy_item(event, payload)
        decisions.append(item)
        decision = str(item.get("decision") or "")
        if decision == "block":
            warnings.append(
                {
                    "code": "policy_blocked",
                    "message": "policy blocked an action",
                    "event_id": item.get("event_id"),
                    "scope": item.get("scope"),
                    "reason": item.get("reason"),
                }
            )
        elif decision == "deny":
            warnings.append(
                {
                    "code": "policy_denied",
                    "message": "policy denied an action",
                    "event_id": item.get("event_id"),
                    "scope": item.get("scope"),
                    "reason": item.get("reason"),
                }
            )
        elif decision not in POLICY_DECISIONS:
            warnings.append(
                {
                    "code": "policy_unknown_decision",
                    "message": "policy decision used an unknown value",
                    "event_id": item.get("event_id"),
                    "decision": decision,
                }
            )
    return {
        "run": {
            "id": run.get("id"),
            "thread_id": run.get("thread_id"),
            "status": run.get("status"),
        },
        "summary": _summary(decisions),
        "policy_decisions": decisions,
        "warnings": warnings,
    }


def _ordered_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        events,
        key=lambda event: (
            int(event.get("sequence") or 0),
            int(event.get("id", 0) or 0),
        ),
    )


def _policy_item(event: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": int(event.get("id", 0) or 0),
        "sequence": int(event.get("sequence", 0) or 0),
        "created_at": event.get("created_at"),
        "policy_trace_id": payload.get("policy_trace_id"),
        "scope": payload.get("scope"),
        "source": payload.get("source"),
        "subject": payload.get("subject"),
        "action": payload.get("action"),
        "resource": payload.get("resource"),
        "decision": payload.get("decision"),
        "reason": payload.get("reason"),
        "risk_level": payload.get("risk_level"),
        "requires_approval": bool(payload.get("requires_approval", False)),
        "related_call_id": payload.get("related_call_id"),
        "related_event_id": payload.get("related_event_id"),
        "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
    }


def _summary(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {decision: 0 for decision in sorted(POLICY_DECISIONS)}
    scopes: dict[str, int] = {}
    for item in decisions:
        decision = str(item.get("decision") or "")
        if decision in counts:
            counts[decision] += 1
        scope = str(item.get("scope") or "")
        if scope:
            scopes[scope] = scopes.get(scope, 0) + 1
    return {
        "total": len(decisions),
        **counts,
        "by_scope": scopes,
        "has_block_or_deny": counts["block"] > 0 or counts["deny"] > 0,
    }


def _safe_resource(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "?" in text or "://" in text:
        return canonicalize_side_effect_path(text)
    return text[:300]


def _safe_metadata(value: dict[str, Any]) -> dict[str, Any]:
    sanitized = sanitize_tool_payload(value)
    if not isinstance(sanitized, dict):
        return {}
    return _hash_query_values(sanitized)


def _hash_query_values(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower() == "query":
                out["query_hash"] = _hash_text(str(item or ""))
                continue
            out[str(key)] = _hash_query_values(item)
        return out
    if isinstance(value, list):
        return [_hash_query_values(item) for item in value]
    return value


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _new_trace_id(scope: str) -> str:
    return f"pol_{scope}_{uuid.uuid4().hex[:16]}"
