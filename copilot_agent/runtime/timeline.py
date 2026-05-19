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
MEMORY_EVENTS = {"memory_run_summary", "memory_thread_summary"}


class TimelineProjector:
    """Project raw EventStore events into a UI-oriented run timeline."""

    def project_run(self, run: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
        ordered_events = sorted(events, key=lambda event: int(event.get("id", 0) or 0))
        warnings: list[dict[str, Any]] = []
        items: list[dict[str, Any]] = []
        token_buffer: list[str] = []
        token_event_ids: list[int] = []
        tools: OrderedDict[str, dict[str, Any]] = OrderedDict()
        approval_pending: dict[str, Any] | None = None
        approval_index = 0

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
        warning_items = [
            {
                "kind": "warning",
                "title": warning["code"],
                "warning": warning,
            }
            for warning in warnings
        ]

        return {
            "status": run.get("status"),
            "items": _sort_items(items + warning_items),
            "warnings": warnings,
            "assistant_output": "".join(
                str(_payload(event).get("text", ""))
                for event in ordered_events
                if event.get("type") == "token"
            ),
            "event_count": len(ordered_events),
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


def _preview(text: str, limit: int) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 3]}..."
