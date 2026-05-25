from __future__ import annotations

import json
import sqlite3

from copilot_agent.memory.item_schema import MemoryItemRecord, MemoryScope, MemoryType


def row_to_memory_item(row: sqlite3.Row) -> MemoryItemRecord:
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


def encode_embedding(item: MemoryItemRecord) -> str | None:
    return json.dumps(item.embedding) if item.embedding else None


def encode_history(item: MemoryItemRecord) -> str:
    return json.dumps(item.history, ensure_ascii=False)


__all__ = ["encode_embedding", "encode_history", "row_to_memory_item"]
