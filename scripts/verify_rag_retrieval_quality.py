#!/usr/bin/env python
"""Verify §11.2 retrieval quality: rewrite, BM25, RRF, doc_type boost, dedup."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.rag.bm25 import BM25Index  # noqa: E402
from copilot_agent.rag.fusion import dedup_chunks, rrf_fuse, rank_from_scores  # noqa: E402
from copilot_agent.rag.query_rewrite import rewrite_query  # noqa: E402
from copilot_agent.rag.query_router import route_query  # noqa: E402
from copilot_agent.rag.retriever import build_rag_store  # noqa: E402
from copilot_agent.rag.schema import DocChunk, dynamic_search_top_k, select_chunks_for_budget  # noqa: E402
from copilot_agent.rag.tokenize import tokenize  # noqa: E402
from copilot_agent.scenario import load_scenario  # noqa: E402
from copilot_agent.scenario.bootstrap import apply_scenario_environment  # noqa: E402


def _assert(name: str, ok: bool) -> None:
    if not ok:
        raise SystemExit(f"FAIL: {name}")
    print(f"PASS: {name}")


def main() -> int:
    apply_scenario_environment(load_scenario("watermark"))
    _assert("cjk tokenize", "生产部署" in tokenize("生产部署 Java API"))
    _assert("ascii tokenize", "verify-config" in tokenize("verify-config self check"))

    rewritten = rewrite_query("水印任务一直卡住怎么排查？")
    _assert("query rewrite expands", "QUEUED" in rewritten or "Redis" in rewritten)

    chunks = [
        DocChunk(
            source="a.md",
            start_line=1,
            text="Redis Stream wm:jobs:stream",
            heading_path="Queue > Redis",
            doc_type="tech_selection",
        ),
        DocChunk(
            source="a.md",
            start_line=10,
            text="Redis Stream wm:jobs:stream duplicate",
            heading_path="Queue > Redis",
            doc_type="tech_selection",
        ),
        DocChunk(source="b.md", start_line=1, text="verify-config environment", doc_type="deploy"),
    ]
    bm25 = BM25Index(chunks)
    scores = bm25.scores("Redis Stream")
    _assert("bm25 scores", ("a.md", 1) in scores)

    r1 = rank_from_scores({("a.md", 1): 1.0, ("b.md", 1): 0.5})
    r2 = rank_from_scores({("b.md", 1): 1.0, ("a.md", 1): 0.2})
    fused = rrf_fuse([r1, r2], k=60)
    _assert("rrf fuse", ("a.md", 1) in fused and ("b.md", 1) in fused)

    deduped = dedup_chunks(chunks[:2])
    _assert("dedup same heading path", len(deduped) == 1)

    huge = [
        DocChunk(source=f"big-{i}.md", start_line=1, text="x" * 5000, doc_type="doc")
        for i in range(6)
    ]
    packed = select_chunks_for_budget(huge, max_chars=8000)
    _assert("budget packing limits chunks", 1 <= len(packed) < len(huge))
    _assert("dynamic top-k respects budget", dynamic_search_top_k(budget_chars=4200, ceiling=8) <= 3)

    sparse = route_query("POST /api/v1/auth/login 需要哪些请求字段？", vector_available=True)
    _assert(
        "route sparse for api path",
        sparse.mode in {"sparse", "hybrid"} and sparse.bm25_weight >= sparse.vector_weight,
    )

    dense = route_query("生产部署 Java API 的大致步骤是什么？", vector_available=True)
    _assert("route dense for open chinese", dense.mode in {"dense", "hybrid"} and dense.vector_weight > 0)

    store = build_rag_store()
    detailed = store.search_detailed("队列里的水印任务 JSON 字段有哪些？", top_k=6)
    sources = {h.source for h in detailed.chunks}
    _assert(
        "store search chinese queue question",
        "watermark-java-backend-tech-selection.md" in sources,
    )
    _assert("store search returns route", detailed.route.mode in {"sparse", "dense", "hybrid"})

    print("verify_rag_retrieval_quality=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
