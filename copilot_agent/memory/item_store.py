from __future__ import annotations

import hashlib
import json
import sqlite3
from threading import Lock
from typing import Any
from uuid import uuid4

from copilot_agent.memory.item_schema import MemoryItemRecord, MemoryScope, MemoryType
from copilot_agent.runtime.event_store import utc_now_iso


def content_hash(content: str) -> str:
    normalized = " ".join((content or "").split()).strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


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
                    json.dumps(item.embedding) if item.embedding else None,
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
                    json.dumps(item.history, ensure_ascii=False),
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
                    json.dumps(item.embedding) if item.embedding else None,
                    item.version,
                    item.supersedes_id,
                    1 if item.is_deprecated else 0,
                    item.expires_at,
                    item.access_count,
                    item.last_accessed_at,
                    item.updated_at,
                    1 if item.pending_confirmation else 0,
                    json.dumps(item.history, ensure_ascii=False),
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

    def get(self, item_id: str) -> MemoryItemRecord | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM memory_items WHERE id = ?", (item_id,)).fetchone()
        return _row_to_record(row) if row is not None else None

    def list_active(
        self,
        *,
        user_id: str,
        thread_id: str | None = None,
        scopes: tuple[MemoryScope, ...] | None = None,
        include_pending: bool = False,
    ) -> list[MemoryItemRecord]:
        sql = "SELECT * FROM memory_items WHERE user_id = ? AND is_deprecated = 0"
        params: list[Any] = [user_id]
        if not include_pending:
            sql += " AND pending_confirmation = 0"
        if scopes:
            placeholders = ",".join("?" for _ in scopes)
            sql += f" AND scope IN ({placeholders})"
            params.extend(scope.value for scope in scopes)
        if thread_id is not None:
            sql += " AND (scope = ? OR thread_id = ?)"
            params.extend([MemoryScope.USER.value, thread_id])
        sql += " ORDER BY updated_at DESC"
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        now = utc_now_iso()
        items = [_row_to_record(row) for row in rows]
        return [item for item in items if item.expires_at is None or item.expires_at > now]

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
        items = self.list_active(user_id=user_id)
        if len(items) <= keep_count:
            return []
        scored = sorted(
            (
                (
                    item.importance + (0.05 if item.last_accessed_at else 0.0),
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
        for _score, _updated, item in scored[:to_remove]:
            self.deprecate(
                item.id,
                history_entry={"action": "evicted", "at": utc_now_iso(), "reason": "capacity_limit"},
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


def _row_to_record(row: sqlite3.Row) -> MemoryItemRecord:
    embedding_raw = row["embedding_json"]
    embedding = json.loads(embedding_raw) if embedding_raw else None
    history_raw = row["history_json"] or "[]"
    history = json.loads(history_raw)
    return MemoryItemRecord(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        thread_id=str(row["thread_id"]) if row["thread_id"] else None,
        scope=MemoryScope(str(row["scope"])),
        memory_type=MemoryType(str(row["memory_type"])),
        content=str(row["content"]),
        content_hash=str(row["content_hash"]),
        importance=float(row["importance"]),
        confidence=float(row["confidence"]),
        version=int(row["version"]),
        supersedes_id=str(row["supersedes_id"]) if row["supersedes_id"] else None,
        is_deprecated=bool(row["is_deprecated"]),
        pending_confirmation=bool(row["pending_confirmation"]) if "pending_confirmation" in row.keys() else False,
        expires_at=str(row["expires_at"]) if row["expires_at"] else None,
        access_count=int(row["access_count"]),
        last_accessed_at=str(row["last_accessed_at"]) if row["last_accessed_at"] else None,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        source_run_id=str(row["source_run_id"]) if row["source_run_id"] else None,
        history=history if isinstance(history, list) else [],
        embedding=embedding if isinstance(embedding, list) else None,
    )
