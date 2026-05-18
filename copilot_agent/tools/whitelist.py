from __future__ import annotations

import re
from urllib.parse import urlparse

GET_ALLOWED_ACTUATOR = ("/actuator/health",)

GET_API_PATTERNS = [
    re.compile(r"^/api/v1/stats/dashboard/?$"),
    re.compile(
        r"^/api/v1/jobs/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/?$"
    ),
    re.compile(r"^/api/v1/files/?$"),
    re.compile(r"^/api/v1/files/\d+/?$"),
    re.compile(r"^/api/v1/admin/users/?$"),
    re.compile(r"^/api/v1/admin/users/\d+/?$"),
    re.compile(r"^/api/v1/admin/groups/?$"),
    re.compile(r"^/api/v1/admin/stats/?$"),
]

POST_EXACT = frozenset(
    {
        "/api/v1/auth/login",
        "/api/v1/jobs/watermark",
    }
)


def _dangerous_path(path: str) -> bool:
    if ".." in path or path.startswith("//"):
        return True
    if urlparse(path).scheme:
        return True
    return False


def validate_get_path(path: str) -> str | None:
    """Return error message or None if OK."""
    if not path.startswith("/"):
        return "path must start with /"
    if _dangerous_path(path):
        return "invalid path"
    base = path.split("?", 1)[0]
    if base in GET_ALLOWED_ACTUATOR:
        return None
    if base.startswith("/actuator/") and base != "/actuator/health":
        return "only /actuator/health is allowed under actuator"
    if not base.startswith("/api/v1/"):
        return "GET only allows /api/v1/* or /actuator/health"
    for pat in GET_API_PATTERNS:
        if pat.match(base):
            return None
    return "GET path not on whitelist"


def validate_post_path(path: str) -> str | None:
    if not path.startswith("/"):
        return "path must start with /"
    if _dangerous_path(path):
        return "invalid path"
    base = path.split("?", 1)[0]
    if base not in POST_EXACT:
        return "POST path not on whitelist"
    return None
