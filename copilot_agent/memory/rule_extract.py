from __future__ import annotations

import re
from typing import Any

from copilot_agent.memory.item_schema import MemoryScope, MemoryType

_PREFERENCE_PATTERNS = (
    re.compile(r"(?:我|用户)?(?:偏好|喜欢|习惯)(.{2,80})", re.I),
    re.compile(r"(?:不要|不喜欢|别)(.{2,80})", re.I),
    re.compile(r"(?:prefer|like to|don't like)\s+(.{2,80})", re.I),
)


def extract_memory_candidates(
    *,
    goal: str,
    key_outputs: list[str],
    outcome: str,
    run_id: str,
) -> list[dict[str, Any]]:
    """Rule-based short→long extraction (no LLM)."""
    candidates: list[dict[str, Any]] = []
    goal_text = (goal or "").strip()
    if goal_text:
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
    return candidates
