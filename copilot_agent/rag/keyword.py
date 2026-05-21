from __future__ import annotations

from copilot_agent.rag.schema import DocChunk
from copilot_agent.rag.tokenize import token_set


def _score_chunk(chunk: DocChunk, qt: set[str]) -> float:
    low = chunk.text.lower()
    score = float(sum(low.count(t) * (3 if len(t) > 4 else 1) for t in qt))
    if score == 0:
        score = float(sum(1 for t in qt if t in low or t in chunk.text))
    return score


def keyword_search(chunks: list[DocChunk], query: str, top_k: int = 6) -> list[DocChunk]:
    qt = token_set(query)
    if not qt:
        return []
    scored: list[tuple[float, DocChunk]] = []
    for c in chunks:
        score = _score_chunk(c, qt)
        if score > 0:
            scored.append((score, c))
    scored.sort(key=lambda x: (-x[0], x[1].source, x[1].start_line))
    return [c for _, c in scored[:top_k]]


def keyword_scores(chunks: list[DocChunk], query: str) -> dict[tuple[str, int], float]:
    """Return normalized keyword scores keyed by (source, start_line)."""
    qt = token_set(query)
    if not qt:
        return {}
    raw: dict[tuple[str, int], float] = {}
    for c in chunks:
        score = _score_chunk(c, qt)
        if score > 0:
            raw[c.key] = score
    if not raw:
        return {}
    max_s = max(raw.values()) or 1.0
    return {k: v / max_s for k, v in raw.items()}
