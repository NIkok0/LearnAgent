from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.rag.ingest import load_chunks as _load_chunks  # noqa: E402
from copilot_agent.rag.retriever import RagStore, build_rag_store  # noqa: E402
from copilot_agent.scenario import load_scenario  # noqa: E402
from copilot_agent.scenario.bootstrap import apply_scenario_environment  # noqa: E402
from copilot_agent.settings import settings  # noqa: E402


def apply_verify_scenario(name: str = "watermark") -> None:
    os.environ.setdefault("SCENARIO", name)
    apply_scenario_environment(load_scenario(name))


def watermark_docs_dir() -> Path:
    return ROOT / "scenarios" / "watermark" / "docs"


def load_verify_chunks(*, sources: tuple[str, ...] | None = None) -> list[Any]:
    apply_verify_scenario("watermark")
    return _load_chunks(sources=sources)


def build_keyword_rag_store() -> RagStore:
    apply_verify_scenario("watermark")
    old_use_vector = settings.rag_use_vector
    old_rebuild = settings.rag_rebuild_index
    try:
        settings.rag_use_vector = False
        settings.rag_rebuild_index = False
        os.environ["RAG_USE_VECTOR"] = "false"
        return build_rag_store(sync_vector=False)
    finally:
        settings.rag_use_vector = old_use_vector
        settings.rag_rebuild_index = old_rebuild


def write_verify_summary(path: Path | str, payload: dict[str, Any]) -> Path:
    summary_path = Path(path).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary_path
