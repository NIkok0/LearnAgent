from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from copilot_agent.rag.api_parse import parse_api_section
from copilot_agent.rag.ingest_source import FileIngestSource, IngestSource
from copilot_agent.rag.schema import DocChunk

log = logging.getLogger(__name__)

# Legacy constants kept for tests and backward-compatible imports.
DOC_FILENAMES = (
    "REQUIREMENTS-CHECKLIST-AND-TEST-CASES.md",
    "API-CONTRACT.md",
    "DEPLOY-SERVER.md",
    "SECURITY-BASELINE.md",
    "RUNBOOK.md",
    "OPERATIONS-SLO-SLA.md",
    "watermark-java-backend-tech-selection.md",
    "README.md",
    "README_ALGORITHM.md",
)

DOC_TYPE_BY_FILE: dict[str, str] = {
    "API-CONTRACT.md": "api_contract",
    "DEPLOY-SERVER.md": "deploy",
    "SECURITY-BASELINE.md": "security",
    "RUNBOOK.md": "runbook",
    "OPERATIONS-SLO-SLA.md": "operations",
    "REQUIREMENTS-CHECKLIST-AND-TEST-CASES.md": "requirements",
    "watermark-java-backend-tech-selection.md": "tech_selection",
    "README.md": "overview",
    "README_ALGORITHM.md": "algorithm",
}


@dataclass(frozen=True)
class _Section:
    start_line: int
    text: str
    section_title: str
    heading_path: str


def resolve_ingest_source(base: Path | None = None) -> FileIngestSource:
    root = base if base is not None else repo_docs_dir()
    return FileIngestSource(root)


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


def _heading_level(line: str) -> int:
    stripped = line.lstrip()
    if not stripped.startswith("#"):
        return 0
    return len(stripped) - len(stripped.lstrip("#"))


def _heading_title(line: str) -> str:
    return line.lstrip("#").strip()


def _update_heading_stack(stack: list[tuple[int, str]], line: str) -> None:
    level = _heading_level(line)
    if level == 0:
        return
    title = _heading_title(line)
    while stack and stack[-1][0] >= level:
        stack.pop()
    stack.append((level, title))


def _heading_path(stack: list[tuple[int, str]]) -> str:
    return " > ".join(title for _, title in stack)


def _split_sections(lines: list[str]) -> list[_Section]:
    heading_stack: list[tuple[int, str]] = []
    buf: list[str] = []
    start = 1
    section_title = ""
    heading_path = ""
    sections: list[_Section] = []

    for i, line in enumerate(lines, start=1):
        if line.startswith("#"):
            _update_heading_stack(heading_stack, line)
            if buf:
                sections.append(
                    _Section(
                        start_line=start,
                        text="\n".join(buf),
                        section_title=section_title,
                        heading_path=heading_path,
                    )
                )
            buf = [line]
            start = i
            section_title = _heading_title(line)
            heading_path = _heading_path(heading_stack)
        else:
            if not buf:
                buf = [line]
                start = i
            else:
                buf.append(line)

    if buf:
        sections.append(
            _Section(
                start_line=start,
                text="\n".join(buf),
                section_title=section_title,
                heading_path=heading_path,
            )
        )
    return sections


def _append_chunk(
    chunks: list[DocChunk],
    *,
    source: str,
    start_line: int,
    text: str,
    section_title: str,
    heading_path: str,
    doc_type: str,
    chunk_index: int,
    updated_at: str,
    api_meta=None,
) -> None:
    endpoint = api_meta.api_endpoint if api_meta is not None else None
    request_fields = list(api_meta.request_fields) if api_meta is not None else []
    response_fields = list(api_meta.response_fields) if api_meta is not None else []
    error_codes = list(api_meta.error_codes) if api_meta is not None else []
    chunks.append(
        DocChunk(
            source=source,
            start_line=start_line,
            text=text,
            section_title=section_title,
            heading_path=heading_path,
            doc_type=doc_type,
            chunk_index=chunk_index,
            updated_at=updated_at,
            api_endpoint=endpoint,
            request_fields=request_fields,
            response_fields=response_fields,
            error_codes=error_codes,
        )
    )


def _load_file_chunks(
    base: Path,
    name: str,
    *,
    max_chunk_chars: int,
    overlap: int,
    doc_type: str,
) -> list[DocChunk]:
    path = base / name
    if not path.is_file():
        return []
    updated_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()
    chunks: list[DocChunk] = []
    chunk_index = 0
    lines = path.read_text(encoding="utf-8").splitlines()
    for section in _split_sections(lines):
        title = section.section_title
        path_str = section.heading_path
        api_meta = parse_api_section(section_title=title, text=section.text, heading_path=path_str) if doc_type == "api_contract" else None
        if len(section.text) <= max_chunk_chars:
            _append_chunk(
                chunks,
                source=name,
                start_line=section.start_line,
                text=section.text,
                section_title=title,
                heading_path=path_str,
                doc_type=doc_type,
                chunk_index=chunk_index,
                updated_at=updated_at,
                api_meta=api_meta,
            )
            chunk_index += 1
        else:
            step = max_chunk_chars - overlap
            pos = 0
            offset_line = section.start_line
            while pos < len(section.text):
                piece = section.text[pos : pos + max_chunk_chars]
                _append_chunk(
                    chunks,
                    source=name,
                    start_line=offset_line,
                    text=piece,
                    section_title=title,
                    heading_path=path_str,
                    doc_type=doc_type,
                    chunk_index=chunk_index,
                    updated_at=updated_at,
                    api_meta=api_meta,
                )
                chunk_index += 1
                pos += step
    return chunks


def load_chunks(
    max_chunk_chars: int = 1400,
    overlap: int = 200,
    *,
    sources: tuple[str, ...] | None = None,
    ingest_source: IngestSource | None = None,
) -> list[DocChunk]:
    source = ingest_source or resolve_ingest_source()
    base = source.docs_dir()
    chunks: list[DocChunk] = []
    if base is None:
        log.warning(
            "project docs not found (set WATERMARK_DOCS_PATH or add Markdown files under docs/source). RAG disabled."
        )
        return chunks
    manifest = source.manifest()
    names = sources if sources is not None else source.list_filenames()
    allowed = set(manifest.filenames(docs_dir=base))
    for name in names:
        if name not in allowed:
            continue
        doc_type = manifest.doc_type_for(name)
        chunks.extend(
            _load_file_chunks(
                base,
                name,
                max_chunk_chars=max_chunk_chars,
                overlap=overlap,
                doc_type=doc_type,
            )
        )
    return chunks


def docs_source_fingerprint(base: Path | None = None, *, ingest_source: IngestSource | None = None) -> str:
    """Cheap change detector for watched markdown sources (mtime + size per file)."""
    source = ingest_source or resolve_ingest_source()
    root = base if base is not None else source.docs_dir()
    if root is None:
        return ""
    h = hashlib.sha256()
    for name in source.list_filenames():
        path = root / name
        if not path.is_file():
            h.update(name.encode("utf-8"))
            h.update(b"missing")
            continue
        stat = path.stat()
        h.update(name.encode("utf-8"))
        h.update(str(stat.st_mtime_ns).encode("ascii"))
        h.update(str(stat.st_size).encode("ascii"))
    return h.hexdigest()
