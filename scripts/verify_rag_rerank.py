#!/usr/bin/env python
"""Verify optional cross-encoder rerank (B3)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.rag.rerank import rerank_available, rerank_chunks  # noqa: E402
from copilot_agent.rag.retriever import build_rag_store  # noqa: E402
from copilot_agent.rag.schema import DocChunk  # noqa: E402


def _assert(name: str, ok: bool) -> None:
    if not ok:
        raise SystemExit(f"FAIL: {name}")
    print(f"PASS: {name}")


def main() -> int:
    chunks = [
        DocChunk(
            source="tech.md",
            start_line=1,
            text="Redis Stream wm:jobs:stream default key for watermark jobs queue",
        ),
        DocChunk(
            source="noise.md",
            start_line=1,
            text="unrelated deployment kubernetes helm chart generic guide",
        ),
        DocChunk(
            source="tech2.md",
            start_line=10,
            text="WM_JOBS_GROUP consumer group wm-workers processes queue messages",
        ),
    ]
    query = "Redis Stream default key wm:jobs:stream"

    if rerank_available():
        os.environ["RAG_RERANK_ENABLED"] = "true"
        from copilot_agent.settings import settings  # noqa: WPS433

        settings.rag_rerank_enabled = True
        ranked = rerank_chunks(query, chunks, top_k=2)
        _assert("rerank promotes relevant chunk", ranked[0].source in {"tech.md", "tech2.md"})
        _assert("rerank returns top_k", len(ranked) == 2)

        store = build_rag_store()
        hits = store.search("Redis Stream 的 key 默认叫什么？", top_k=3)
        _assert("store search with rerank enabled", len(hits) > 0)
        print("rerank_mode=enabled")
    else:
        print("rerank_mode=SKIP (sentence-transformers not installed)")

    os.environ["RAG_RERANK_ENABLED"] = "false"
    from copilot_agent.settings import settings  # noqa: WPS433

    settings.rag_rerank_enabled = False
    store_off = build_rag_store()
    hits_off = store_off.search("Redis Stream 的 key 默认叫什么？", top_k=3)
    _assert("store search with rerank disabled", len(hits_off) > 0)

    print("verify_rag_rerank=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
