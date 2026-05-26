from __future__ import annotations

import hashlib
import sqlite3
from threading import Lock
from typing import Any
from uuid import uuid4

from copilot_agent.memory.eviction_policy import memory_eviction_score
from copilot_agent.memory.item_schema import MemoryItemRecord, MemoryScope, MemoryType
from copilot_agent.memory.item_store_queries import build_list_active_query, build_list_items_query
from copilot_agent.memory.item_store_rows import encode_embedding, encode_history, row_to_memory_item
from copilot_agent.runtime.event_store import utc_now_iso


def content_hash(content: str) -> str:
    normalized = " ".join((content or "").split()).strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


DELETED_MEMORY_CONTENT = "[deleted memory item]"


class MemoryItemStore:
    """SQLite-backed structured long-term memory items (same DB file as EventStore)."""

    def __init__(self, db_path: str) -> None:
        self.path = str(db_path)
        self._lock = Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_items (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    thread_id TEXT NULL,
                    scope TEXT NOT NULL,
                    memory_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    importance REAL NOT NULL DEFAULT 0.5,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    embedding_json TEXT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    supersedes_id TEXT NULL,
                    is_deprecated INTEGER NOT NULL DEFAULT 0,
                    expires_at TEXT NULL,
                    access_count INTEGER NOT NULL DEFAULT 0,
                    last_accessed_at TEXT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    source_run_id TEXT NULL,
                    pending_confirmation INTEGER NOT NULL DEFAULT 0,
                    history_json TEXT NOT NULL DEFAULT '[]'
                );

                CREATE INDEX IF NOT EXISTS idx_memory_items_user_scope
                    ON memory_items(user_id, scope, is_deprecated);
                CREATE INDEX IF NOT EXISTS idx_memory_items_thread
                    ON memory_items(thread_id, is_deprecated);
                CREATE INDEX IF NOT EXISTS idx_memory_items_expires
                    ON memory_items(expires_at);
                """
            )
            self._migrate_items(conn)

    def _migrate_items(self, conn: sqlite3.Connection) -> None:
        columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(memory_items)").fetchall()}
        if "pending_confirmation" not in columns:
            conn.execute(
                "ALTER TABLE memory_items ADD COLUMN pending_confirmation INTEGER NOT NULL DEFAULT 0"
            )

    def insert(self, item: MemoryItemRecord) -> MemoryItemRecord:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO memory_items (
                    id, user_id, thread_id, scope, memory_type, content, content_hash,
                    importance, confidence, embedding_json, version, supersedes_id,
                    is_deprecated, expires_at, access_count, last_accessed_at,
                    created_at, updated_at, source_run_id, pending_confirmation, history_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.id,
                    item.user_id,
                    item.thread_id,
                    item.scope.value,
                    item.memory_type.value,
                    item.content,
                    item.content_hash,
                    item.importance,
                    item.confidence,
                    encode_embedding(item),
                    item.version,
                    item.supersedes_id,
                    1 if item.is_deprecated else 0,
                    item.expires_at,
                    item.access_count,
                    item.last_accessed_at,
                    item.created_at,
                    item.updated_at,
                    item.source_run_id,
                    1 if item.pending_confirmation else 0,
                    encode_history(item),
                ),
            )
        return item

    def update(self, item: MemoryItemRecord) -> MemoryItemRecord:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE memory_items SET
                    content = ?, content_hash = ?, importance = ?, confidence = ?,
                    embedding_json = ?, version = ?, supersedes_id = ?,
                    is_deprecated = ?, expires_at = ?, access_count = ?,
                    last_accessed_at = ?, updated_at = ?, pending_confirmation = ?, history_json = ?
                WHERE id = ?
                """,
                (
                    item.content,
                    item.content_hash,
                    item.importance,
                    item.confidence,
                    encode_embedding(item),
                    item.version,
                    item.supersedes_id,
                    1 if item.is_deprecated else 0,
                    item.expires_at,
                    item.access_count,
                    item.last_accessed_at,
                    item.updated_at,
                    1 if item.pending_confirmation else 0,
                    encode_history(item),
                    item.id,
                ),
            )
        return item

    def deprecate(self, item_id: str, *, history_entry: dict[str, Any] | None = None) -> None:
        row = self.get(item_id)
        if row is None:
            return
        history = list(row.history)
        if history_entry:
            history.append(history_entry)
        updated = MemoryItemRecord(
            id=row.id,
            user_id=row.user_id,
            thread_id=row.thread_id,
            scope=row.scope,
            memory_type=row.memory_type,
            content=row.content,
            content_hash=row.content_hash,
            importance=row.importance,
            confidence=row.confidence,
            version=row.version,
            supersedes_id=row.supersedes_id,
            is_deprecated=True,
            pending_confirmation=row.pending_confirmation,
            expires_at=row.expires_at,
            access_count=row.access_count,
            last_accessed_at=row.last_accessed_at,
            created_at=row.created_at,
            updated_at=utc_now_iso(),
            source_run_id=row.source_run_id,
            history=history,
            embedding=row.embedding,
        )
        self.update(updated)

    def delete_with_tombstone(self, item_id: str, *, history_entry: dict[str, Any] | None = None) -> MemoryItemRecord | None:
        row = self.get(item_id)
        if row is None:
            return None
        history = list(row.history)
        if history_entry:
            history.append(history_entry)
        updated = MemoryItemRecord(
            id=row.id,
            user_id=row.user_id,
            thread_id=row.thread_id,
            scope=row.scope,
            memory_type=row.memory_type,
            content=DELETED_MEMORY_CONTENT,
            content_hash=content_hash(DELETED_MEMORY_CONTENT),
            importance=0.0,
            confidence=0.0,
            version=row.version,
            supersedes_id=row.supersedes_id,
            is_deprecated=True,
            pending_confirmation=False,
            expires_at=row.expires_at,
            access_count=row.access_count,
            last_accessed_at=row.last_accessed_at,
            created_at=row.created_at,
            updated_at=utc_now_iso(),
            source_run_id=row.source_run_id,
            history=history,
            embedding=None,
        )
        return self.update(updated)

    def get(self, item_id: str) -> MemoryItemRecord | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM memory_items WHERE id = ?", (item_id,)).fetchone()
        return row_to_memory_item(row) if row is not None else None

    def list_active(
        self,
        *,
        user_id: str,
        thread_id: str | None = None,
        scopes: tuple[MemoryScope, ...] | None = None,
        include_pending: bool = False,
    ) -> list[MemoryItemRecord]:
        sql, params = build_list_active_query(
            user_id=user_id,
            thread_id=thread_id,
            scopes=scopes,
            include_pending=include_pending,
        )
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        now = utc_now_iso()
        items = [row_to_memory_item(row) for row in rows]
        return [item for item in items if item.expires_at is None or item.expires_at > now]

    def list_items(
        self,
        *,
        user_id: str,
        thread_id: str | None = None,
        status: str = "active",
        scopes: tuple[MemoryScope, ...] | None = None,
        limit: int = 100,
    ) -> list[MemoryItemRecord]:
        sql, params = build_list_items_query(
            user_id=user_id,
            thread_id=thread_id,
            status=status,
            scopes=scopes,
            limit=limit,
        )
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        items = [row_to_memory_item(row) for row in rows]
        if status in {"active", "pending"}:
            now = utc_now_iso()
            return [item for item in items if item.expires_at is None or item.expires_at > now]
        return items

    def delete_expired(self) -> int:
        now = utc_now_iso()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM memory_items WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (now,),
            )
        return int(cur.rowcount)

    def count_active(self, *, user_id: str) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM memory_items WHERE user_id = ? AND is_deprecated = 0",
                (user_id,),
            ).fetchone()
        return int(row["c"]) if row is not None else 0

    def touch_access(self, item_ids: list[str]) -> None:
        if not item_ids:
            return
        now = utc_now_iso()
        placeholders = ",".join("?" for _ in item_ids)
        with self._lock, self._connect() as conn:
            conn.execute(
                f"""
                UPDATE memory_items
                SET access_count = access_count + 1,
                    last_accessed_at = ?,
                    updated_at = ?
                WHERE id IN ({placeholders})
                """,
                [now, now, *item_ids],
            )

    def evict_lowest_score(
        self,
        *,
        user_id: str,
        keep_count: int,
        protected_importance: float,
    ) -> list[str]:
        items = self.list_items(user_id=user_id, status="all", limit=10000)
        items = [item for item in items if not item.is_deprecated]
        if len(items) <= keep_count:
            return []
        scored = sorted(
            (
                (
                    memory_eviction_score(item),
                    item.updated_at,
                    item,
                )
                for item in items
                if item.importance < protected_importance
            ),
            key=lambda triple: (triple[0], triple[1]),
        )
        to_remove = len(items) - keep_count
        removed: list[str] = []
        for score, _updated, item in scored[:to_remove]:
            self.deprecate(
                item.id,
                history_entry={
                    "action": "evicted",
                    "at": utc_now_iso(),
                    "reason": "capacity_limit_v2",
                    "eviction_score": round(score, 4),
                },
            )
            removed.append(item.id)
        return removed

    def confirm_item(self, item_id: str) -> MemoryItemRecord | None:
        row = self.get(item_id)
        if row is None:
            return None
        updated = MemoryItemRecord(
            id=row.id,
            user_id=row.user_id,
            thread_id=row.thread_id,
            scope=row.scope,
            memory_type=row.memory_type,
            content=row.content,
            content_hash=row.content_hash,
            importance=row.importance,
            confidence=max(row.confidence, 0.85),
            version=row.version,
            supersedes_id=row.supersedes_id,
            is_deprecated=False,
            pending_confirmation=False,
            expires_at=row.expires_at,
            access_count=row.access_count,
            last_accessed_at=row.last_accessed_at,
            created_at=row.created_at,
            updated_at=utc_now_iso(),
            source_run_id=row.source_run_id,
            history=list(row.history),
            embedding=row.embedding,
        )
        return self.update(updated)

    def new_id(self) -> str:
        return f"mem_{uuid4().hex[:12]}"
_row_to_record = row_to_memory_item
