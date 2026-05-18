from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class _Entry:
    cookie_header: str
    expires_at: float


class ConversationCookieStore:
    """In-memory WMSESSIONID (full Cookie header value) per conversation; never log raw values."""

    def __init__(self, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._data: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def set_cookie(self, conversation_id: str, cookie_header: str) -> None:
        with self._lock:
            self._data[conversation_id] = _Entry(
                cookie_header=cookie_header,
                expires_at=time.monotonic() + self._ttl,
            )

    def get_cookie(self, conversation_id: str) -> str | None:
        now = time.monotonic()
        with self._lock:
            self._purge_locked(now)
            e = self._data.get(conversation_id)
            if e is None:
                return None
            if e.expires_at < now:
                del self._data[conversation_id]
                return None
            return e.cookie_header

    def _purge_locked(self, now: float) -> None:
        dead = [k for k, v in self._data.items() if v.expires_at < now]
        for k in dead:
            del self._data[k]


def redact_cookie_header(value: str | None) -> str:
    if not value:
        return ""
    lower = value.lower()
    if "wmsessionid=" in lower:
        return "Cookie:<WMSESSIONID=***REDACTED***>"
    return "Cookie:<REDACTED>"
