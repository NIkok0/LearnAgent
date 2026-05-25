from __future__ import annotations

from typing import Any

from copilot_agent.memory.item_schema import MemoryScope, MemoryType
from copilot_agent.tools.sanitize import sanitize_tool_payload

MAX_ITEMS_PER_SECTION = 8
MAX_SEEDS = 8
MAX_TEXT_CHARS = 300


def seed(
    *,
    content: str,
    memory_type: MemoryType,
    importance: float,
    confidence: float,
    source_kind: str,
    source_event_ids: list[int],
    reusable: bool,
    scope: MemoryScope = MemoryScope.SESSION,
    ttl_days: int | None = None,
) -> dict[str, Any]:
    return sanitize_tool_payload(
        {
            "scope": scope,
            "memory_type": memory_type,
            "content": bounded_text(content, 400),
            "importance": importance,
            "confidence": confidence,
            "source_kind": source_kind,
            "source_event_ids": source_event_ids,
            "ttl_days": ttl_days,
            "reusable": reusable,
        },
        max_string_length=MAX_TEXT_CHARS,
    )


def dedupe_seeds(seeds: list[dict[str, Any]], *, outcome: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in seeds:
        content = str(candidate.get("content") or "").strip()
        if not content:
            continue
        memory_type = str(candidate.get("memory_type") or "")
        if outcome != "completed" and memory_type not in {"preference", "behavior"} and not bool(candidate.get("reusable")):
            continue
        key = " ".join(content.lower().split())
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def dedupe_records(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        key = str(sorted((k, str(v)) for k, v in item.items() if k != "source_event_ids"))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def dedupe_strings(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def done_event_ids(events: list[dict[str, Any]]) -> list[int]:
    return [event_id(event) for event in events if event.get("type") == "done" and event_id(event) is not None]


def event_id(event: dict[str, Any]) -> int | None:
    raw = event.get("id")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def event_ids(value: int | None) -> list[int]:
    return [value] if value is not None else []


def bounded_text(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "MAX_ITEMS_PER_SECTION",
    "MAX_SEEDS",
    "MAX_TEXT_CHARS",
    "bounded_text",
    "dedupe_records",
    "dedupe_seeds",
    "dedupe_strings",
    "done_event_ids",
    "event_id",
    "event_ids",
    "int_or_none",
    "seed",
]
