#!/usr/bin/env python
"""Verify authority-aware dedup keeps highest-authority chunk per heading."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.rag.fusion import dedup_chunks  # noqa: E402
from copilot_agent.rag.schema import DocChunk  # noqa: E402


def _chunk(source: str, heading: str, *, authority: int, start_line: int) -> DocChunk:
    return DocChunk(
        source=source,
        start_line=start_line,
        text=f"content for {heading}",
        section_title=heading,
        heading_path=heading,
        authority=authority,
    )


def main() -> int:
    ranked = [
        _chunk("policy.md", "Redis retry", authority=60, start_line=10),
        _chunk("policy.md", "Redis retry", authority=90, start_line=20),
        _chunk("policy.md", "Queue sizing", authority=70, start_line=30),
    ]
    deduped = dedup_chunks(ranked)
    by_heading = {c.heading_path: c for c in deduped}
    checks = {
        "dedup_count": len(deduped) == 2,
        "redis_retry_keeps_high_authority": by_heading.get("Redis retry") is not None
        and by_heading["Redis retry"].authority == 90
        and by_heading["Redis retry"].start_line == 20,
        "queue_sizing_preserved": by_heading.get("Queue sizing") is not None
        and by_heading["Queue sizing"].authority == 70,
    }
    overall = all(checks.values())
    print(f"checks={json.dumps(checks, ensure_ascii=False, sort_keys=True)}")
    print(f"rag_authority_dedup={'PASS' if overall else 'FAIL'}")
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
