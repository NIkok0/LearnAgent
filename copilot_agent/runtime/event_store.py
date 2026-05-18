from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4


THREAD_STATUS_ACTIVE = "active"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_QUEUED = "queued"
RUN_STATUS_WAITING_APPROVAL = "waiting_approval"
RUN_STATUS_CANCELLING = "cancelling"
RUN_STATUS_CANCELLED = "cancelled"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_FAILED = "failed"

TERMINAL_RUN_STATUSES = {RUN_STATUS_CANCELLED, RUN_STATUS_COMPLETED, RUN_STATUS_FAILED}


def utc_now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


class EventStore:
    """Small SQLite-backed thread/run/event store."""

    def __init__(self, path: str) -> None:
        self.path = str(Path(path).expanduser())
        db_path = Path(self.path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS threads (
                    id TEXT PRIMARY KEY,
                    title TEXT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT NULL,
                    error TEXT NULL,
                    FOREIGN KEY(thread_id) REFERENCES threads(id)
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(thread_id) REFERENCES threads(id),
                    FOREIGN KEY(run_id) REFERENCES runs(id)
                );

                CREATE INDEX IF NOT EXISTS idx_runs_thread_id_created_at
                    ON runs(thread_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_events_thread_run_id
                    ON events(thread_id, run_id, id);
                """
            )

    def ensure_thread(self, thread_id: str, *, title: str | None = None) -> dict[str, Any]:
        now = utc_now_iso()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO threads (id, title, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = COALESCE(excluded.title, threads.title),
                    updated_at = excluded.updated_at
                """,
                (thread_id, title, THREAD_STATUS_ACTIVE, now, now),
            )
            row = conn.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
        return _row_to_dict(row)

    def create_run(self, thread_id: str, *, run_id: str | None = None, status: str = RUN_STATUS_QUEUED) -> dict[str, Any]:
        self.ensure_thread(thread_id)
        run_id = run_id or str(uuid4())
        now = utc_now_iso()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (id, thread_id, status, created_at, completed_at, error)
                VALUES (?, ?, ?, ?, NULL, NULL)
                """,
                (run_id, thread_id, status, now),
            )
            conn.execute("UPDATE threads SET updated_at = ? WHERE id = ?", (now, thread_id))
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return _row_to_dict(row)

    def append_event(self, thread_id: str, run_id: str, event_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        now = utc_now_iso()
        payload_json = json.dumps(payload or {}, ensure_ascii=False)
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO events (thread_id, run_id, type, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (thread_id, run_id, event_type, payload_json, now),
            )
            conn.execute("UPDATE threads SET updated_at = ? WHERE id = ?", (now, thread_id))
            row = conn.execute("SELECT * FROM events WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _event_row_to_dict(row)

    def update_run_status(self, run_id: str, status: str, *, error: str | None = None, completed: bool = False) -> dict[str, Any]:
        now = utc_now_iso()
        completed_at = now if completed or status in TERMINAL_RUN_STATUSES else None
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET status = ?, completed_at = ?, error = ?
                WHERE id = ?
                """,
                (status, completed_at, error, run_id),
            )
            run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run is not None:
                conn.execute("UPDATE threads SET updated_at = ? WHERE id = ?", (now, run["thread_id"]))
        return _row_to_dict(run)

    def complete_run(self, run_id: str, *, error: str | None = None) -> dict[str, Any]:
        status = RUN_STATUS_FAILED if error else RUN_STATUS_COMPLETED
        return self.update_run_status(run_id, status, error=error, completed=True)

    def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
        return _row_to_dict(row) if row is not None else None

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return _row_to_dict(row) if row is not None else None

    def list_runs(self, thread_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE thread_id = ? ORDER BY created_at ASC",
                (thread_id,),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_events(self, thread_id: str, *, run_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events WHERE thread_id = ?"
        params: list[Any] = [thread_id]
        if run_id:
            sql += " AND run_id = ?"
            params.append(run_id)
        sql += " ORDER BY id ASC"
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_event_row_to_dict(row) for row in rows]

    def list_run_events(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM events WHERE run_id = ? ORDER BY id ASC", (run_id,)).fetchall()
        return [_event_row_to_dict(row) for row in rows]


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    return dict(row)


def _event_row_to_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    data = _row_to_dict(row)
    if not data:
        return data
    payload_json = data.pop("payload_json", "{}")
    data["payload"] = json.loads(payload_json)
    return data
