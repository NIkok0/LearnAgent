from __future__ import annotations

SESSION_COOKIE_NAME: str = ""


def configure_session_cookie(name: str) -> None:
    global SESSION_COOKIE_NAME
    SESSION_COOKIE_NAME = (name or "").strip()
