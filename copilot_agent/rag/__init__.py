from copilot_agent.rag.ingest import load_chunks
from copilot_agent.rag.schema import DocChunk, format_chunks_for_prompt

__all__ = [
    "DocChunk",
    "RagStore",
    "RagStoreManager",
    "build_rag_store",
    "format_chunks_for_prompt",
    "load_chunks",
    "sync_rag_store_vectors",
]


def __getattr__(name: str):
    if name == "RagStoreManager":
        from copilot_agent.rag.reload import RagStoreManager

        return RagStoreManager
    if name in {"RagStore", "build_rag_store", "sync_rag_store_vectors"}:
        from copilot_agent.rag.retriever import RagStore, build_rag_store, sync_rag_store_vectors

        return {
            "RagStore": RagStore,
            "build_rag_store": build_rag_store,
            "sync_rag_store_vectors": sync_rag_store_vectors,
        }[name]
    raise AttributeError(name)
