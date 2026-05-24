from __future__ import annotations

import json
import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Callable
from uuid import uuid4

from copilot_agent.runtime.event_schema import envelope_payload
from copilot_agent.runtime.run_state import (
    NON_TERMINAL_RUN_STATUSES,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_CANCELLING,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_QUEUED,
    RUN_STATUS_RUNNING,
    RUN_STATUS_WAITING_APPROVAL,
    TERMINAL_RUN_STATUSES,
    validate_run_status,
    validate_run_transition,
)
from copilot_agent.settings import settings


THREAD_STATUS_ACTIVE = "active"
THREAD_STATUS_ENDED = "ended"
THREAD_STATUS_ARCHIVED = "archived"
THREAD_END_REASON_IDLE = "idle"
THREAD_END_REASON_EXPLICIT = "explicit"
THREAD_END_REASON_BROWSER_CLOSE = "browser_close"


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


class RunConcurrencyLimitError(RuntimeError):
    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(f"max concurrent runs ({limit}) reached")


class IdempotencyConflictError(RuntimeError):
    def __init__(self, thread_id: str, idempotency_key: str) -> None:
        self.thread_id = thread_id
        self.idempotency_key = idempotency_key
        super().__init__("idempotency key conflict")


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
                    updated_at TEXT NOT NULL,
                    last_interaction_at TEXT NOT NULL,
                    ended_at TEXT NULL,
                    archived_at TEXT NULL,
                    end_reason TEXT NULL
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
            self._migrate_threads(conn)
            self._migrate_runs(conn)
            self._migrate_events(conn)

    def _migrate_runs(self, conn: sqlite3.Connection) -> None:
        columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
        if "idempotency_key" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN idempotency_key TEXT NULL")
        if "idempotency_payload_hash" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN idempotency_payload_hash TEXT NULL")
        if "recovered_at" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN recovered_at TEXT NULL")
        if "recovery_reason" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN recovery_reason TEXT NULL")
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_runs_thread_id_idempotency
                ON runs(thread_id, idempotency_key)
            """
        )

    def _migrate_events(self, conn: sqlite3.Connection) -> None:
        columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(events)").fetchall()}
        if "sequence" not in columns:
            conn.execute("ALTER TABLE events ADD COLUMN sequence INTEGER")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_run_sequence ON events(run_id, sequence, id)"
        )
        rows = conn.execute(
            """
            SELECT id, run_id
            FROM events
            WHERE sequence IS NULL
            ORDER BY run_id ASC, id ASC
            """
        ).fetchall()
        if not rows:
            return
        next_seq: dict[str, int] = {}
        for row in rows:
            run_id = str(row["run_id"])
            seq = next_seq.get(run_id, 0) + 1
            next_seq[run_id] = seq
            conn.execute("UPDATE events SET sequence = ? WHERE id = ?", (seq, int(row["id"])))

    def _migrate_threads(self, conn: sqlite3.Connection) -> None:
        columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(threads)").fetchall()}
        if "last_interaction_at" not in columns:
            conn.execute("ALTER TABLE threads ADD COLUMN last_interaction_at TEXT")
            conn.execute("UPDATE threads SET last_interaction_at = updated_at")
        if "ended_at" not in columns:
            conn.execute("ALTER TABLE threads ADD COLUMN ended_at TEXT NULL")
        if "archived_at" not in columns:
            conn.execute("ALTER TABLE threads ADD COLUMN archived_at TEXT NULL")
        if "end_reason" not in columns:
            conn.execute("ALTER TABLE threads ADD COLUMN end_reason TEXT NULL")
        conn.execute("UPDATE threads SET last_interaction_at = updated_at WHERE last_interaction_at IS NULL")
        conn.execute(
            """
            UPDATE threads
            SET ended_at = COALESCE(ended_at, updated_at)
            WHERE status = ? AND ended_at IS NULL
            """,
            (THREAD_STATUS_ENDED,),
        )
        conn.execute(
            """
            UPDATE threads
            SET archived_at = COALESCE(archived_at, updated_at)
            WHERE status = ? AND archived_at IS NULL
            """,
            (THREAD_STATUS_ARCHIVED,),
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_threads_status_last_interaction ON threads(status, last_interaction_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_threads_status_ended_at ON threads(status, ended_at)")
        if "user_id" not in columns:
            conn.execute("ALTER TABLE threads ADD COLUMN user_id TEXT NULL")
            conn.execute("UPDATE threads SET user_id = id WHERE user_id IS NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_threads_user_id ON threads(user_id)")

    def ensure_thread(self, thread_id: str, *, title: str | None = None, user_id: str | None = None) -> dict[str, Any]:
        now = utc_now_iso()
        effective_user_id = user_id or thread_id
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO threads (
                    id, title, status, created_at, updated_at,
                    last_interaction_at, ended_at, archived_at, end_reason, user_id
                )
                VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = COALESCE(excluded.title, threads.title),
                    updated_at = excluded.updated_at,
                    user_id = COALESCE(threads.user_id, excluded.user_id),
                    last_interaction_at = CASE
                        WHEN threads.status = 'active' THEN excluded.last_interaction_at
                        ELSE threads.last_interaction_at
                    END
                """,
                (thread_id, title, THREAD_STATUS_ACTIVE, now, now, now, effective_user_id),
            )
            row = conn.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
        return _row_to_dict(row)

    def touch_thread(self, thread_id: str) -> dict[str, Any] | None:
        now = utc_now_iso()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE threads
                SET last_interaction_at = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (now, now, thread_id, THREAD_STATUS_ACTIVE),
            )
            row = conn.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
        return _row_to_dict(row) if row is not None else None

    def end_thread(self, thread_id: str, *, reason: str = THREAD_END_REASON_EXPLICIT) -> dict[str, Any] | None:
        now = utc_now_iso()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE threads
                SET status = ?, ended_at = COALESCE(ended_at, ?), end_reason = COALESCE(end_reason, ?), updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (THREAD_STATUS_ENDED, now, reason, now, thread_id, THREAD_STATUS_ACTIVE),
            )
            row = conn.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
        return _row_to_dict(row) if row is not None else None

    def archive_thread(self, thread_id: str) -> dict[str, Any] | None:
        now = utc_now_iso()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE threads
                SET status = ?,
                    archived_at = COALESCE(archived_at, ?),
                    updated_at = ?
                WHERE id = ? AND status != ?
                """,
                (THREAD_STATUS_ARCHIVED, now, now, thread_id, THREAD_STATUS_ARCHIVED),
            )
            row = conn.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
        return _row_to_dict(row) if row is not None else None

    def end_idle_threads_before(
        self,
        cutoff_iso: str,
        *,
        reason: str = THREAD_END_REASON_IDLE,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        now = utc_now_iso()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM threads
                WHERE status = ?
                  AND last_interaction_at <= ?
                ORDER BY last_interaction_at ASC
                LIMIT ?
                """,
                (THREAD_STATUS_ACTIVE, cutoff_iso, limit),
            ).fetchall()
            thread_ids = [str(row["id"]) for row in rows]
            if thread_ids:
                placeholders = ",".join("?" for _ in thread_ids)
                conn.execute(
                    f"""
                    UPDATE threads
                    SET status = ?, ended_at = ?, end_reason = ?, updated_at = ?
                    WHERE id IN ({placeholders})
                    """,
                    [THREAD_STATUS_ENDED, now, reason, now, *thread_ids],
                )
                rows = conn.execute(
                    f"SELECT * FROM threads WHERE id IN ({placeholders}) ORDER BY updated_at ASC",
                    thread_ids,
                ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_ended_threads_before(self, cutoff_iso: str, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM threads
                WHERE status = ?
                  AND ended_at <= ?
                ORDER BY ended_at ASC
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
                  AND ended_at <= ?
                ORDER BY ended_at ASC
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
                    SET status = ?, archived_at = ?, updated_at = ?
                    WHERE id IN ({placeholders})
                    """,
                    [THREAD_STATUS_ARCHIVED, now, now, *thread_ids],
                )
                rows = conn.execute(
                    f"SELECT * FROM threads WHERE id IN ({placeholders}) ORDER BY updated_at ASC",
                    thread_ids,
                ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def end_idle_threads_older_than(self, ttl_seconds: int | float, *, limit: int = 100) -> list[dict[str, Any]]:
        return self.end_idle_threads_before(utc_iso_before(ttl_seconds), limit=limit)

    def list_idle_active_threads_older_than(
        self,
        ttl_seconds: int | float,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        cutoff_iso = utc_iso_before(ttl_seconds)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM threads
                WHERE status = ?
                  AND last_interaction_at <= ?
                ORDER BY last_interaction_at ASC
                LIMIT ?
                """,
                (THREAD_STATUS_ACTIVE, cutoff_iso, limit),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def archive_ended_threads_older_than(self, ttl_seconds: int | float, *, limit: int = 100) -> list[dict[str, Any]]:
        return self.archive_ended_threads_before(utc_iso_before(ttl_seconds), limit=limit)

    def create_run(
        self,
        thread_id: str,
        *,
        run_id: str | None = None,
        status: str = RUN_STATUS_QUEUED,
        idempotency_key: str | None = None,
        idempotency_payload_hash: str | None = None,
    ) -> dict[str, Any]:
        validate_run_status(status)
        run_id = run_id or str(uuid4())
        idempotency_key = (idempotency_key or "").strip() or None
        idempotency_payload_hash = (idempotency_payload_hash or "").strip() or None
        now = utc_now_iso()
        with self._lock, self._connect() as conn:
            thread = conn.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
            if thread is None:
                conn.execute(
                    """
                    INSERT INTO threads (
                        id, title, status, created_at, updated_at,
                        last_interaction_at, ended_at, archived_at, end_reason, user_id
                    )
                    VALUES (?, NULL, ?, ?, ?, ?, NULL, NULL, NULL, ?)
                    """,
                    (thread_id, THREAD_STATUS_ACTIVE, now, now, now, thread_id),
                )
                thread_status = THREAD_STATUS_ACTIVE
            else:
                thread_status = str(thread["status"])
            if thread_status != THREAD_STATUS_ACTIVE:
                raise ThreadNotActiveError(thread_id, thread_status)

            if idempotency_key:
                existing = conn.execute(
                    """
                    SELECT * FROM runs
                    WHERE thread_id = ? AND idempotency_key = ?
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    (thread_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    existing_hash = str(existing["idempotency_payload_hash"] or "")
                    if existing_hash and idempotency_payload_hash and existing_hash != idempotency_payload_hash:
                        raise IdempotencyConflictError(thread_id, idempotency_key)
                    return _row_to_dict(existing)

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
                INSERT INTO runs (
                    id, thread_id, status, created_at, completed_at, error,
                    idempotency_key, idempotency_payload_hash, recovered_at, recovery_reason
                )
                VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, NULL, NULL)
                """,
                (run_id, thread_id, status, now, idempotency_key, idempotency_payload_hash),
            )
            conn.execute(
                "UPDATE threads SET last_interaction_at = ?, updated_at = ? WHERE id = ?",
                (now, now, thread_id),
            )
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return _row_to_dict(row)

    def idempotency_payload_hash(self, payload: dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    def append_event(self, thread_id: str, run_id: str, event_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if event_type == "tool_end":
            call_id = str((payload or {}).get("call_id") or "").strip()
            if call_id:
                existing = self.find_tool_end_event(run_id, call_id)
                if existing is not None:
                    return existing
        now = utc_now_iso()
        stored_payload = envelope_payload(event_type, payload)
        payload_json = json.dumps(stored_payload, ensure_ascii=False)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS max_seq FROM events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            sequence = int(row["max_seq"] or 0) + 1 if row is not None else 1
            cur = conn.execute(
                """
                INSERT INTO events (thread_id, run_id, type, payload_json, created_at, sequence)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (thread_id, run_id, event_type, payload_json, now, sequence),
            )
            conn.execute("UPDATE threads SET updated_at = ? WHERE id = ?", (now, thread_id))
            row = conn.execute("SELECT * FROM events WHERE id = ?", (cur.lastrowid,)).fetchone()
        event = _event_row_to_dict(row)
        self._notify(event)
        return event

    def find_tool_end_event(self, run_id: str, call_id: str) -> dict[str, Any] | None:
        if not call_id.strip():
            return None
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM events
                WHERE run_id = ? AND type = 'tool_end'
                ORDER BY sequence ASC, id ASC
                """,
                (run_id,),
            ).fetchall()
        for row in rows:
            event = _event_row_to_dict(row)
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            if str(payload.get("call_id") or "") == call_id:
                return event
        return None

    def find_successful_tool_end_by_idempotency(
        self,
        run_id: str,
        *,
        tool_name: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        key = idempotency_key.strip()
        if not key:
            return None
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM events
                WHERE run_id = ? AND type = 'tool_end'
                ORDER BY sequence ASC, id ASC
                """,
                (run_id,),
            ).fetchall()
        for row in rows:
            event = _event_row_to_dict(row)
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            if str(payload.get("name") or "") != tool_name:
                continue
            if str(payload.get("idempotency_key") or "") != key:
                continue
            if payload.get("success") is True:
                return event
        return None

    def update_run_status(self, run_id: str, status: str, *, error: str | None = None, completed: bool = False) -> dict[str, Any]:
        validate_run_status(status)
        now = utc_now_iso()
        with self._lock, self._connect() as conn:
            current = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if current is None:
                return {}
            current_status = str(current["status"])
            if current_status == status:
                return _row_to_dict(current)
            validate_run_transition(current_status, status)
            completed_at = now if completed or status in TERMINAL_RUN_STATUSES else None
            conn.execute(
                """
                UPDATE runs
                SET status = ?, completed_at = ?, error = ?
                WHERE id = ?
                """,
                (status, completed_at, error, run_id),
            )
            run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            conn.execute("UPDATE threads SET updated_at = ? WHERE id = ?", (now, run["thread_id"]))
        return _row_to_dict(run)

    def mark_run_recovered(self, run_id: str, *, reason: str) -> dict[str, Any]:
        now = utc_now_iso()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET recovered_at = COALESCE(recovered_at, ?),
                    recovery_reason = COALESCE(recovery_reason, ?)
                WHERE id = ?
                """,
                (now, reason, run_id),
            )
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return _row_to_dict(row)

    def complete_run(self, run_id: str, *, error: str | None = None) -> dict[str, Any]:
        status = RUN_STATUS_FAILED if error else RUN_STATUS_COMPLETED
        return self.update_run_status(run_id, status, error=error, completed=True)

    def list_runs_by_status(self, statuses: set[str] | list[str] | tuple[str, ...]) -> list[dict[str, Any]]:
        for status in statuses:
            validate_run_status(str(status))
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM runs WHERE status IN ({placeholders}) ORDER BY created_at ASC",
                [str(status) for status in statuses],
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def fail_non_terminal_runs(self, *, error: str, exclude_statuses: set[str] | None = None) -> list[dict[str, Any]]:
        exclude = {str(status) for status in (exclude_statuses or set())}
        statuses = sorted(status for status in NON_TERMINAL_RUN_STATUSES if status not in exclude)
        if not statuses:
            return []
        now = utc_now_iso()
        placeholders = ",".join("?" for _ in statuses)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM runs WHERE status IN ({placeholders}) ORDER BY created_at ASC",
                statuses,
            ).fetchall()
            run_ids = [str(row["id"]) for row in rows]
            if run_ids:
                run_placeholders = ",".join("?" for _ in run_ids)
                conn.execute(
                    f"""
                    UPDATE runs
                    SET status = ?, completed_at = ?, error = ?
                    WHERE id IN ({run_placeholders})
                    """,
                    [RUN_STATUS_FAILED, now, error, *run_ids],
                )
                conn.execute(
                    f"""
                    UPDATE threads
                    SET updated_at = ?
                    WHERE id IN (
                        SELECT DISTINCT thread_id FROM runs WHERE id IN ({run_placeholders})
                    )
                    """,
                    [now, *run_ids],
                )
                rows = conn.execute(
                    f"SELECT * FROM runs WHERE id IN ({run_placeholders}) ORDER BY created_at ASC",
                    run_ids,
                ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def get_user_id(self, thread_id: str) -> str:
        thread = self.get_thread(thread_id)
        if thread and thread.get("user_id"):
            return str(thread["user_id"])
        return thread_id

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

    def list_events(
        self,
        thread_id: str,
        *,
        run_id: str | None = None,
        after_id: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        return self._list_events_query(
            "thread_id = ?",
            [thread_id],
            run_id=run_id,
            after_id=after_id,
            limit=limit,
        )

    def list_run_events(
        self,
        run_id: str,
        *,
        after_id: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        return self._list_events_query(
            "run_id = ?",
            [run_id],
            run_id=None,
            after_id=after_id,
            limit=limit,
        )

    def latest_run_event_id(self, run_id: str) -> int | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM events WHERE run_id = ? ORDER BY sequence DESC, id DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return int(row["id"])

    def latest_run_sequence(self, run_id: str) -> int | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT sequence FROM events WHERE run_id = ? ORDER BY sequence DESC, id DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        if row is None or row["sequence"] is None:
            return None
        return int(row["sequence"])

    def list_events_page(
        self,
        thread_id: str,
        *,
        run_id: str | None = None,
        after_id: int | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        effective_limit = _resolve_event_limit(after_id, limit)
        fetch_limit = effective_limit + 1 if effective_limit is not None else None
        events = self.list_events(thread_id, run_id=run_id, after_id=after_id, limit=fetch_limit)
        return _page_from_events(events, effective_limit)

    def list_run_events_page(
        self,
        run_id: str,
        *,
        after_id: int | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        effective_limit = _resolve_event_limit(after_id, limit)
        fetch_limit = effective_limit + 1 if effective_limit is not None else None
        events = self.list_run_events(run_id, after_id=after_id, limit=fetch_limit)
        return _page_from_events(events, effective_limit)

    def _list_events_query(
        self,
        base_where: str,
        base_params: list[Any],
        *,
        run_id: str | None,
        after_id: int | None,
        limit: int | None,
    ) -> list[dict[str, Any]]:
        sql = f"SELECT * FROM events WHERE {base_where}"
        params: list[Any] = list(base_params)
        if run_id:
            sql += " AND run_id = ?"
            params.append(run_id)
        if after_id is not None:
            sql += " AND id > ?"
            params.append(int(after_id))
        sql += " ORDER BY sequence ASC, id ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_event_row_to_dict(row) for row in rows]

    def latest_run_id(self, thread_id: str) -> str | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM runs WHERE thread_id = ? ORDER BY created_at DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
        return str(row["id"]) if row is not None else None

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


def _resolve_event_limit(after_id: int | None, limit: int | None) -> int | None:
    if after_id is None and limit is None:
        return None
    if limit is None:
        return min(settings.event_page_default_limit, settings.event_page_max_limit)
    return min(max(1, int(limit)), settings.event_page_max_limit)


def _page_from_events(events: list[dict[str, Any]], limit: int | None) -> dict[str, Any]:
    if limit is None:
        return {
            "events": events,
            "next_after_id": int(events[-1]["id"]) if events else None,
            "has_more": False,
        }
    has_more = len(events) > limit
    page = events[:limit]
    return {
        "events": page,
        "next_after_id": int(page[-1]["id"]) if page else None,
        "has_more": has_more,
    }


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
