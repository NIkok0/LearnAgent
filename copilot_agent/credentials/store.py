from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class _SecretEntry:
    value: str
    expires_at: float
    user_id: str = ""


class InMemoryCredentialStore:
    """Process-local secret store keyed by thread_id (M14 MVP storage backend)."""

    def __init__(self, *, ttl_seconds: int) -> None:
        self._ttl = max(1, int(ttl_seconds))
        self._data: dict[str, _SecretEntry] = {}
        self._lock = threading.Lock()

    def set(self, thread_id: str, *, user_id: str, secret: str) -> None:
        with self._lock:
            self._data[thread_id] = _SecretEntry(
                value=secret,
                expires_at=time.monotonic() + self._ttl,
                user_id=user_id,
            )

    def get(self, thread_id: str) -> str | None:
        now = time.monotonic()
        with self._lock:
            self._purge_locked(now)
            entry = self._data.get(thread_id)
            if entry is None:
                return None
            if entry.expires_at < now:
                del self._data[thread_id]
                return None
            return entry.value

    def _purge_locked(self, now: float) -> None:
        dead = [key for key, entry in self._data.items() if entry.expires_at < now]
        for key in dead:
            del self._data[key]
