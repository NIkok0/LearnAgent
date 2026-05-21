#!/usr/bin/env python
"""Verify RAG incremental manifest delta (no vector deps required)."""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.rag.ingest import repo_docs_dir  # noqa: E402
from copilot_agent.rag.manifest import RagManifest, compute_delta, load_manifest, save_manifest  # noqa: E402
from copilot_agent.rag.schema import chunk_content_hash  # noqa: E402


def main() -> int:
    base = repo_docs_dir()
    if base is None:
        print("rag_incremental=SKIP (no docs dir)")
        return 0

    target = base / "DEPLOY-SERVER.md"
    if not target.is_file():
        print("rag_incremental=SKIP (DEPLOY-SERVER.md missing)")
        return 0

    manifest = load_manifest()
    before = compute_delta(manifest, docs_dir=base)

    original = target.read_text(encoding="utf-8")
    marker = f"\n<!-- incremental-test {time.time_ns()} -->\n"
    after = before
    try:
        empty = compute_delta(RagManifest.empty(), docs_dir=base)
        assert len(empty.changed) >= 1, "empty manifest should detect files to index"

        target.write_text(original + marker, encoding="utf-8")
        after = compute_delta(manifest, docs_dir=base)
        assert "DEPLOY-SERVER.md" in after.changed, "edited file should appear in delta.changed"
        assert after.removed == tuple(), "no removals expected for append"

        sample_hash = chunk_content_hash("sample")
        assert len(sample_hash) == 16
    finally:
        target.write_text(original, encoding="utf-8")
        save_manifest(manifest)

    print(f"docs_dir={base}")
    print(f"delta_changed_on_edit={list(after.changed)}")
    print("rag_incremental=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
