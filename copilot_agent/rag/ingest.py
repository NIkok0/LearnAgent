from __future__ import annotations

import logging
import os
from pathlib import Path

from copilot_agent.rag.schema import DocChunk

log = logging.getLogger(__name__)

DOC_FILENAMES = (
    "DEPLOY-SERVER.md",
    "REQUIREMENTS-CHECKLIST-AND-TEST-CASES.md",
    "watermark-java-backend-tech-selection.md",
)


def repo_docs_dir() -> Path | None:
    """Locate project docs from env, local sample docs, or a parent `backend-java/docs`."""
    env = os.environ.get("WATERMARK_DOCS_PATH", "").strip()
    if env:
        p = Path(env)
        return p if p.is_dir() else None
    here = Path(__file__).resolve()
    for base in here.parents:
        cand = base / "docs" / "source"
        if cand.is_dir():
            return cand
    for base in here.parents:
        cand = base / "backend-java" / "docs"
        if cand.is_dir():
            return cand
    return None


def load_chunks(max_chunk_chars: int = 1400, overlap: int = 200) -> list[DocChunk]:
    base = repo_docs_dir()
    chunks: list[DocChunk] = []
    if base is None:
        log.warning(
            "project docs not found (set WATERMARK_DOCS_PATH or add Markdown files under docs/source). RAG disabled."
        )
        return chunks
    for name in DOC_FILENAMES:
        path = base / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        sections: list[tuple[int, str]] = []
        buf: list[str] = []
        start = 1
        for i, line in enumerate(lines, start=1):
            if line.startswith("#") and buf:
                sections.append((start, "\n".join(buf)))
                buf = [line]
                start = i
            else:
                buf.append(line)
        if buf:
            sections.append((start, "\n".join(buf)))
        for start_line, sec in sections:
            if len(sec) <= max_chunk_chars:
                chunks.append(DocChunk(source=name, start_line=start_line, text=sec))
            else:
                step = max_chunk_chars - overlap
                pos = 0
                offset_line = start_line
                while pos < len(sec):
                    piece = sec[pos : pos + max_chunk_chars]
                    chunks.append(DocChunk(source=name, start_line=offset_line, text=piece))
                    pos += step
    return chunks
