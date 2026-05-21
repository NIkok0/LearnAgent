#!/usr/bin/env python
"""Verify RAG hot reload: fingerprint detect + keyword store swap on MemoryManager."""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.memory.manager import MemoryManager  # noqa: E402
from copilot_agent.rag.ingest import docs_source_fingerprint, repo_docs_dir  # noqa: E402
from copilot_agent.rag.manifest import compute_delta, load_manifest  # noqa: E402
from copilot_agent.rag.reload import RagStoreManager  # noqa: E402


def main() -> int:
    base = repo_docs_dir()
    if base is None:
        print("rag_hot_reload=SKIP (no docs dir)")
        return 0

    target = base / "README.md"
    if not target.is_file():
        print("rag_hot_reload=SKIP (README.md missing)")
        return 0

    manager = RagStoreManager(trigger="cli")
    memory = MemoryManager(rag_store=manager.store, event_store=None, checkpoint_path=":memory:")
    manager.attach_memory(memory)
    fp0 = manager.status()["fingerprint"]
    chunks0 = len(manager.store.chunks)

    original = target.read_text(encoding="utf-8")
    marker = f"\n<!-- hot-reload-test {time.time_ns()} -->\n"
    try:
        target.write_text(original + marker, encoding="utf-8")
        assert docs_source_fingerprint() != fp0, "fingerprint should change after edit"

        delta = compute_delta(load_manifest(), docs_dir=base)
        assert "README.md" in delta.changed, "manifest delta should include README.md"

        changed = manager.check_and_reload_if_changed()
        assert changed, "check_and_reload_if_changed should reload"
        assert manager.status()["fingerprint"] != fp0, "manager fingerprint should update"
        assert len(manager.store.chunks) >= chunks0, "chunk count should not shrink for append"
        assert memory.rag_store is manager.store, "MemoryManager should point at same store"

        manual = manager.reload(trigger="api", sync_vector=True)
        assert manual["chunk_count"] >= chunks0
        assert memory.rag_store is manager.store
    finally:
        target.write_text(original, encoding="utf-8")
        manager.reload(trigger="cli", sync_vector=True)

    print(f"docs_dir={base}")
    print(f"chunks={chunks0}")
    print("rag_hot_reload=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
