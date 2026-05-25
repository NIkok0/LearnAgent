from __future__ import annotations

from typing import Any

from copilot_agent.memory.item_schema import MemoryScope


def build_list_active_query(
    *,
    user_id: str,
    thread_id: str | None = None,
    scopes: tuple[MemoryScope, ...] | None = None,
    include_pending: bool = False,
) -> tuple[str, list[Any]]:
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
    return sql, params


def build_list_items_query(
    *,
    user_id: str,
    thread_id: str | None = None,
    status: str = "active",
    scopes: tuple[MemoryScope, ...] | None = None,
    limit: int = 100,
) -> tuple[str, list[Any]]:
    sql = "SELECT * FROM memory_items WHERE user_id = ?"
    params: list[Any] = [user_id]
    if status == "active":
        sql += " AND is_deprecated = 0 AND pending_confirmation = 0"
    elif status == "pending":
        sql += " AND is_deprecated = 0 AND pending_confirmation = 1"
    elif status == "deprecated":
        sql += " AND is_deprecated = 1"
    elif status == "all":
        pass
    else:
        raise ValueError(f"unsupported memory item status: {status}")
    if scopes:
        placeholders = ",".join("?" for _ in scopes)
        sql += f" AND scope IN ({placeholders})"
        params.extend(scope.value for scope in scopes)
    if thread_id is not None:
        sql += " AND (scope = ? OR thread_id = ?)"
        params.extend([MemoryScope.USER.value, thread_id])
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(max(1, int(limit)))
    return sql, params


__all__ = ["build_list_active_query", "build_list_items_query"]
