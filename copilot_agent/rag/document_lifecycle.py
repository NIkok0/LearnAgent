from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from copilot_agent.rag.docs_manifest import load_docs_manifest, remove_document_from_manifest
from copilot_agent.rag.index import delete_vector_chunks
from copilot_agent.rag.ingest import load_chunks, repo_docs_dir
from copilot_agent.settings import settings


@dataclass(frozen=True)
class RagDocumentDeleteResult:
    doc_id: str
    source_file: str
    tenant_id: str
    classification: str
    pii_level: str
    deleted_chunk_count: int
    vector_delete_attempted: bool
    vector_delete_success: bool
    vector_error: str | None
    reason: str
    deleted_at: str
    rag_status: dict[str, Any]
    source_hash: str = ""
    delete_event_id: int | None = None

    def audit_payload(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "source_file": self.source_file,
            "tenant_id": self.tenant_id,
            "classification": self.classification,
            "pii_level": self.pii_level,
            "deleted_chunk_count": self.deleted_chunk_count,
            "vector_delete_attempted": self.vector_delete_attempted,
            "vector_delete_success": self.vector_delete_success,
            "vector_error": self.vector_error,
            "reason": self.reason,
            "deleted_at": self.deleted_at,
            "source_hash": self.source_hash,
        }

    def as_response(self) -> dict[str, Any]:
        return {**self.audit_payload(), "rag": self.rag_status}

    def proof_payload(self, *, delete_event_id: int | None = None) -> dict[str, Any]:
        return {
            **self.audit_payload(),
            "delete_event_id": delete_event_id if delete_event_id is not None else self.delete_event_id,
            "proof_created_at": datetime.now(UTC).isoformat(),
            "proof_version": 1,
        }


@dataclass(frozen=True)
class RagDocumentIngestResult:
    doc_id: str
    source_file: str
    tenant_id: str
    classification: str
    pii_level: str
    retention_policy: str
    source_hash: str
    chunk_count: int
    ingested_at: str
    reload_success: bool
    rag_status: dict[str, Any]

    def audit_payload(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "source_file": self.source_file,
            "tenant_id": self.tenant_id,
            "classification": self.classification,
            "pii_level": self.pii_level,
            "retention_policy": self.retention_policy,
            "source_hash": self.source_hash,
            "chunk_count": self.chunk_count,
            "ingested_at": self.ingested_at,
            "reload_success": self.reload_success,
        }

    def as_response(self) -> dict[str, Any]:
        return {**self.audit_payload(), "rag": self.rag_status}


def document_source_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_ingest_result(
    *,
    filename: str,
    security: dict[str, object],
    text: str,
    rag_status: dict[str, Any],
    docs_dir: Path | None = None,
) -> RagDocumentIngestResult:
    source_hash = str(security.get("source_hash") or document_source_hash(text))
    base = docs_dir if docs_dir is not None else repo_docs_dir()
    chunk_count = 0
    if base is not None:
        chunk_count = sum(1 for chunk in load_chunks(sources=(filename,)) if chunk.source == filename)
    return RagDocumentIngestResult(
        doc_id=str(security.get("doc_id") or filename),
        source_file=filename,
        tenant_id=str(security.get("tenant_id") or "default"),
        classification=str(security.get("classification") or "internal"),
        pii_level=str(security.get("pii_level") or "none"),
        retention_policy=str(security.get("retention_policy") or "default"),
        source_hash=source_hash,
        chunk_count=chunk_count,
        ingested_at=datetime.now(UTC).isoformat(),
        reload_success=bool(rag_status),
        rag_status=rag_status,
    )


def list_rag_documents(*, docs_dir: Path | None = None) -> dict[str, Any]:
    base = docs_dir if docs_dir is not None else repo_docs_dir()
    if base is None:
        return {"docs_dir": None, "documents": [], "document_count": 0, "deleted_document_count": 0}
    manifest = load_docs_manifest(base)
    chunks = load_chunks()
    chunks_by_source: dict[str, list[Any]] = {}
    for chunk in chunks:
        chunks_by_source.setdefault(chunk.source, []).append(chunk)
    documents: list[dict[str, Any]] = []
    for filename in manifest.filenames(docs_dir=base):
        security = manifest.security_for(filename)
        file_chunks = chunks_by_source.get(filename, [])
        path = base / filename
        updated_at = ""
        if path.is_file():
            updated_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()
        elif file_chunks:
            updated_at = str(file_chunks[0].updated_at or "")
        source_hash = str(security.get("source_hash") or "")
        if not source_hash and path.is_file():
            source_hash = document_source_hash(path.read_text(encoding="utf-8"))
        documents.append(
            {
                "doc_id": str(security.get("doc_id") or filename),
                "source_file": filename,
                "tenant_id": str(security.get("tenant_id") or "default"),
                "classification": str(security.get("classification") or "internal"),
                "pii_level": str(security.get("pii_level") or "none"),
                "source_hash": source_hash,
                "retention_policy": str(security.get("retention_policy") or "default"),
                "doc_type": manifest.doc_type_for(filename),
                "chunk_count": len(file_chunks),
                "updated_at": updated_at,
                "deleted": False,
            }
        )
    return {
        "docs_dir": str(base),
        "documents": documents,
        "document_count": len(documents),
        "deleted_document_count": 0,
        "chunk_count": sum(int(item["chunk_count"]) for item in documents),
    }


def delete_rag_document(
    doc_id: str,
    *,
    manager: Any,
    reason: str = "api_delete",
    sync_vector: bool = True,
) -> RagDocumentDeleteResult:
    base = repo_docs_dir()
    if base is None:
        raise FileNotFoundError("docs dir not configured")
    before_chunks = load_chunks()
    before_by_source: dict[str, int] = {}
    for chunk in before_chunks:
        before_by_source[chunk.source] = before_by_source.get(chunk.source, 0) + 1

    _updated, source_file, security = remove_document_from_manifest(base, doc_id)
    if source_file is None:
        raise KeyError(doc_id)

    vector_delete_attempted = bool(settings.rag_use_vector)
    vector_delete_success = not vector_delete_attempted
    vector_error: str | None = None
    deleted_chunk_count = before_by_source.get(source_file, 0)
    source_hash = str(security.get("source_hash") or "")
    path = base / source_file
    if not source_hash and path.is_file():
        source_hash = document_source_hash(path.read_text(encoding="utf-8"))
    if vector_delete_attempted:
        try:
            deleted_chunk_count = max(deleted_chunk_count, delete_vector_chunks(source_file))
            vector_delete_success = True
        except Exception as exc:
            vector_error = str(exc)
            vector_delete_success = False

    if path.is_file():
        path.unlink()

    rag_status = manager.reload(trigger="api", sync_vector=sync_vector)
    return RagDocumentDeleteResult(
        doc_id=str(security.get("doc_id") or source_file),
        source_file=source_file,
        tenant_id=str(security.get("tenant_id") or "default"),
        classification=str(security.get("classification") or "internal"),
        pii_level=str(security.get("pii_level") or "none"),
        deleted_chunk_count=deleted_chunk_count,
        vector_delete_attempted=vector_delete_attempted,
        vector_delete_success=vector_delete_success,
        vector_error=vector_error,
        reason=reason,
        deleted_at=datetime.now(UTC).isoformat(),
        rag_status=rag_status,
        source_hash=source_hash,
    )
