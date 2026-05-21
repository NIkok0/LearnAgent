from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from copilot_agent.scenario.schema import ScenarioResourcesConfig

_active_policy: HttpPathPolicy | None = None


@dataclass(frozen=True)
class HttpPathPolicy:
    get_actuator_paths: tuple[str, ...] = ()
    get_path_patterns: tuple[re.Pattern[str], ...] = ()
    post_exact_paths: frozenset[str] = frozenset()

    @classmethod
    def from_resources(cls, resources: ScenarioResourcesConfig) -> HttpPathPolicy:
        actuator = tuple(
            path.split("?", 1)[0]
            for path in (resources.http_get_actuator_paths or [])
            if str(path).strip()
        )
        patterns: list[re.Pattern[str]] = []
        for raw in resources.http_get_patterns or []:
            text = str(raw).strip()
            if not text:
                continue
            patterns.append(re.compile(text))
        post_paths = frozenset(
            str(path).split("?", 1)[0]
            for path in (resources.http_post_paths or [])
            if str(path).strip()
        )
        return cls(
            get_actuator_paths=actuator,
            get_path_patterns=tuple(patterns),
            post_exact_paths=post_paths,
        )

    @classmethod
    def empty(cls) -> HttpPathPolicy:
        return cls()

    def validate_get(self, path: str) -> str | None:
        if not path.startswith("/"):
            return "path must start with /"
        if _dangerous_path(path):
            return "invalid path"
        base = path.split("?", 1)[0]
        if base in self.get_actuator_paths:
            return None
        if base.startswith("/actuator/") and base not in self.get_actuator_paths:
            allowed = ", ".join(self.get_actuator_paths) or "/actuator/health"
            return f"only configured actuator paths are allowed ({allowed})"
        if not base.startswith("/api/v1/") and not base.startswith("/actuator/"):
            return "GET only allows configured /api/v1/* or actuator paths"
        for pattern in self.get_path_patterns:
            if pattern.match(base):
                return None
        return "GET path not on scenario allowlist"

    def validate_post(self, path: str) -> str | None:
        if not path.startswith("/"):
            return "path must start with /"
        if _dangerous_path(path):
            return "invalid path"
        base = path.split("?", 1)[0]
        if base in self.post_exact_paths:
            return None
        return "POST path not on scenario allowlist"


def bind_http_path_policy(policy: HttpPathPolicy) -> None:
    global _active_policy
    _active_policy = policy


def get_http_path_policy() -> HttpPathPolicy:
    return _active_policy or HttpPathPolicy.empty()


def _dangerous_path(path: str) -> bool:
    if ".." in path or path.startswith("//"):
        return True
    return bool(urlparse(path).scheme)
