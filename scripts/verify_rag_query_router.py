#!/usr/bin/env python
"""Verify query routing selects sparse/dense/hybrid BM25+vector fusion weights."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.rag.query_router import route_query  # noqa: E402
from copilot_agent.rag.retriever import build_rag_store  # noqa: E402


def _assert(name: str, ok: bool) -> None:
    if not ok:
        raise SystemExit(f"FAIL: {name}")
    print(f"PASS: {name}")


def main() -> int:
    api = route_query("GET /actuator/health 返回什么状态？", vector_available=True)
    _assert("api query routes sparse", api.mode == "sparse")
    _assert("api query favors bm25", api.bm25_weight > api.vector_weight)

    open_q = route_query("需求检查表里有哪些已知偏差或风险点？", vector_available=True)
    _assert("open chinese routes dense or hybrid", open_q.mode in {"dense", "hybrid"})
    _assert("open chinese enables vector channel", open_q.vector_weight > 0)

    mixed = route_query("水印任务一直 QUEUED 怎么排查？", vector_available=True)
    _assert("troubleshooting routes hybrid", mixed.mode == "hybrid")
    _assert("hybrid mixes bm25 and vector", mixed.bm25_weight > 0 and mixed.vector_weight > 0)

    no_vec = route_query("POST /api/v1/jobs/watermark 默认 algorithmType 是什么？", vector_available=False)
    _assert("no vector zeroes vec weight", no_vec.vector_weight == 0.0)
    _assert("no vector keeps bm25", no_vec.bm25_weight > 0)

    store = build_rag_store()
    detailed = store.search_detailed("WM_JOBS_GROUP 消费者组默认值是什么？", top_k=6)
    _assert("store detailed includes route", detailed.route.mode in {"sparse", "hybrid"})
    _assert("exact constant query returns chunks", len(detailed.chunks) > 0)

    print("verify_rag_query_router=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
