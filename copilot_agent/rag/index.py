from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from copilot_agent.rag.schema import DocChunk
from copilot_agent.settings import settings

log = logging.getLogger(__name__)


def _chroma_dir() -> Path:
    if settings.rag_chroma_path.strip():
        return Path(settings.rag_chroma_path)
    return Path(__file__).resolve().parent.parent.parent / "storage" / "chroma"


def _fingerprint(chunks: list[DocChunk]) -> str:
    h = hashlib.sha256()
    for c in sorted(chunks, key=lambda x: (x.source, x.start_line)):
        h.update(c.source.encode("utf-8"))
        h.update(str(c.start_line).encode("utf-8"))
        h.update(c.text.encode("utf-8"))
    return h.hexdigest()


def _write_fingerprint(path: Path, fp: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fingerprint": fp}, indent=2), encoding="utf-8")


def _read_fingerprint(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return str(data.get("fingerprint", "")) or None
    except (json.JSONDecodeError, OSError):
        return None


def build_vector_index(chunks: list[DocChunk]) -> Any | None:
    """Build or load a persisted Chroma index. Returns LlamaIndex VectorStoreIndex or None."""
    if not chunks or not settings.rag_use_vector:
        return None
    try:
        import chromadb
        from llama_index.core import Document, Settings, StorageContext, VectorStoreIndex
        from llama_index.embeddings.huggingface import HuggingFaceEmbedding
        from llama_index.vector_stores.chroma import ChromaVectorStore
    except ImportError as e:
        log.warning("LlamaIndex vector stack unavailable (%s); keyword-only RAG.", e)
        return None

    from copilot_agent.settings import apply_hf_home

    apply_hf_home(settings.hf_home)

    chroma_path = _chroma_dir()
    fp_path = chroma_path / "wm_docs_fingerprint.json"
    fp = _fingerprint(chunks)

    Settings.embed_model = HuggingFaceEmbedding(model_name=settings.rag_embedding_model)
    client = chromadb.PersistentClient(path=str(chroma_path))
    collection = client.get_or_create_collection("wm_docs")
    vector_store = ChromaVectorStore(chroma_collection=collection)

    stored_fp = _read_fingerprint(fp_path)
    if collection.count() > 0 and stored_fp == fp and not settings.rag_rebuild_index:
        log.info("Loading persisted Chroma index from %s (%d vectors)", chroma_path, collection.count())
        return VectorStoreIndex.from_vector_store(
            vector_store=vector_store,
            embed_model=Settings.embed_model,
        )

    if collection.count() > 0:
        log.info("Rebuilding Chroma index (fingerprint changed or RAG_REBUILD_INDEX=true)")
        client.delete_collection("wm_docs")
        collection = client.get_or_create_collection("wm_docs")
        vector_store = ChromaVectorStore(chroma_collection=collection)

    documents = [
        Document(
            text=c.text,
            metadata={"source": c.source, "start_line": c.start_line},
        )
        for c in chunks
    ]
    storage_context = StorageContext.from_defaults(vector_store=vector_store)
    index = VectorStoreIndex.from_documents(
        documents,
        storage_context=storage_context,
        show_progress=False,
    )
    _write_fingerprint(fp_path, fp)
    log.info("Built Chroma index with %d chunks at %s", len(chunks), chroma_path)
    return index
