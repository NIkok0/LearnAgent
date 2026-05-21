from __future__ import annotations

import re

from copilot_agent.rag.schema import DocChunk
from copilot_agent.settings import settings


def normalize_query(query: str) -> str:
    text = (query or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return re.sub(r"[^\w\s\u4e00-\u9fff]", "", text)


def queries_equivalent(left: str, right: str) -> bool:
    a = normalize_query(left)
    b = normalize_query(right)
    if not a or not b:
        return False
    if a == b:
        return True
    if a in b or b in a:
        return True
    tokens_a = set(a.split())
    tokens_b = set(b.split())
    if not tokens_a or not tokens_b:
        return False
    overlap = len(tokens_a & tokens_b)
    union = len(tokens_a | tokens_b)
    return union > 0 and (overlap / union) >= 0.75


def chunk_keys(hits: list[DocChunk]) -> set[tuple[str, int]]:
    return {chunk.key for chunk in hits}


def build_preretrieval_cache(*, query: str, hits: list[DocChunk]) -> dict[str, object]:
    return {
        "query": query,
        "chunk_keys": [f"{source}:{start_line}" for source, start_line in chunk_keys(hits)],
        "sources": sorted({chunk.source for chunk in hits}),
    }


def filter_new_chunks(existing_keys: set[tuple[str, int]], hits: list[DocChunk]) -> list[DocChunk]:
    return [chunk for chunk in hits if chunk.key not in existing_keys]


def parse_cache_keys(cache: dict[str, object]) -> set[tuple[str, int]]:
    keys: set[tuple[str, int]] = set()
    raw = cache.get("chunk_keys")
    if not isinstance(raw, list):
        return keys
    for item in raw:
        text = str(item)
        if ":" not in text:
            continue
        source, line = text.rsplit(":", 1)
        try:
            keys.add((source, int(line)))
        except ValueError:
            continue
    return keys


def apply_preretrieval_dedupe(
    query: str,
    hits: list[DocChunk],
    cache: dict[str, object] | None,
) -> tuple[list[DocChunk], dict[str, object]]:
    """Drop chunks already injected via preretrieval when tool search_docs repeats the turn query."""
    meta: dict[str, object] = {
        "dedupe_enabled": bool(settings.context_preretrieval_dedupe_enabled),
        "deduped_count": 0,
        "skipped_all_duplicate": False,
        "same_query_as_preretrieval": False,
    }
    if not settings.context_preretrieval_dedupe_enabled or not cache:
        return hits, meta

    cached_query = str(cache.get("query") or "")
    meta["same_query_as_preretrieval"] = queries_equivalent(query, cached_query)
    if not meta["same_query_as_preretrieval"]:
        return hits, meta

    existing = parse_cache_keys(cache)
    if not existing:
        return hits, meta

    filtered = filter_new_chunks(existing, hits)
    meta["deduped_count"] = len(hits) - len(filtered)
    if hits and not filtered:
        meta["skipped_all_duplicate"] = True
    return filtered, meta
