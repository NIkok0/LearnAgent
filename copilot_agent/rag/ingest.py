from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path

from copilot_agent.rag.docs_resolver import resolve_docs_source
from copilot_agent.rag.api_parse import parse_api_section
from copilot_agent.rag.ingest_source import FileIngestSource, IngestSource
from copilot_agent.rag.preprocess import DocumentPreprocessor
from copilot_agent.rag.schema import DocChunk
from copilot_agent.rag.security import resolve_authority

log = logging.getLogger(__name__)


def resolve_ingest_source(base: Path | None = None) -> FileIngestSource:
    root = base if base is not None else repo_docs_dir()
    return FileIngestSource(root)


def repo_docs_dir() -> Path | None:
    """Compatibility wrapper for the Scenario-bound docs resolver."""
    return resolve_docs_source().docs_dir


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
    security_meta: dict[str, object] | None = None,
    source_format: str = "markdown",
    page_number: int | None = None,
    ocr_used: bool = False,
    ocr_required: bool = False,
) -> None:
    endpoint = api_meta.api_endpoint if api_meta is not None else None
    request_fields = list(api_meta.request_fields) if api_meta is not None else []
    response_fields = list(api_meta.response_fields) if api_meta is not None else []
    error_codes = list(api_meta.error_codes) if api_meta is not None else []
    security = security_meta or {}
    acl_raw = security.get("acl") or []
    acl = [str(item) for item in acl_raw] if isinstance(acl_raw, list) else []
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
            tenant_id=str(security.get("tenant_id") or "default"),
            doc_id=str(security.get("doc_id") or source),
            acl=acl,
            classification=str(security.get("classification") or "internal"),
            pii_level=str(security.get("pii_level") or "none"),
            source_hash=str(security.get("source_hash") or ""),
            retention_policy=str(security.get("retention_policy") or "default"),
            authority=resolve_authority(doc_type=doc_type, security_meta=security),
            source_format=source_format,
            page_number=page_number,
            ocr_used=ocr_used,
            ocr_required=ocr_required,
        )
    )


def _load_file_chunks(
    base: Path,
    name: str,
    *,
    max_chunk_chars: int,
    overlap: int,
    doc_type: str,
    security_meta: dict[str, object] | None = None,
) -> list[DocChunk]:
    path = base / name
    if not path.is_file():
        return []
    updated_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()
    chunks: list[DocChunk] = []
    chunk_index = 0
    document = DocumentPreprocessor().preprocess(path)
    for section in document.sections:
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
                security_meta=security_meta,
                source_format=section.source_format,
                page_number=section.page_number,
                ocr_used=section.ocr_used,
                ocr_required=section.ocr_required,
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
                    security_meta=security_meta,
                    source_format=section.source_format,
                    page_number=section.page_number,
                    ocr_used=section.ocr_used,
                    ocr_required=section.ocr_required,
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
            "project docs not found (set COPILOT_DOCS_PATH or add Markdown under scenarios/*/docs). RAG disabled."
        )
        return chunks
    manifest = source.manifest()
    names = sources if sources is not None else source.list_filenames()
    allowed = set(manifest.filenames(docs_dir=base))
    for name in names:
        if name not in allowed:
            continue
        doc_type = manifest.doc_type_for(name)
        security_meta = manifest.security_for(name)
        chunks.extend(
            _load_file_chunks(
                base,
                name,
                max_chunk_chars=max_chunk_chars,
                overlap=overlap,
                doc_type=doc_type,
                security_meta=security_meta,
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
