from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from copilot_agent.rag.ingest import DOC_FILENAMES, load_chunks, repo_docs_dir
from copilot_agent.rag.manifest import (
    ManifestDelta,
    RagManifest,
    chroma_dir,
    compute_delta,
    load_manifest,
    remove_file_entry,
    save_manifest,
    update_file_entry,
)
from copilot_agent.rag.schema import DocChunk
from copilot_agent.settings import settings

log = logging.getLogger(__name__)


@dataclass
class VectorSyncResult:
    index: Any | None
    delta: ManifestDelta
    upserted_files: list[str] = field(default_factory=list)
    removed_files: list[str] = field(default_factory=list)
    upserted_chunks: int = 0
    skipped: bool = False


def _chunk_metadata(chunk: DocChunk) -> dict[str, str | int]:
    meta: dict[str, str | int] = {
        "source": chunk.source,
        "start_line": int(chunk.start_line),
        "section_title": chunk.section_title or "",
        "heading_path": chunk.heading_path or "",
        "doc_type": chunk.doc_type or "doc",
        "chunk_id": chunk.chunk_id,
        "chunk_index": int(chunk.chunk_index),
    }
    if chunk.updated_at:
        meta["updated_at"] = chunk.updated_at
    if chunk.api_endpoint is not None:
        meta["http_method"] = chunk.api_endpoint.method
        meta["http_path"] = chunk.api_endpoint.path
    return meta


def _get_embed_model():
    from llama_index.embeddings.huggingface import HuggingFaceEmbedding

    from copilot_agent.settings import apply_hf_home

    apply_hf_home(settings.hf_home)
    return HuggingFaceEmbedding(model_name=settings.rag_embedding_model)


def _load_index_from_collection(collection: Any, embed_model: Any) -> Any:
    from llama_index.core import Settings, VectorStoreIndex
    from llama_index.vector_stores.chroma import ChromaVectorStore

    Settings.embed_model = embed_model
    vector_store = ChromaVectorStore(chroma_collection=collection)
    return VectorStoreIndex.from_vector_store(vector_store=vector_store, embed_model=embed_model)


def _delete_chunk_ids(collection: Any, chunk_ids: list[str]) -> None:
    if not chunk_ids:
        return
    # Chroma batch delete; split large lists if needed
    batch = 500
    for i in range(0, len(chunk_ids), batch):
        collection.delete(ids=chunk_ids[i : i + batch])


def _upsert_chunks(collection: Any, embed_model: Any, chunks: list[DocChunk]) -> int:
    if not chunks:
        return 0
    ids = [c.chunk_id for c in chunks]
    documents = [c.text for c in chunks]
    metadatas = [_chunk_metadata(c) for c in chunks]
    embeddings = embed_model.get_text_embedding_batch(documents)
    collection.upsert(ids=ids, documents=documents, metadatas=metadatas, embeddings=embeddings)
    return len(chunks)


def sync_vector_index(chunks: list[DocChunk]) -> VectorSyncResult:
    """Incrementally upsert/delete Chroma vectors for changed files only."""
    empty_delta = ManifestDelta(changed=tuple(), removed=tuple())
    if not chunks or not settings.rag_use_vector:
        return VectorSyncResult(index=None, delta=empty_delta, skipped=True)

    try:
        import chromadb
    except ImportError as e:
        log.warning("Chroma unavailable (%s); keyword-only RAG.", e)
        return VectorSyncResult(index=None, delta=empty_delta, skipped=True)

    docs_dir = repo_docs_dir()
    if docs_dir is None:
        return VectorSyncResult(index=None, delta=empty_delta, skipped=True)

    manifest = load_manifest()
    if manifest.embedding_model != settings.rag_embedding_model:
        log.info(
            "Embedding model changed (%s -> %s); resetting vector index",
            manifest.embedding_model,
            settings.rag_embedding_model,
        )
        if collection.count() > 0:
            client.delete_collection("wm_docs")
            collection = client.get_or_create_collection("wm_docs")
        manifest = RagManifest.empty()

    if not manifest.files and collection.count() > 0 and not settings.rag_rebuild_index:
        log.info("Migrating legacy Chroma index to rag_manifest incremental layout")
        client.delete_collection("wm_docs")
        collection = client.get_or_create_collection("wm_docs")

    delta = compute_delta(manifest, docs_dir=docs_dir)
    if settings.rag_rebuild_index:
        delta = ManifestDelta(changed=tuple(DOC_FILENAMES), removed=tuple(manifest.files.keys()))

    client = chromadb.PersistentClient(path=str(chroma_dir()))
    collection = client.get_or_create_collection("wm_docs")
    embed_model = _get_embed_model()

    if (
        not settings.rag_rebuild_index
        and not delta.changed
        and not delta.removed
        and collection.count() > 0
        and manifest.files
    ):
        log.info("Vector index up to date (%d vectors); loading persisted index", collection.count())
        return VectorSyncResult(
            index=_load_index_from_collection(collection, embed_model),
            delta=delta,
            skipped=True,
        )

    if settings.rag_rebuild_index and collection.count() > 0:
        log.info("RAG_REBUILD_INDEX=true; clearing wm_docs collection")
        client.delete_collection("wm_docs")
        collection = client.get_or_create_collection("wm_docs")
        manifest = RagManifest.empty()
        delta = ManifestDelta(changed=tuple(DOC_FILENAMES), removed=tuple())

    delete_ids: list[str] = []
    for source in delta.removed:
        delete_ids.extend(remove_file_entry(manifest, source))
    for source in delta.changed:
        delete_ids.extend(remove_file_entry(manifest, source))

    _delete_chunk_ids(collection, delete_ids)

    upserted_files: list[str] = []
    upserted_chunks = 0
    for source in delta.changed:
        file_chunks = load_chunks(sources=(source,))
        if not file_chunks:
            manifest.files.pop(source, None)
            continue
        upserted_chunks += _upsert_chunks(collection, embed_model, file_chunks)
        update_file_entry(manifest, source, file_chunks, docs_dir=docs_dir)
        upserted_files.append(source)

    manifest.embedding_model = settings.rag_embedding_model
    save_manifest(manifest)

    log.info(
        "Vector sync: changed=%d removed=%d upserted_chunks=%d total_vectors=%d",
        len(delta.changed),
        len(delta.removed),
        upserted_chunks,
        collection.count(),
    )
    return VectorSyncResult(
        index=_load_index_from_collection(collection, embed_model),
        delta=delta,
        upserted_files=upserted_files,
        removed_files=list(delta.removed),
        upserted_chunks=upserted_chunks,
    )


def build_vector_index(chunks: list[DocChunk]) -> Any | None:
    """Backward-compatible entry: incremental sync when possible."""
    return sync_vector_index(chunks).index
