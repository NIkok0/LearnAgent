from __future__ import annotations

from copilot_agent.rag.schema import DocChunk

# Static authority-ish boosts (§4.7); merged with query hints at search time.
DOC_TYPE_BOOST: dict[str, float] = {
    "api_contract": 1.12,
    "requirements": 1.10,
    "deploy": 1.08,
    "tech_selection": 1.08,
    "runbook": 1.06,
    "security": 1.06,
    "operations": 1.05,
    "algorithm": 1.05,
    "overview": 1.0,
    "doc": 1.0,
}


def rank_from_scores(scores: dict[tuple[str, int], float]) -> dict[tuple[str, int], int]:
    ordered = sorted(scores.items(), key=lambda x: (-x[1], x[0][0], x[0][1]))
    return {key: rank + 1 for rank, (key, _) in enumerate(ordered)}


def rrf_fuse(
    rankings: list[dict[tuple[str, int], int]],
    *,
    weights: list[float] | None = None,
    k: int = 60,
) -> dict[tuple[str, int], float]:
    if not rankings:
        return {}
    if weights is None:
        weights = [1.0] * len(rankings)
    fused: dict[tuple[str, int], float] = {}
    for ranking, weight in zip(rankings, weights):
        for key, rank in ranking.items():
            fused[key] = fused.get(key, 0.0) + weight * (1.0 / (k + rank))
    return fused


def apply_doc_type_boost(
    scores: dict[tuple[str, int], float],
    chunks_by_key: dict[tuple[str, int], DocChunk],
    *,
    query_hints: dict[str, float] | None = None,
    enabled: bool = True,
) -> dict[tuple[str, int], float]:
    if not enabled or not scores:
        return scores
    hints = query_hints or {}
    boosted: dict[tuple[str, int], float] = {}
    for key, score in scores.items():
        chunk = chunks_by_key.get(key)
        if chunk is None:
            boosted[key] = score
            continue
        static = DOC_TYPE_BOOST.get(chunk.doc_type, 1.0)
        hint = hints.get(chunk.doc_type, 1.0)
        boosted[key] = score * static * hint
    return boosted


def apply_authority_boost(
    scores: dict[tuple[str, int], float],
    chunks_by_key: dict[tuple[str, int], DocChunk],
    *,
    enabled: bool = True,
) -> dict[tuple[str, int], float]:
    if not enabled or not scores:
        return scores
    boosted: dict[tuple[str, int], float] = {}
    for key, score in scores.items():
        chunk = chunks_by_key.get(key)
        if chunk is None:
            boosted[key] = score
            continue
        authority = int(getattr(chunk, "authority", 50) or 50)
        boosted[key] = score * (1.0 + (authority - 50) * 0.002)
    return boosted


def dedup_chunks(chunks: list[DocChunk]) -> list[DocChunk]:
    """Keep one chunk per (source, heading_path); prefer highest authority."""
    grouped: dict[tuple[str, str], list[DocChunk]] = {}
    order: list[tuple[str, str]] = []
    for chunk in chunks:
        path = chunk.heading_path or chunk.section_title or str(chunk.start_line)
        dedup_key = (chunk.source, path)
        if dedup_key not in grouped:
            order.append(dedup_key)
            grouped[dedup_key] = []
        grouped[dedup_key].append(chunk)
    return [max(grouped[key], key=lambda item: int(getattr(item, "authority", 50) or 50)) for key in order]
