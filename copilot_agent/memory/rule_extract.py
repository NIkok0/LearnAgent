from __future__ import annotations

import re
from typing import Any

from copilot_agent.memory.item_schema import MemoryScope, MemoryType

_PREFERENCE_PATTERNS = (
    re.compile(r"(?:我|用户)?(?:偏好|喜欢|习惯)(.{2,80})", re.I),
    re.compile(r"(?:不要|不喜欢|避免)(.{2,80})", re.I),
    re.compile(r"(?:prefer|like to|don't like)\s+(.{2,80})", re.I),
)


def extract_memory_candidates(
    *,
    goal: str,
    key_outputs: list[str],
    outcome: str,
    run_id: str,
    memory_candidates_seed: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Rule-based short-to-long extraction (no LLM)."""
    candidates: list[dict[str, Any]] = []
    seeded = _seed_candidates(memory_candidates_seed or [], run_id=run_id)
    candidates.extend(seeded)

    goal_text = (goal or "").strip()
    has_seed_task = any(_memory_type_of(item) == MemoryType.TASK_SUMMARY for item in seeded)
    if goal_text and not has_seed_task:
        importance = 0.75 if outcome == "completed" else 0.55
        candidates.append(
            {
                "scope": MemoryScope.SESSION,
                "memory_type": MemoryType.TASK_SUMMARY,
                "content": goal_text[:400],
                "importance": importance,
                "confidence": 0.9 if outcome == "completed" else 0.7,
                "source_run_id": run_id,
                "ttl_days": None,
            }
        )

    if not seeded:
        for output in key_outputs[:2]:
            text = str(output or "").strip()
            if len(text) < 20:
                continue
            candidates.append(
                {
                    "scope": MemoryScope.SESSION,
                    "memory_type": MemoryType.FACT,
                    "content": text[:300],
                    "importance": 0.6,
                    "confidence": 0.75,
                    "source_run_id": run_id,
                    "ttl_days": 30,
                }
            )

    for pattern in _PREFERENCE_PATTERNS:
        match = pattern.search(goal_text)
        if not match:
            continue
        phrase = match.group(0).strip()[:200]
        candidates.append(
            {
                "scope": MemoryScope.USER,
                "memory_type": MemoryType.PREFERENCE,
                "content": phrase,
                "importance": 0.85,
                "confidence": 0.8,
                "source_run_id": run_id,
                "ttl_days": None,
            }
        )
        break
    return _dedupe_candidates(candidates)


def _seed_candidates(seeds: list[dict[str, Any]], *, run_id: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for seed in seeds[:8]:
        if not isinstance(seed, dict):
            continue
        content = str(seed.get("content") or "").strip()
        if not content:
            continue
        candidates.append(
            {
                "scope": _parse_scope(seed.get("scope")),
                "memory_type": _parse_memory_type(seed.get("memory_type")),
                "content": content[:400],
                "importance": _clamp_float(seed.get("importance"), default=0.65),
                "confidence": _clamp_float(seed.get("confidence"), default=0.78),
                "source_run_id": run_id,
                "ttl_days": seed.get("ttl_days"),
                "reusable": bool(seed.get("reusable", True)),
                "source_kind": str(seed.get("source_kind") or ""),
                "source_event_ids": list(seed.get("source_event_ids") or []),
            }
        )
    return candidates


def _parse_memory_type(value: Any) -> MemoryType:
    if isinstance(value, MemoryType):
        return value
    raw = str(value or "fact").strip().lower()
    if raw == "preference":
        return MemoryType.PREFERENCE
    if raw == "behavior":
        return MemoryType.BEHAVIOR
    if raw in {"task", "task_summary"}:
        return MemoryType.TASK_SUMMARY
    return MemoryType.FACT


def _parse_scope(value: Any) -> MemoryScope:
    if isinstance(value, MemoryScope):
        return value
    return MemoryScope.USER if str(value or "").strip().lower() == "user" else MemoryScope.SESSION


def _memory_type_of(candidate: dict[str, Any]) -> MemoryType:
    return _parse_memory_type(candidate.get("memory_type"))


def _clamp_float(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, parsed))


def _dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        content = " ".join(str(candidate.get("content") or "").split()).lower()
        if not content or content in seen:
            continue
        seen.add(content)
        out.append(candidate)
    return out
