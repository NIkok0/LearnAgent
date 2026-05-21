from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from copilot_agent.contracts.base import RuntimeEvent
from copilot_agent.contracts.envelope import EVENT_SCHEMA_VERSION, payload_schema_version
from copilot_agent.contracts.events.registry import (
    PayloadValidationError,
    validate_payload_for_kind,
)
from copilot_agent.runtime.event_schema import KNOWN_EVENT_TYPES


class ContractValidationError(ValueError):
    """Raised when an event row fails RuntimeEvent contract validation."""


def validate_stored_event(
    *,
    kind: str,
    payload: dict[str, Any] | None,
    thread_id: str | None = None,
    run_id: str | None = None,
) -> RuntimeEvent:
    """Parse and validate a persisted event row as RuntimeEvent."""
    if kind not in KNOWN_EVENT_TYPES:
        raise ContractValidationError(f"unknown event kind: {kind}")
    raw_payload = payload if isinstance(payload, dict) else {}
    try:
        normalized_payload = validate_payload_for_kind(kind, raw_payload)
    except PayloadValidationError as exc:
        raise ContractValidationError(str(exc)) from exc

    event = RuntimeEvent.from_stored(
        kind=kind,
        payload=normalized_payload,
        thread_id=thread_id,
        run_id=run_id,
    )
    stored = event.to_store_payload()
    if payload_schema_version(stored) != EVENT_SCHEMA_VERSION:
        raise ContractValidationError(f"{kind}: schema_version must be {EVENT_SCHEMA_VERSION}")
    return event


def validate_event_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate EventStore list_events() rows; return summary with errors."""
    errors: list[str] = []
    validated = 0
    for row in rows:
        kind = str(row.get("type", ""))
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        thread_id = str(row.get("thread_id", "")) or None
        run_id = str(row.get("run_id", "")) or None
        try:
            validate_stored_event(kind=kind, payload=payload, thread_id=thread_id, run_id=run_id)
            validated += 1
        except (ContractValidationError, ValidationError) as exc:
            event_id = row.get("id", "?")
            errors.append(f"event_id={event_id} kind={kind}: {exc}")
        except Exception as exc:
            event_id = row.get("id", "?")
            errors.append(f"event_id={event_id} kind={kind}: {exc}")
    return {
        "validated_count": validated,
        "error_count": len(errors),
        "errors": errors,
        "model_validate_ok": len(errors) == 0,
    }


def validate_event_kinds(kinds: list[str]) -> dict[str, Any]:
    unknown = [kind for kind in kinds if kind not in KNOWN_EVENT_TYPES]
    return {
        "kinds_total": len(kinds),
        "unknown_kinds": unknown,
        "kinds_ok": not unknown,
    }


def enrich_event_row(row: dict[str, Any]) -> dict[str, Any]:
    """Return row with contract-validated payload and correlation block."""
    kind = str(row.get("type", ""))
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    thread_id = str(row.get("thread_id", "")) or None
    run_id = str(row.get("run_id", "")) or None
    event = validate_stored_event(kind=kind, payload=payload, thread_id=thread_id, run_id=run_id)
    enriched = dict(row)
    enriched["payload"] = event.to_store_payload()
    enriched["correlation"] = event.correlation.model_dump(exclude_none=True)
    enriched["contract_validated"] = True
    return enriched
