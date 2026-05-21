from __future__ import annotations

from typing import Any

EVENT_SCHEMA_VERSION = 1


def envelope_payload(event_type: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    data = dict(payload or {})
    if "schema_version" not in data:
        data["schema_version"] = EVENT_SCHEMA_VERSION
    return data


def payload_schema_version(payload: dict[str, Any] | None) -> int:
    if not isinstance(payload, dict):
        return 0
    raw = payload.get("schema_version")
    try:
        return int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return 0
