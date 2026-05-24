from __future__ import annotations

import re
from typing import Any

from copilot_agent.rag.schema import DocChunk


def _token_set(text: str) -> set[str]:
    return set(re.findall(r"[A-Za-z0-9_\u4e00-\u9fff]{2,}", str(text or "").lower()))


def text_jaccard(left: str, right: str) -> float:
    left_tokens = _token_set(left)
    right_tokens = _token_set(right)
    if not left_tokens or not right_tokens:
        return 0.0
    union = left_tokens | right_tokens
    if not union:
        return 0.0
    return len(left_tokens & right_tokens) / len(union)


def context_overlap_rate(chunks: list[DocChunk]) -> float:
    """Average Jaccard overlap between adjacent chunks from the same source."""
    by_source: dict[str, list[DocChunk]] = {}
    for chunk in chunks:
        by_source.setdefault(chunk.source, []).append(chunk)
    overlaps: list[float] = []
    for group in by_source.values():
        ordered = sorted(group, key=lambda item: item.start_line)
        for left, right in zip(ordered, ordered[1:]):
            overlaps.append(text_jaccard(left.text, right.text))
    if not overlaps:
        return 0.0
    return sum(overlaps) / len(overlaps)


def truncation_rate(truncated_flags: list[bool]) -> float:
    if not truncated_flags:
        return 0.0
    return sum(1 for flag in truncated_flags if flag) / len(truncated_flags)


def authority_spread(chunks: list[DocChunk]) -> dict[str, Any]:
    values = [int(getattr(chunk, "authority", 50) or 50) for chunk in chunks]
    if not values:
        return {"min": 0, "max": 0, "avg": 0.0}
    return {
        "min": min(values),
        "max": max(values),
        "avg": round(sum(values) / len(values), 2),
    }
