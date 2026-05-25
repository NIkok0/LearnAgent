from __future__ import annotations

from datetime import UTC, datetime

from copilot_agent.memory.item_schema import MemoryItemRecord, MemoryType


def memory_eviction_score(item: MemoryItemRecord, *, now: datetime | None = None) -> float:
    """Higher score means more worth keeping; lower score is evicted first."""
    current = now or datetime.now(UTC)
    recency = _age_score(item.updated_at, current=current, half_life_days=30.0)
    access_recency = _age_score(item.last_accessed_at, current=current, half_life_days=45.0) if item.last_accessed_at else 0.0
    access_score = min(1.0, item.access_count / 8.0)
    type_score = {
        MemoryType.PREFERENCE: 0.18,
        MemoryType.BEHAVIOR: 0.12,
        MemoryType.TASK_SUMMARY: 0.08,
        MemoryType.FACT: 0.0,
    }.get(item.memory_type, 0.0)
    score = (
        0.42 * _clamp_unit(item.importance)
        + 0.24 * _clamp_unit(item.confidence)
        + 0.14 * access_score
        + 0.08 * access_recency
        + 0.07 * recency
        + type_score
        - _ttl_penalty(item.expires_at, current=current)
        - (0.3 if item.pending_confirmation else 0.0)
    )
    return round(score, 6)


def _age_score(value: str | None, *, current: datetime, half_life_days: float) -> float:
    if not value:
        return 0.0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
    except ValueError:
        return 0.0
    age_days = max(0.0, (current - parsed).total_seconds() / 86400.0)
    if half_life_days <= 0:
        return 1.0
    return 2 ** (-age_days / half_life_days)


def _ttl_penalty(value: str | None, *, current: datetime) -> float:
    if not value:
        return 0.0
    try:
        expires = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
    except ValueError:
        return 0.0
    days_left = (expires - current).total_seconds() / 86400.0
    if days_left <= 0:
        return 0.45
    if days_left <= 1:
        return 0.25
    if days_left <= 7:
        return 0.12
    return 0.0


def _clamp_unit(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


__all__ = ["memory_eviction_score"]
