from __future__ import annotations

from collections import OrderedDict
from typing import Any


TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
LIFECYCLE_EVENTS = {
    "run_created",
    "run_started",
    "done",
    "error",
    "cancel_requested",
    "cancelled",
}
CHECKPOINT_EVENTS = {
    "run_checkpoint_meta",
    "run_completed_meta",
    "run_failed_meta",
    "run_consistency_checked",
    "checkpoint_consistency_checked",
    "thread_checkpoint_purged",
}
MEMORY_EVENTS = {
    "memory_run_summary",
    "memory_thread_summary",
    "checkpoint_compacted",
    "memory_item_confirmed",
    "memory_item_rejected",
    "memory_item_deleted",
    "memory_item_delete_proof",
}


class TimelineProjector:
    """Project raw EventStore events into a UI-oriented run timeline."""

    def project_run(self, run: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
        ordered_events = sorted(
            events,
            key=lambda event: (
                int(event.get("sequence") or 0),
                int(event.get("id", 0) or 0),
            ),
        )
        warnings: list[dict[str, Any]] = []
        items: list[dict[str, Any]] = []
        token_buffer: list[str] = []
        token_event_ids: list[int] = []
        tools: OrderedDict[str, dict[str, Any]] = OrderedDict()
        approval_pending: dict[str, Any] | None = None
        approval_index = 0
        checkpoint_items: list[dict[str, Any]] = []

        def flush_tokens() -> None:
            if not token_buffer:
                return
            text = "".join(token_buffer)
            items.append(
                {
                    "kind": "assistant_output",
                    "title": "Assistant output",
                    "text": text,
                    "preview": _preview(text, 240),
                    "event_ids": list(token_event_ids),
                }
            )
            token_buffer.clear()
            token_event_ids.clear()

        for event in ordered_events:
            event_type = str(event.get("type", ""))
            payload = _payload(event)
            event_id = int(event.get("id", 0) or 0)

            if event_type == "token":
                token_buffer.append(str(payload.get("text", "")))
                token_event_ids.append(event_id)
                continue

            flush_tokens()

            if event_type in LIFECYCLE_EVENTS:
                items.append(_lifecycle_item(event, payload))
                if event_type == "done" and isinstance(payload.get("final_answer"), dict):
                    final_answer = payload.get("final_answer") or {}
                    items.append(
                        {
                            "kind": "final_answer",
                            "title": "Final answer",
                            "event_id": event_id,
                            "created_at": event.get("created_at"),
                            "answer": final_answer.get("answer"),
                            "citation_count": len(final_answer.get("citations") or []),
                            "tools_used": final_answer.get("tools_used") or [],
                            "contract_version": final_answer.get("contract_version"),
                            "citation_required": final_answer.get("citation_required"),
                            "citation_status": final_answer.get("citation_status"),
                            "source_count": final_answer.get("source_count"),
                            "tool_evidence_count": len(final_answer.get("tool_evidence") or []),
                            "contract_warnings": final_answer.get("contract_warnings") or [],
                            "safety_status": final_answer.get("safety_status"),
                            "output_guard_action": final_answer.get("output_guard_action"),
                            "payload": final_answer,
                        }
                    )
                continue

            if event_type == "tool_start":
                call_id = _call_id(payload, event_id, warnings)
                tools[call_id] = {
                    "kind": "tool_call",
                    "title": str(payload.get("name") or "tool"),
                    "call_id": call_id,
                    "name": payload.get("name"),
                    "category": payload.get("category"),
                    "risk_level": payload.get("risk_level"),
                    "requires_approval": bool(payload.get("requires_approval", False)),
                    "arguments": payload.get("arguments", {}),
                    "start_event_id": event_id,
                    "started_at": event.get("created_at"),
                    "end_event_id": None,
                    "ended_at": None,
                    "result": None,
                    "duration_ms": None,
                    "success": None,
                    "error": None,
                }
                continue

            if event_type == "tool_end":
                call_id = _call_id(payload, event_id, warnings)
                item = tools.get(call_id)
                if item is None:
                    warnings.append(
                        {
                            "code": "tool_missing_start",
                            "message": "tool_end has no matching tool_start",
                            "event_id": event_id,
                            "call_id": call_id,
                        }
                    )
                    item = {
                        "kind": "tool_call",
                        "title": str(payload.get("name") or "tool"),
                        "call_id": call_id,
                        "name": payload.get("name"),
                        "category": payload.get("category"),
                        "risk_level": payload.get("risk_level"),
                        "requires_approval": bool(payload.get("requires_approval", False)),
                        "arguments": {},
                        "start_event_id": None,
                        "started_at": None,
                    }
                    tools[call_id] = item
                item.update(
                    {
                        "end_event_id": event_id,
                        "ended_at": event.get("created_at"),
                        "result": payload.get("result"),
                        "duration_ms": payload.get("duration_ms"),
                        "success": bool(payload.get("success", True)),
                        "error": payload.get("error"),
                    }
                )
                if item.get("success") is False:
                    warnings.append(
                        {
                            "code": "tool_failed",
                            "message": "tool call failed",
                            "event_id": event_id,
                            "call_id": call_id,
                            "tool": item.get("name"),
                        }
                    )
                continue

            if event_type == "approval_required":
                approval_pending = {
                    "kind": "approval",
                    "title": "Approval required",
                    "status": "waiting",
                    "required_event_id": event_id,
                    "resolved_event_id": None,
                    "requested_at": event.get("created_at"),
                    "resolved_at": None,
                    "required": payload,
                    "resolved": None,
                }
                approval_index = len(items)
                items.append(approval_pending)
                continue

            if event_type == "approval_resolved":
                if approval_pending is None or approval_pending.get("status") != "waiting":
                    approval_pending = {
                        "kind": "approval",
                        "title": "Approval resolved",
                        "status": "resolved",
                        "required_event_id": None,
                        "resolved_event_id": event_id,
                        "requested_at": None,
                        "resolved_at": event.get("created_at"),
                        "required": None,
                        "resolved": payload,
                    }
                    items.append(approval_pending)
                else:
                    approval_pending.update(
                        {
                            "title": "Approval resolved",
                            "status": "approved" if payload.get("approved") else "rejected",
                            "resolved_event_id": event_id,
                            "resolved_at": event.get("created_at"),
                            "resolved": payload,
                        }
                    )
                    items[approval_index] = approval_pending
                continue

            if event_type in CHECKPOINT_EVENTS:
                checkpoint_items.append(
                    {
                        "kind": "checkpoint",
                        "title": event_type,
                        "event_id": event_id,
                        "created_at": event.get("created_at"),
                        "payload": payload,
                    }
                )
                continue

            if event_type in {"plan_created", "plan_updated"}:
                plan_payload = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
                steps = plan_payload.get("steps") if isinstance(plan_payload.get("steps"), list) else []
                items.append(
                    {
                        "kind": "plan",
                        "title": event_type,
                        "event_id": event_id,
                        "created_at": event.get("created_at"),
                        "event_type": event_type,
                        "goal": payload.get("goal") or plan_payload.get("goal"),
                        "strategy": payload.get("strategy"),
                        "tool_route": payload.get("tool_route"),
                        "steps": [
                            {
                                "id": step.get("id"),
                                "goal": step.get("goal"),
                                "tool_hint": step.get("tool_hint"),
                                "status": step.get("status"),
                                "outcome": step.get("outcome"),
                            }
                            for step in steps
                            if isinstance(step, dict)
                        ],
                        "payload": payload,
                    }
                )
                continue

            if event_type == "output_guard_checked":
                items.append(
                    {
                        "kind": "output_guard",
                        "title": "Output guard checked",
                        "event_id": event_id,
                        "created_at": event.get("created_at"),
                        "safe": bool(payload.get("safe", True)),
                        "action": payload.get("action"),
                        "finding_count": int(payload.get("finding_count") or 0),
                        "payload": payload,
                    }
                )
                continue

            if event_type == "llm_generation":
                item = {
                    "kind": "observability",
                    "title": "LLM generation",
                    "event_id": event_id,
                    "created_at": event.get("created_at"),
                    "provider": payload.get("provider"),
                    "model": payload.get("model"),
                    "round_index": payload.get("round_index"),
                    "latency_ms": payload.get("latency_ms"),
                    "prompt_tokens": payload.get("prompt_tokens"),
                    "completion_tokens": payload.get("completion_tokens"),
                    "total_tokens": payload.get("total_tokens"),
                    "estimated_cost": payload.get("estimated_cost"),
                    "finish_reason": payload.get("finish_reason"),
                    "tool_call_count": payload.get("tool_call_count"),
                    "observability_provider": payload.get("observability_provider"),
                    "external_trace_url": payload.get("external_trace_url"),
                    "payload": payload,
                }
                items.append(item)
                continue

            if event_type == "policy_decision_recorded":
                decision = str(payload.get("decision") or "")
                items.append(
                    {
                        "kind": "policy",
                        "title": "Policy decision",
                        "event_id": event_id,
                        "created_at": event.get("created_at"),
                        "policy_trace_id": payload.get("policy_trace_id"),
                        "scope": payload.get("scope"),
                        "source": payload.get("source"),
                        "subject": payload.get("subject"),
                        "action": payload.get("action"),
                        "resource": payload.get("resource"),
                        "decision": decision,
                        "reason": payload.get("reason"),
                        "risk_level": payload.get("risk_level"),
                        "requires_approval": bool(payload.get("requires_approval", False)),
                        "related_call_id": payload.get("related_call_id"),
                        "related_event_id": payload.get("related_event_id"),
                        "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
                        "payload": payload,
                    }
                )
                if decision == "block":
                    warnings.append(
                        {
                            "code": "policy_blocked",
                            "message": "policy blocked an action",
                            "event_id": event_id,
                            "scope": payload.get("scope"),
                            "reason": payload.get("reason"),
                        }
                    )
                elif decision == "deny":
                    warnings.append(
                        {
                            "code": "policy_denied",
                            "message": "policy denied an action",
                            "event_id": event_id,
                            "scope": payload.get("scope"),
                            "reason": payload.get("reason"),
                        }
                    )
                continue

            if event_type == "tool_side_effect_recorded":
                side_effect_status = str(payload.get("side_effect_status") or "")
                items.append(
                    {
                        "kind": "side_effect",
                        "title": "Tool side effect",
                        "event_id": event_id,
                        "created_at": event.get("created_at"),
                        "tool_name": payload.get("tool_name"),
                        "call_id": payload.get("call_id"),
                        "method": payload.get("method"),
                        "path": payload.get("path"),
                        "risk_level": payload.get("risk_level"),
                        "requires_approval": bool(payload.get("requires_approval", False)),
                        "approval_status": payload.get("approval_status"),
                        "side_effect_status": side_effect_status,
                        "status_code": payload.get("status_code"),
                        "idempotency_key": payload.get("idempotency_key"),
                        "idempotency_reused": bool(payload.get("idempotency_reused", False)),
                        "compensatable": bool(payload.get("compensatable", False)),
                        "reason": payload.get("reason"),
                        "policy_trace_id": payload.get("policy_trace_id"),
                        "payload": payload,
                    }
                )
                if side_effect_status == "unknown":
                    warnings.append(
                        {
                            "code": "side_effect_unknown",
                            "message": "write tool side effect could not be confirmed",
                            "event_id": event_id,
                            "call_id": payload.get("call_id"),
                            "tool": payload.get("tool_name"),
                            "reason": payload.get("reason"),
                        }
                    )
                continue

            if event_type in {"assistant_state", "context_built"}:
                title = "Context assembled" if event_type == "context_built" else event_type
                items.append(
                    {
                        "kind": event_type,
                        "title": title,
                        "event_id": event_id,
                        "created_at": event.get("created_at"),
                        "payload": payload,
                    }
                )
                continue

            if event_type == "retrieval_completed":
                sources = payload.get("sources") if isinstance(payload.get("sources"), list) else []
                items.append(
                    {
                        "kind": "retrieval",
                        "title": "Document retrieval",
                        "event_id": event_id,
                        "created_at": event.get("created_at"),
                        "query": payload.get("query"),
                        "source_count": int(payload.get("source_count") or len(sources)),
                        "excerpt_chars": payload.get("excerpt_chars"),
                        "sources": sources,
                        "preview": _retrieval_preview(payload),
                        "success": bool(payload.get("success", True)),
                        "error": payload.get("error"),
                        "call_id": payload.get("call_id"),
                        "payload": payload,
                    }
                )
                continue

            if event_type in MEMORY_EVENTS:
                items.append(
                    {
                        "kind": "memory",
                        "title": event_type,
                        "derived": True,
                        "event_id": event_id,
                        "created_at": event.get("created_at"),
                        "payload": payload,
                    }
                )
                continue

            items.append(
                {
                    "kind": "event",
                    "title": event_type,
                    "event_id": event_id,
                    "created_at": event.get("created_at"),
                    "payload": payload,
                }
            )

        flush_tokens()

        for checkpoint_item in checkpoint_items:
            items.append(checkpoint_item)

        for tool in tools.values():
            if tool.get("end_event_id") is None:
                warnings.append(
                    {
                        "code": "tool_missing_end",
                        "message": "tool_start has no matching tool_end",
                        "event_id": tool.get("start_event_id"),
                        "call_id": tool.get("call_id"),
                        "tool": tool.get("name"),
                    }
                )
            items.append(tool)

        _append_status_warnings(run, ordered_events, warnings)
        _append_sequence_warnings(ordered_events, warnings)
        _append_checkpoint_consistency_warnings(checkpoint_items, warnings)
        warning_items = [
            {
                "kind": "warning",
                "title": warning["code"],
                "warning": warning,
            }
            for warning in warnings
        ]

        projected_items = _sort_items(items + warning_items)
        return {
            "status": run.get("status"),
            "items": projected_items,
            "warnings": warnings,
            "assistant_output": "".join(
                str(_payload(event).get("text", ""))
                for event in ordered_events
                if event.get("type") == "token"
            ),
            "event_count": len(ordered_events),
            "checkpoint": _checkpoint_summary(checkpoint_items),
            "observability": _observability_summary(ordered_events),
            "cost": _cost_summary(ordered_events),
            "debugger": _debugger_summary(run, ordered_events, projected_items, warnings),
        }


def _payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else {}


def _lifecycle_item(event: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    event_type = str(event.get("type", ""))
    return {
        "kind": "lifecycle",
        "title": event_type,
        "event_id": int(event.get("id", 0) or 0),
        "created_at": event.get("created_at"),
        "payload": payload,
    }


def _call_id(payload: dict[str, Any], event_id: int, warnings: list[dict[str, Any]]) -> str:
    raw_call_id = payload.get("call_id") or payload.get("tool_call_id")
    if raw_call_id:
        return str(raw_call_id)
    name = str(payload.get("name") or "tool")
    fallback = f"{name}:{event_id}"
    warnings.append(
        {
            "code": "tool_missing_call_id",
            "message": "tool event is missing call_id",
            "event_id": event_id,
            "tool": name,
        }
    )
    return fallback


def _append_sequence_warnings(events: list[dict[str, Any]], warnings: list[dict[str, Any]]) -> None:
    sequences = [int(event.get("sequence") or 0) for event in events if event.get("sequence") is not None]
    if len(sequences) >= 2:
        for index in range(1, len(sequences)):
            if sequences[index] != sequences[index - 1] + 1:
                warnings.append(
                    {
                        "code": "sequence_gap",
                        "message": "run event sequence is not strictly monotonic",
                        "previous": sequences[index - 1],
                        "current": sequences[index],
                    }
                )
                break
    seen_tool_end: set[str] = set()
    for event in events:
        if str(event.get("type", "")) != "tool_end":
            continue
        payload = _payload(event)
        call_id = str(payload.get("call_id") or "").strip()
        if not call_id:
            continue
        if call_id in seen_tool_end:
            warnings.append(
                {
                    "code": "duplicate_tool_end_call_id",
                    "message": "duplicate tool_end for the same call_id",
                    "call_id": call_id,
                    "event_id": int(event.get("id", 0) or 0),
                }
            )
        seen_tool_end.add(call_id)


def _append_status_warnings(
    run: dict[str, Any],
    events: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> None:
    event_types = {str(event.get("type", "")) for event in events}
    status = str(run.get("status", ""))
    if status == "completed" and "done" not in event_types:
        warnings.append({"code": "completed_without_done", "message": "run is completed but has no done event"})
    if status == "failed" and "error" not in event_types:
        warnings.append({"code": "failed_without_error_event", "message": "run is failed but has no error event"})
    if status == "cancelled" and "cancelled" not in event_types:
        warnings.append({"code": "cancelled_without_event", "message": "run is cancelled but has no cancelled event"})
    if "cancel_requested" in event_types and status not in TERMINAL_STATUSES and "cancelled" not in event_types:
        warnings.append(
            {
                "code": "cancel_requested_not_cancelled",
                "message": "cancel was requested but run is not terminal",
            }
        )


def _sort_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def item_order(item: dict[str, Any]) -> int:
        event_id = (
            item.get("event_id")
            or item.get("start_event_id")
            or item.get("required_event_id")
            or item.get("end_event_id")
            or 10**12
        )
        return int(event_id)

    return sorted(items, key=item_order)


def _checkpoint_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for item in items:
        payload = item.get("payload") or {}
        event_type = str(item.get("title", ""))
        if event_type == "run_completed_meta":
            summary["completed"] = payload
        elif event_type == "run_checkpoint_meta":
            summary["interrupt"] = payload
        elif event_type == "run_failed_meta":
            summary["failed"] = payload
        elif event_type == "run_consistency_checked":
            summary["consistency"] = payload
        elif event_type == "checkpoint_consistency_checked":
            summary["consistency_v2"] = payload
        elif event_type == "thread_checkpoint_purged":
            summary["purged"] = payload
    return summary


def _append_checkpoint_consistency_warnings(
    checkpoint_items: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> None:
    latest = next(
        (
            item
            for item in reversed(checkpoint_items)
            if str(item.get("title", "")) == "checkpoint_consistency_checked"
        ),
        None,
    )
    if latest is None:
        return
    payload = latest.get("payload") if isinstance(latest.get("payload"), dict) else {}
    event_id = latest.get("event_id")
    if payload.get("checkpoint_read_ok") is False:
        warnings.append(
            {
                "code": "checkpoint_read_failed",
                "message": "checkpoint consistency check could not read LangGraph checkpoint",
                "event_id": event_id,
                "error": payload.get("error"),
            }
        )
    if payload.get("checkpoint_missing") is True:
        warnings.append(
            {
                "code": "checkpoint_missing",
                "message": "checkpoint consistency check found no usable checkpoint snapshot",
                "event_id": event_id,
            }
        )
    if payload.get("checkpoint_match") is False:
        warnings.append(
            {
                "code": "checkpoint_message_count_mismatch",
                "message": "checkpoint message count does not match run_completed_meta",
                "event_id": event_id,
                "actual": payload.get("checkpoint_message_count_actual"),
                "reported": payload.get("checkpoint_message_count_reported"),
            }
        )


def _observability_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    llm_payloads = [
        _payload(event)
        for event in events
        if str(event.get("type", "")) == "llm_generation"
    ]
    completed_meta = next(
        (
            _payload(event)
            for event in reversed(events)
            if str(event.get("type", "")) == "run_completed_meta"
        ),
        {},
    )
    output_guard = next(
        (
            _payload(event)
            for event in reversed(events)
            if str(event.get("type", "")) == "output_guard_checked"
        ),
        {},
    )
    return {
        "llm_rounds": int(completed_meta.get("llm_rounds") or len(llm_payloads)),
        "total_prompt_tokens": int(completed_meta.get("prompt_tokens") or _sum_int(llm_payloads, "prompt_tokens")),
        "total_completion_tokens": int(
            completed_meta.get("completion_tokens") or _sum_int(llm_payloads, "completion_tokens")
        ),
        "total_tokens": int(completed_meta.get("total_tokens") or _sum_int(llm_payloads, "total_tokens")),
        "tool_count": int(completed_meta.get("tool_count") or _count_event_type(events, "tool_start")),
        "failed_tool_count": int(completed_meta.get("failed_tool_count") or _failed_tool_events(events)),
        "retrieval_count": int(completed_meta.get("retrieval_count") or _count_event_type(events, "retrieval_completed")),
        "output_guard_action": completed_meta.get("output_guard_action") or output_guard.get("action"),
        "trace_id": completed_meta.get("trace_id") or _first_payload_value(llm_payloads, "trace_id"),
        "observability_provider": completed_meta.get("observability_provider")
        or _first_payload_value(llm_payloads, "observability_provider"),
        "external_trace_url": completed_meta.get("external_trace_url")
        or _first_payload_value(llm_payloads, "external_trace_url"),
    }


def _cost_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    llm_payloads = [
        _payload(event)
        for event in events
        if str(event.get("type", "")) == "llm_generation"
    ]
    total = 0.0
    has_cost = False
    for payload in llm_payloads:
        value = payload.get("estimated_cost")
        if value is None:
            continue
        total += float(value)
        has_cost = True
    completed_meta = next(
        (
            _payload(event)
            for event in reversed(events)
            if str(event.get("type", "")) == "run_completed_meta"
        ),
        {},
    )
    if completed_meta.get("estimated_cost") is not None:
        total = float(completed_meta.get("estimated_cost") or 0)
        has_cost = True
    return {
        "estimated_cost": round(total, 8) if has_cost else None,
        "currency": "USD",
        "source": "static_price_table" if has_cost else "unmatched_model",
    }


def _debugger_summary(
    run: dict[str, Any],
    events: list[dict[str, Any]],
    items: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    event_types = [str(event.get("type", "")) for event in events]
    tool_items = [item for item in items if item.get("kind") == "tool_call"]
    approval_items = [item for item in items if item.get("kind") == "approval"]
    plan_items = [item for item in items if item.get("kind") == "plan"]
    final_answer_items = [item for item in items if item.get("kind") == "final_answer"]
    output_guard_items = [item for item in items if item.get("kind") == "output_guard"]
    side_effect_items = [item for item in items if item.get("kind") == "side_effect"]
    policy_items = [item for item in items if item.get("kind") == "policy"]
    memory_items = [item for item in items if item.get("kind") == "memory"]
    memory_governance_items = [
        item
        for item in memory_items
        if str(item.get("title") or "").startswith("memory_item_")
    ]
    observability = _observability_summary(events)
    cost = _cost_summary(events)
    checkpoint = _checkpoint_summary(
        [
            item
            for item in items
            if item.get("kind") == "checkpoint"
        ]
    )
    last_event_id = int(events[-1].get("id", 0) or 0) if events else None
    failed_meta = checkpoint.get("failed") if isinstance(checkpoint.get("failed"), dict) else {}
    consistency = checkpoint.get("consistency") if isinstance(checkpoint.get("consistency"), dict) else {}
    return {
        "run_id": run.get("id"),
        "thread_id": run.get("thread_id"),
        "status": run.get("status"),
        "event_count": len(events),
        "last_event_id": last_event_id,
        "last_successful_event_id": failed_meta.get("last_successful_event_id") or consistency.get("last_event_id"),
        "event_types": event_types,
        "tool_calls": {
            "total": len(tool_items),
            "failed": sum(1 for item in tool_items if item.get("success") is False),
            "missing_end": sum(1 for item in tool_items if item.get("end_event_id") is None),
        },
        "side_effects": {
            "total": len(side_effect_items),
            "unknown": sum(1 for item in side_effect_items if item.get("side_effect_status") == "unknown"),
        },
        "side_effect_count": len(side_effect_items),
        "unknown_side_effect_count": sum(
            1 for item in side_effect_items if item.get("side_effect_status") == "unknown"
        ),
        "policy_decisions": {
            "total": len(policy_items),
            "allow": sum(1 for item in policy_items if item.get("decision") == "allow"),
            "ask": sum(1 for item in policy_items if item.get("decision") == "ask"),
            "deny": sum(1 for item in policy_items if item.get("decision") == "deny"),
            "block": sum(1 for item in policy_items if item.get("decision") == "block"),
            "redact": sum(1 for item in policy_items if item.get("decision") == "redact"),
        },
        "policy_decision_count": len(policy_items),
        "policy_block_count": sum(1 for item in policy_items if item.get("decision") == "block"),
        "policy_ask_count": sum(1 for item in policy_items if item.get("decision") == "ask"),
        "policy_deny_count": sum(1 for item in policy_items if item.get("decision") == "deny"),
        "memory_governance": {
            "total": len(memory_governance_items),
            "confirmed": sum(1 for item in memory_governance_items if item.get("title") == "memory_item_confirmed"),
            "rejected": sum(1 for item in memory_governance_items if item.get("title") == "memory_item_rejected"),
            "deleted": sum(1 for item in memory_governance_items if item.get("title") == "memory_item_deleted"),
            "delete_proof": sum(
                1 for item in memory_governance_items if item.get("title") == "memory_item_delete_proof"
            ),
        },
        "memory_governance_count": len(memory_governance_items),
        "approval": {
            "count": len(approval_items),
            "waiting": any(item.get("status") == "waiting" for item in approval_items),
            "last_status": approval_items[-1].get("status") if approval_items else None,
        },
        "plan": {
            "count": len(plan_items),
            "step_count": sum(len(item.get("steps") or []) for item in plan_items),
            "last_event_type": plan_items[-1].get("event_type") if plan_items else None,
        },
        "final_answer": {
            "present": bool(final_answer_items),
            "citation_count": final_answer_items[-1].get("citation_count") if final_answer_items else 0,
            "contract_version": final_answer_items[-1].get("contract_version") if final_answer_items else None,
            "citation_required": final_answer_items[-1].get("citation_required") if final_answer_items else None,
            "citation_status": final_answer_items[-1].get("citation_status") if final_answer_items else None,
            "source_count": final_answer_items[-1].get("source_count") if final_answer_items else 0,
            "tool_evidence_count": final_answer_items[-1].get("tool_evidence_count") if final_answer_items else 0,
            "contract_warnings": final_answer_items[-1].get("contract_warnings") if final_answer_items else [],
            "safety_status": final_answer_items[-1].get("safety_status") if final_answer_items else None,
        },
        "output_guard": {
            "present": bool(output_guard_items),
            "last_action": output_guard_items[-1].get("action") if output_guard_items else None,
            "safe": output_guard_items[-1].get("safe") if output_guard_items else None,
        },
        "observability": observability,
        "cost": cost,
        "checkpoint": checkpoint,
        "consistency": consistency,
        "warnings": [warning.get("code") for warning in warnings],
    }


def _preview(text: str, limit: int) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 3]}..."


def _retrieval_preview(payload: dict[str, Any]) -> str:
    query = str(payload.get("query") or "").strip()
    sources = payload.get("sources") if isinstance(payload.get("sources"), list) else []
    names: list[str] = []
    for source in sources[:4]:
        if not isinstance(source, dict):
            continue
        file_name = str(source.get("source_file") or "").strip()
        section = str(
            source.get("section_title") or source.get("heading_path") or ""
        ).strip()
        if file_name and section:
            names.append(f"{file_name} · {section}")
        elif file_name:
            names.append(file_name)
    parts = [f"query={query!r}"] if query else []
    if names:
        parts.append("sources=" + ", ".join(names))
    count = int(payload.get("source_count") or len(sources))
    if count:
        parts.append(f"hits={count}")
    return _preview("; ".join(parts) or "retrieval", 240)


def _sum_int(payloads: list[dict[str, Any]], key: str) -> int:
    return sum(int(payload.get(key) or 0) for payload in payloads)


def _count_event_type(events: list[dict[str, Any]], event_type: str) -> int:
    return sum(1 for event in events if str(event.get("type", "")) == event_type)


def _failed_tool_events(events: list[dict[str, Any]]) -> int:
    count = 0
    for event in events:
        if str(event.get("type", "")) != "tool_end":
            continue
        if _payload(event).get("success") is False:
            count += 1
    return count


def _first_payload_value(payloads: list[dict[str, Any]], key: str) -> Any:
    for payload in payloads:
        value = payload.get(key)
        if value:
            return value
    return None
