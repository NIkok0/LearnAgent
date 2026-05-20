from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)


class CheckpointStore:
    """Purge LangGraph SQLite checkpoints for archived threads."""

    def __init__(self, path: str) -> None:
        self.path = str(Path(path).expanduser())

    def purge_thread(self, thread_id: str) -> int:
        db_path = Path(self.path)
        if not db_path.is_file():
            return 0
        deleted = 0
        with sqlite3.connect(self.path) as conn:
            tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            if "checkpoints" in tables:
                cur = conn.execute("DELETE FROM checkpoints WHERE thread_id = ?", (thread_id,))
                deleted += int(cur.rowcount or 0)
            if "writes" in tables:
                cur = conn.execute("DELETE FROM writes WHERE thread_id = ?", (thread_id,))
                deleted += int(cur.rowcount or 0)
        if deleted:
            log.info("Purged %d checkpoint row(s) for thread %s", deleted, thread_id[:8])
        return deleted

    def has_thread(self, thread_id: str) -> bool:
        db_path = Path(self.path)
        if not db_path.is_file():
            return False
        with sqlite3.connect(self.path) as conn:
            tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            if "checkpoints" not in tables:
                return False
            row = conn.execute(
                "SELECT 1 FROM checkpoints WHERE thread_id = ? LIMIT 1",
                (thread_id,),
            ).fetchone()
        return row is not None
