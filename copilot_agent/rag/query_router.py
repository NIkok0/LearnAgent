from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from copilot_agent.rag.tokenize import extract_ascii_tokens, extract_cjk_tokens

RetrievalMode = Literal["sparse", "dense", "hybrid"]

# Signals that BM25 / keyword should dominate (exact terms, API paths, codes).
_SPARSE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"/api/|/actuator/", "api_path"),
    (r"\b(GET|POST|PUT|DELETE)\s+/", "http_method_path"),
    (r"WM_JOBS_[A-Z0-9_]+|WMSESSIONID|verify-config", "platform_constant"),
    (r"\b(QUEUED|PROCESSING|FAILED|UNAUTHORIZED|errorCode)\b", "status_or_error"),
    (r"\bR-\d{3,}\b", "requirement_id"),
    (r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "uuid"),
    (r"wm:jobs:stream|wm-workers", "redis_key"),
)

# Signals that dense / semantic retrieval should weigh more (open Chinese questions).
_DENSE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"怎么|如何|为什么|哪些|是什么|大致|常见|步骤|原因|排查", "open_question"),
    (r"一直|卡住|不动", "troubleshooting_phrase"),
    (r"支持.+吗|能否", "capability_question"),
)

_MODE_WEIGHTS: dict[RetrievalMode, tuple[float, float, float]] = {
    # keyword, bm25, vector
    "sparse": (0.35, 1.25, 0.25),
    "dense": (0.25, 0.55, 1.20),
    "hybrid": (0.50, 1.00, 0.85),
}


@dataclass(frozen=True)
class RetrievalRoute:
    mode: RetrievalMode
    keyword_weight: float
    bm25_weight: float
    vector_weight: float
    sparse_signals: tuple[str, ...]
    dense_signals: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "keyword_weight": round(self.keyword_weight, 4),
            "bm25_weight": round(self.bm25_weight, 4),
            "vector_weight": round(self.vector_weight, 4),
            "sparse_signals": list(self.sparse_signals),
            "dense_signals": list(self.dense_signals),
        }


def _match_signals(query: str, patterns: tuple[tuple[str, str], ...]) -> tuple[str, ...]:
    hits: list[str] = []
    for pattern, label in patterns:
        if re.search(pattern, query, flags=re.IGNORECASE):
            hits.append(label)
    return tuple(hits)


def _lexical_balance(query: str) -> tuple[int, int]:
    ascii_n = len(extract_ascii_tokens(query))
    cjk_n = len(extract_cjk_tokens(query))
    return ascii_n, cjk_n


def route_query(query: str, *, vector_available: bool = True) -> RetrievalRoute:
    """Classify query and return per-channel fusion weights for BM25 + vector mixing."""
    text = query.strip()
    sparse_signals = _match_signals(text, _SPARSE_PATTERNS)
    dense_signals = _match_signals(text, _DENSE_PATTERNS)
    ascii_n, cjk_n = _lexical_balance(text)

    sparse_score = len(sparse_signals) + (1 if ascii_n >= 2 and ascii_n > cjk_n else 0)
    dense_score = len(dense_signals) + (1 if cjk_n >= 2 and cjk_n > ascii_n else 0)

    if sparse_score >= 1 and dense_score >= 1 and vector_available:
        mode: RetrievalMode = "hybrid"
    elif sparse_score >= 2 and sparse_score > dense_score:
        mode = "sparse"
    elif dense_score >= 2 and dense_score > sparse_score and vector_available:
        mode = "dense"
    elif vector_available and dense_score >= 1 and sparse_score == 0:
        mode = "dense"
    elif sparse_score >= 1:
        mode = "sparse"
    else:
        mode = "hybrid"

    kw_w, bm25_w, vec_w = _MODE_WEIGHTS[mode]
    if not vector_available:
        # No vector index: redistribute weight to sparse channels.
        total = kw_w + bm25_w
        kw_w = kw_w / total
        bm25_w = bm25_w / total
        vec_w = 0.0
        if mode == "dense":
            mode = "hybrid"

    return RetrievalRoute(
        mode=mode,
        keyword_weight=kw_w,
        bm25_weight=bm25_w,
        vector_weight=vec_w,
        sparse_signals=sparse_signals,
        dense_signals=dense_signals,
    )
