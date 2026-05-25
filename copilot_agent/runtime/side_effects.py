from __future__ import annotations

from typing import Any

from copilot_agent.runtime.event_schema import EVENT_TOOL_SIDE_EFFECT_RECORDED

SIDE_EFFECT_STATUSES = ("confirmed", "reused", "none", "unknown", "blocked")
SIDE_EFFECT_STATUS_SET = set(SIDE_EFFECT_STATUSES)


def build_side_effect_read_model(run: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    """Project high-risk write tool side effects into a safe run-level read model."""
    side_effects: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for event in _ordered_events(events):
        if str(event.get("type") or "") != EVENT_TOOL_SIDE_EFFECT_RECORDED:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        item = _side_effect_item(event, payload)
        side_effects.append(item)
        status = str(item.get("side_effect_status") or "")
        if status == "unknown":
            warnings.append(
                {
                    "code": "side_effect_unknown",
                    "message": "write tool side effect could not be confirmed",
                    "event_id": item.get("event_id"),
                    "call_id": item.get("call_id"),
                    "tool": item.get("tool_name"),
                    "reason": item.get("reason"),
                }
            )
        elif status not in SIDE_EFFECT_STATUS_SET:
            warnings.append(
                {
                    "code": "side_effect_unknown_status",
                    "message": "write tool side effect used an unknown status",
                    "event_id": item.get("event_id"),
                    "call_id": item.get("call_id"),
                    "tool": item.get("tool_name"),
                    "side_effect_status": status,
                }
            )
    summary = _summary(side_effects)
    return {
        "run": {
            "id": run.get("id"),
            "thread_id": run.get("thread_id"),
            "status": run.get("status"),
        },
        "summary": summary,
        "side_effects": side_effects,
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


def _side_effect_item(event: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": int(event.get("id", 0) or 0),
        "sequence": int(event.get("sequence", 0) or 0),
        "created_at": event.get("created_at"),
        "tool_name": payload.get("tool_name"),
        "call_id": payload.get("call_id"),
        "method": payload.get("method"),
        "path": payload.get("path"),
        "risk_level": payload.get("risk_level"),
        "requires_approval": bool(payload.get("requires_approval", False)),
        "approval_status": payload.get("approval_status"),
        "side_effect_status": payload.get("side_effect_status"),
        "success": bool(payload.get("success", False)),
        "status_code": payload.get("status_code"),
        "idempotency_key": payload.get("idempotency_key"),
        "idempotency_reused": bool(payload.get("idempotency_reused", False)),
        "compensatable": bool(payload.get("compensatable", False)),
        "reason": payload.get("reason"),
        "policy_trace_id": payload.get("policy_trace_id"),
    }


def _summary(side_effects: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {status: 0 for status in SIDE_EFFECT_STATUSES}
    for item in side_effects:
        status = str(item.get("side_effect_status") or "")
        if status in counts:
            counts[status] += 1
    return {
        "total": len(side_effects),
        **counts,
        "has_unknown": counts["unknown"] > 0,
    }
