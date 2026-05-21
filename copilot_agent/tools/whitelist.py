from __future__ import annotations

from copilot_agent.scenario.http_paths import get_http_path_policy


def validate_get_path(path: str) -> str | None:
    """Return error message or None if OK (uses active Scenario HTTP path policy)."""
    return get_http_path_policy().validate_get(path)


def validate_post_path(path: str) -> str | None:
    return get_http_path_policy().validate_post(path)
