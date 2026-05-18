#!/usr/bin/env python
"""Rebuild the persisted Chroma index for backend-java/docs."""

from __future__ import annotations

import os
import sys

# Allow running from repo: python scripts/build_index.py
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from copilot_agent.settings import settings  # noqa: E402

settings.rag_use_vector = True
settings.rag_rebuild_index = True

from copilot_agent.rag import build_rag_store  # noqa: E402


def main() -> None:
    store = build_rag_store()
    print(f"chunks={len(store.chunks)} vector_enabled={store.vector_enabled}")


if __name__ == "__main__":
    main()
