from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Callable
from uuid import uuid4


THREAD_STATUS_ACTIVE = "active"
THREAD_STATUS_ENDED = "ended"
THREAD_STATUS_ARCHIVED = "archived"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_QUEUED = "queued"
RUN_STATUS_WAITING_APPROVAL = "waiting_approval"
RUN_STATUS_CANCELLING = "cancelling"
RUN_STATUS_CANCELLED = "cancelled"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_FAILED = "failed"

TERMINAL_RUN_STATUSES = {RUN_STATUS_CANCELLED, RUN_STATUS_COMPLETED, RUN_STATUS_FAILED}
NON_TERMINAL_RUN_STATUSES = {
    RUN_STATUS_QUEUED,
    RUN_STATUS_RUNNING,
    RUN_STATUS_WAITING_APPROVAL,
    RUN_STATUS_CANCELLING,
}


class ThreadNotActiveError(RuntimeError):
    def __init__(self, thread_id: str, status: str | None) -> None:
        self.thread_id = thread_id
        self.status = status
        super().__init__("thread is not active")


class ActiveRunExistsError(RuntimeError):
    def __init__(self, thread_id: str, run_id: str) -> None:
        self.thread_id = thread_id
        self.run_id = run_id
        super().__init__("thread already has an active run")


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def utc_iso_before(seconds: int | float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=max(0, seconds))).isoformat()


class EventStore:
    """Small SQLite-backed thread/run/event store."""

    def __init__(self, path: str) -> None:
        self.path = str(Path(path).expanduser())
        db_path = Path(self.path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._subscribers: list[Callable[[dict[str, Any]], None]] = []
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

    def end_thread(self, thread_id: str) -> dict[str, Any] | None:
        now = utc_now_iso()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE threads
                SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                (THREAD_STATUS_ENDED, now, thread_id),
            )
            row = conn.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
        return _row_to_dict(row) if row is not None else None

    def archive_thread(self, thread_id: str) -> dict[str, Any] | None:
        now = utc_now_iso()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE threads
                SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                (THREAD_STATUS_ARCHIVED, now, thread_id),
            )
            row = conn.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
        return _row_to_dict(row) if row is not None else None

    def list_ended_threads_before(self, cutoff_iso: str, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM threads
                WHERE status = ?
                  AND updated_at <= ?
                ORDER BY updated_at ASC
                LIMIT ?
                """,
                (THREAD_STATUS_ENDED, cutoff_iso, limit),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def archive_ended_threads_before(self, cutoff_iso: str, *, limit: int = 100) -> list[dict[str, Any]]:
        now = utc_now_iso()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM threads
                WHERE status = ?
                  AND updated_at <= ?
                ORDER BY updated_at ASC
                LIMIT ?
                """,
                (THREAD_STATUS_ENDED, cutoff_iso, limit),
            ).fetchall()
            thread_ids = [str(row["id"]) for row in rows]
            if thread_ids:
                placeholders = ",".join("?" for _ in thread_ids)
                conn.execute(
                    f"""
                    UPDATE threads
                    SET status = ?, updated_at = ?
                    WHERE id IN ({placeholders})
                    """,
                    [THREAD_STATUS_ARCHIVED, now, *thread_ids],
                )
                rows = conn.execute(
                    f"SELECT * FROM threads WHERE id IN ({placeholders}) ORDER BY updated_at ASC",
                    thread_ids,
                ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def archive_ended_threads_older_than(self, ttl_seconds: int | float, *, limit: int = 100) -> list[dict[str, Any]]:
        return self.archive_ended_threads_before(utc_iso_before(ttl_seconds), limit=limit)

    def create_run(self, thread_id: str, *, run_id: str | None = None, status: str = RUN_STATUS_QUEUED) -> dict[str, Any]:
        run_id = run_id or str(uuid4())
        now = utc_now_iso()
        with self._lock, self._connect() as conn:
            thread = conn.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
            if thread is None:
                conn.execute(
                    """
                    INSERT INTO threads (id, title, status, created_at, updated_at)
                    VALUES (?, NULL, ?, ?, ?)
                    """,
                    (thread_id, THREAD_STATUS_ACTIVE, now, now),
                )
                thread_status = THREAD_STATUS_ACTIVE
            else:
                thread_status = str(thread["status"])
            if thread_status != THREAD_STATUS_ACTIVE:
                raise ThreadNotActiveError(thread_id, thread_status)

            active_run = conn.execute(
                """
                SELECT id FROM runs
                WHERE thread_id = ?
                  AND status IN (?, ?, ?, ?)
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (
                    thread_id,
                    RUN_STATUS_QUEUED,
                    RUN_STATUS_RUNNING,
                    RUN_STATUS_WAITING_APPROVAL,
                    RUN_STATUS_CANCELLING,
                ),
            ).fetchone()
            if active_run is not None:
                raise ActiveRunExistsError(thread_id, str(active_run["id"]))

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
        event = _event_row_to_dict(row)
        self._notify(event)
        return event

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

    def subscribe(self, callback: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def _notify(self, event: dict[str, Any]) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(event)
            except Exception:
                continue


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
