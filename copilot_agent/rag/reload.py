from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from typing import Any, Literal

from copilot_agent.rag.docs_resolver import resolve_docs_source
from copilot_agent.rag.ingest import docs_source_fingerprint, load_chunks
from copilot_agent.rag.retriever import RagStore, build_rag_store, sync_rag_store_vectors
from copilot_agent.settings import settings

log = logging.getLogger(__name__)

ReloadTrigger = Literal["startup", "api", "watch", "cli"]
VectorIndexStatus = Literal["disabled", "ready", "rebuilding", "stale", "failed"]


class RagStoreManager:
    """Process-local RAG store with keyword-first hot reload and incremental vector sync."""

    def __init__(self, *, trigger: ReloadTrigger = "startup") -> None:
        self._store = build_rag_store(sync_vector=True)
        self._fingerprint = docs_source_fingerprint()
        self._last_reload_at = datetime.now(UTC).isoformat()
        self._last_trigger: ReloadTrigger = trigger
        self._memory: Any | None = None
        self._vector_status: VectorIndexStatus = self._initial_vector_status()
        self._last_vector_sync_at: str | None = (
            self._last_reload_at if self._store.vector_enabled else None
        )
        self._last_vector_sync: dict[str, Any] = {}
        self._vector_error: str | None = None
        self._vector_lock = threading.Lock()
        self._vector_thread: threading.Thread | None = None
        self._pending_vector_reload = False

    def _initial_vector_status(self) -> VectorIndexStatus:
        if not settings.rag_use_vector:
            return "disabled"
        return "ready" if self._store.vector_enabled else "stale"

    @property
    def store(self) -> RagStore:
        return self._store

    def attach_memory(self, memory: Any) -> None:
        self._memory = memory
        memory.reload_rag_store(self._store)

    def reload(self, *, trigger: ReloadTrigger = "api", sync_vector: bool | None = None) -> dict[str, Any]:
        new_fp = docs_source_fingerprint()
        chunks = load_chunks()
        self._store.replace_chunks(chunks)
        self._fingerprint = new_fp
        self._last_reload_at = datetime.now(UTC).isoformat()
        self._last_trigger = trigger
        if self._memory is not None:
            self._memory.reload_rag_store(self._store)

        log.info(
            "RAG keyword reload trigger=%s chunks=%d fp=%s…",
            trigger,
            len(chunks),
            new_fp[:12],
        )

        if not settings.rag_use_vector:
            self._vector_status = "disabled"
            return self.status()

        do_async = settings.rag_vector_async_reload if sync_vector is None else not sync_vector
        if do_async:
            self._vector_status = "rebuilding" if self._store.vector_enabled else "stale"
            self._schedule_vector_sync()
        else:
            self._run_vector_sync_blocking()

        return self.status()

    def _schedule_vector_sync(self) -> None:
        with self._vector_lock:
            if self._vector_thread is not None and self._vector_thread.is_alive():
                self._pending_vector_reload = True
                return
            self._pending_vector_reload = False
            self._vector_thread = threading.Thread(target=self._vector_worker, name="rag-vector-sync", daemon=True)
            self._vector_thread.start()

    def _vector_worker(self) -> None:
        while True:
            try:
                sync_info = sync_rag_store_vectors(self._store)
                with self._vector_lock:
                    self._last_vector_sync = sync_info
                    self._last_vector_sync_at = datetime.now(UTC).isoformat()
                    self._vector_error = None
                    self._vector_status = "ready" if self._store.vector_enabled else "stale"
            except Exception as exc:
                log.exception("RAG vector sync failed")
                with self._vector_lock:
                    self._vector_error = str(exc)
                    self._vector_status = "failed"
            with self._vector_lock:
                if not self._pending_vector_reload:
                    break
                self._pending_vector_reload = False

    def _run_vector_sync_blocking(self) -> None:
        try:
            sync_info = sync_rag_store_vectors(self._store)
            self._last_vector_sync = sync_info
            self._last_vector_sync_at = datetime.now(UTC).isoformat()
            self._vector_error = None
            self._vector_status = "ready" if self._store.vector_enabled else "stale"
        except Exception as exc:
            log.exception("RAG vector sync failed")
            self._vector_error = str(exc)
            self._vector_status = "failed"

    def check_and_reload_if_changed(self) -> bool:
        current = docs_source_fingerprint()
        if current == self._fingerprint:
            return False
        self.reload(trigger="watch")
        return True

    def status(self) -> dict[str, Any]:
        docs_source = resolve_docs_source()
        base = docs_source.docs_dir
        with self._vector_lock:
            vector_status = self._vector_status
            vector_sync = dict(self._last_vector_sync)
            vector_error = self._vector_error
            vector_sync_at = self._last_vector_sync_at
            vector_rebuilding = self._vector_thread is not None and self._vector_thread.is_alive()

        return {
            "docs_dir": str(base) if base is not None else None,
            "docs_source": docs_source.as_dict(),
            "chunk_count": len(self._store.chunks),
            "vector_enabled": self._store.vector_enabled,
            "vector_index_status": vector_status,
            "vector_rebuilding": vector_rebuilding,
            "last_vector_sync_at": vector_sync_at,
            "last_vector_sync": vector_sync,
            "vector_error": vector_error,
            "fingerprint": self._fingerprint,
            "last_reload_at": self._last_reload_at,
            "last_reload_trigger": self._last_trigger,
        }
