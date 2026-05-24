from __future__ import annotations

import os
import logging

from copilot_agent.scenario.http_paths import bind_http_path_policy
from copilot_agent.scenario.loader import LoadedScenario, repo_root
from copilot_agent.scenario.resources import resolve_docs_path_env_name

log = logging.getLogger(__name__)


def apply_scenario_environment(scenario: LoadedScenario) -> str:
    """Apply scenario resource bindings and overlays to process/runtime state."""
    from copilot_agent.agent.diagnosis import configure_diagnosis_templates
    from copilot_agent.rag.query_rewrite import configure_rag_rules
    from copilot_agent.scenario.runtime_bindings import configure_session_cookie

    docs = scenario.docs_dir(repo_root=repo_root())
    docs_str = str(docs)
    env_name = resolve_docs_path_env_name(scenario.resources)
    if not os.environ.get(env_name, "").strip():
        os.environ[env_name] = docs_str
        log.info("%s set from scenario %s -> %s", env_name, scenario.name, docs_str)
    configure_rag_rules(scenario.rag_rules)
    configure_diagnosis_templates(scenario.diagnosis_templates)
    configure_session_cookie(scenario.resources.credential_cookie_name)
    bind_http_path_policy(scenario.http_path_policy)
    _apply_rag_runtime_settings(scenario)
    _apply_mcp_process_env(scenario)
    return docs_str


def _apply_rag_runtime_settings(scenario: LoadedScenario) -> None:
    from copilot_agent.settings import settings

    model = (scenario.resources.rag_embedding_model or "").strip()
    if model:
        settings.rag_embedding_model = model
        os.environ["RAG_EMBEDDING_MODEL"] = model
        log.info("RAG_EMBEDDING_MODEL set from scenario %s -> %s", scenario.name, model)


def _apply_mcp_process_env(scenario: LoadedScenario) -> None:
    from copilot_agent.scenario.resources import resolve_api_base_url

    api_base = resolve_api_base_url(scenario.resources)
    if api_base:
        os.environ.setdefault("API_BASE_URL", api_base)
        env_name = (scenario.resources.api_base_url_env or "API_BASE_URL").strip()
        if env_name:
            os.environ.setdefault(env_name, api_base)
