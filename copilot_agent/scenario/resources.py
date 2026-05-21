from __future__ import annotations

import os

from copilot_agent.scenario.schema import ScenarioResourcesConfig


def resolve_api_base_url(resources: ScenarioResourcesConfig) -> str:
    if resources.api_base_url and resources.api_base_url.strip():
        return resources.api_base_url.strip().rstrip("/")
    env_name = (resources.api_base_url_env or "API_BASE_URL").strip() or "API_BASE_URL"
    raw = os.environ.get(env_name, "").strip()
    if raw:
        return raw.rstrip("/")
    default = (resources.default_api_base_url or "").strip()
    if default:
        return default.rstrip("/")
    return ""


def resolve_docs_path_env_name(resources: ScenarioResourcesConfig) -> str:
    return (resources.docs_path_env or "COPILOT_DOCS_PATH").strip() or "COPILOT_DOCS_PATH"
