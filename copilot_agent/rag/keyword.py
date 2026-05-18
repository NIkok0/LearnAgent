from __future__ import annotations

import re

from copilot_agent.rag.schema import DocChunk

_token_re = re.compile(r"[A-Za-z0-9_./:-]{2,}")


def _tokens(q: str) -> set[str]:
    return {t.lower() for t in _token_re.findall(q)}


def keyword_search(chunks: list[DocChunk], query: str, top_k: int = 6) -> list[DocChunk]:
    qt = _tokens(query)
    if not qt:
        return chunks[:top_k]
    scored: list[tuple[int, DocChunk]] = []
    for c in chunks:
        low = c.text.lower()
        score = sum(low.count(t) * (3 if len(t) > 4 else 1) for t in qt)
        if score == 0:
            score = sum(1 for t in qt if t in low)
        scored.append((score, c))
    scored.sort(key=lambda x: (-x[0], x[1].source, x[1].start_line))
    out = [c for s, c in scored if s > 0][:top_k]
    if not out:
        return chunks[:top_k]
    return out


def keyword_scores(chunks: list[DocChunk], query: str) -> dict[tuple[str, int], float]:
    """Return normalized keyword scores keyed by (source, start_line)."""
    qt = _tokens(query)
    if not qt:
        return {}
    raw: dict[tuple[str, int], float] = {}
    for c in chunks:
        low = c.text.lower()
        score = float(sum(low.count(t) * (3 if len(t) > 4 else 1) for t in qt))
        if score == 0:
            score = float(sum(1 for t in qt if t in low))
        if score > 0:
            raw[c.key] = score
    if not raw:
        return {}
    max_s = max(raw.values()) or 1.0
    return {k: v / max_s for k, v in raw.items()}
