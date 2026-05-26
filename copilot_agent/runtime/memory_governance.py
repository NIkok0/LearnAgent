from __future__ import annotations

from typing import Any

from copilot_agent.runtime.event_schema import (
    EVENT_MEMORY_ITEM_CONFIRMED,
    EVENT_MEMORY_ITEM_DELETED,
    EVENT_MEMORY_ITEM_DELETE_PROOF,
    EVENT_MEMORY_ITEM_REJECTED,
)

MEMORY_GOVERNANCE_EVENTS = {
    EVENT_MEMORY_ITEM_CONFIRMED,
    EVENT_MEMORY_ITEM_REJECTED,
    EVENT_MEMORY_ITEM_DELETED,
    EVENT_MEMORY_ITEM_DELETE_PROOF,
}


def build_memory_governance_read_model(events: list[dict[str, Any]]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    latest_delete_proof: dict[str, Any] | None = None
    summary = {
        "total": 0,
        "confirmed": 0,
        "rejected": 0,
        "deleted": 0,
        "delete_proof": 0,
    }
    for event in events:
        event_type = str(event.get("type") or "")
        if event_type not in MEMORY_GOVERNANCE_EVENTS:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        sanitized = _sanitize_governance_payload(payload)
        item = {
            "event_id": int(event.get("id") or 0),
            "event_type": event_type,
            "created_at": event.get("created_at"),
            "payload": sanitized,
        }
        items.append(item)
        summary["total"] += 1
        if event_type == EVENT_MEMORY_ITEM_CONFIRMED:
            summary["confirmed"] += 1
        elif event_type == EVENT_MEMORY_ITEM_REJECTED:
            summary["rejected"] += 1
        elif event_type == EVENT_MEMORY_ITEM_DELETED:
            summary["deleted"] += 1
        elif event_type == EVENT_MEMORY_ITEM_DELETE_PROOF:
            summary["delete_proof"] += 1
            latest_delete_proof = sanitized
    return {
        "events": items,
        "summary": summary,
        "latest_delete_proof": latest_delete_proof,
    }


def _sanitize_governance_payload(payload: dict[str, Any]) -> dict[str, Any]:
    blocked = {
        "content",
        "raw_prompt",
        "prompt",
        "request_body",
        "response_body",
        "raw_response",
        "secret",
        "cookie",
        "embedding",
        "embedding_json",
    }
    return {str(key): value for key, value in payload.items() if str(key).lower() not in blocked}
