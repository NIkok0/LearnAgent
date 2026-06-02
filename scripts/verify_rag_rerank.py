#!/usr/bin/env python
"""Verify optional cross-encoder rerank without forcing network/model downloads."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("SCENARIO", "watermark")

from copilot_agent.rag.retriever import build_rag_store  # noqa: E402
from copilot_agent.rag.schema import DocChunk  # noqa: E402


def _assert(name: str, ok: bool) -> None:
    if not ok:
        raise SystemExit(f"FAIL: {name}")
    print(f"PASS: {name}")


def _verify_model_rerank() -> str:
    from copilot_agent.rag.rerank import rerank_available, rerank_chunks  # noqa: WPS433
    from copilot_agent.settings import settings  # noqa: WPS433

    if not rerank_available():
        print("rerank_mode=SKIP (sentence-transformers not installed)")
        return "SKIP"

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

    os.environ["RAG_RERANK_ENABLED"] = "true"
    settings.rag_rerank_enabled = True
    ranked = rerank_chunks(query, chunks, top_k=2)
    _assert("rerank promotes relevant chunk", ranked[0].source in {"tech.md", "tech2.md"})
    _assert("rerank returns top_k", len(ranked) == 2)
    print("rerank_mode=enabled")
    return "enabled"


def main() -> int:
    force_model = os.environ.get("RAG_RERANK_VERIFY_MODEL", "").strip().lower() in {"1", "true", "yes"}
    if force_model:
        rerank_mode = _verify_model_rerank()
    else:
        rerank_mode = "SKIP"
        print("rerank_mode=SKIP (set RAG_RERANK_VERIFY_MODEL=true to load cross-encoder)")

    os.environ["RAG_RERANK_ENABLED"] = "false"
    from copilot_agent.settings import settings  # noqa: WPS433

    settings.rag_rerank_enabled = False
    store = build_rag_store()
    hits = store.search("Redis Stream default key wm:jobs:stream", top_k=3)
    search_ok = len(hits) > 0
    _assert("store search with rerank disabled", search_ok)

    checks = {
        "store_search_with_rerank_disabled": search_ok,
        "model_rerank_optional": rerank_mode in {"enabled", "SKIP"},
    }
    summary = {
        "checks": checks,
        "rerank_mode": rerank_mode,
        "verify_rag_rerank": "PASS" if all(checks.values()) else "FAIL",
    }
    summary_path = ROOT / "artifacts/runtime/rag-rerank-summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"summary_json={summary_path}")
    print("verify_rag_rerank=PASS")
    return 0 if summary["verify_rag_rerank"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
