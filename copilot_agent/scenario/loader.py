"""Load Scenario business configuration (YAML/MD) — separated from Kernel, not a distributable pack."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from copilot_agent.scenario.paths import resolve_config_path
from copilot_agent.scenario.http_paths import HttpPathPolicy
from copilot_agent.scenario.overlays import load_diagnosis_ref, load_rag_rules_ref
from copilot_agent.scenario.schema import (
    ScenarioBudgets,
    ScenarioConfig,
    ScenarioPolicyConfig,
    ScenarioResourcesConfig,
)
from copilot_agent.memory.policy_config import (
    MemoryPolicyConfig,
    apply_memory_policy_overlay,
    memory_policy_from_settings,
)
from copilot_agent.scenario.router import RouterEngine, load_router_rules
from copilot_agent.scenario.router.schema import RouterRulesConfig
from copilot_agent.settings import settings
from copilot_agent.tools.extensions.mcp.registry import load_mcp_config
from copilot_agent.tools.extensions.mcp.schema import McpResourcesConfig

log = logging.getLogger(__name__)

DEFAULT_SCENARIO = "minimal"


@dataclass(frozen=True)
class LoadedScenario:
    config: ScenarioConfig
    root: Path
    config_path: Path
    policy: ScenarioPolicyConfig
    resources: ScenarioResourcesConfig
    system_prompt: str
    tool_grounded_prompt: str | None = None
    mcp: McpResourcesConfig | None = None
    router_rules: RouterRulesConfig | None = None
    memory_policy_overlay: dict[str, Any] | None = None
    rag_rules: RagRulesOverlay | None = None
    diagnosis_templates: dict[str, DiagnosisTemplate] | None = None

    @property
    def router_engine(self) -> RouterEngine:
        if self.router_rules is None:
            return RouterEngine(RouterRulesConfig())
        return RouterEngine(self.router_rules)

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def capabilities(self) -> tuple[str, ...]:
        return settings.enabled_capabilities()

    @property
    def budgets(self) -> ScenarioBudgets:
        return self.config.budgets

    def resolve_memory_policy(self) -> MemoryPolicyConfig:
        base = memory_policy_from_settings(settings)
        return apply_memory_policy_overlay(base, self.memory_policy_overlay)

    @property
    def http_path_policy(self) -> HttpPathPolicy:
        return HttpPathPolicy.from_resources(self.resources)

    def docs_dir(self, *, repo_root: Path | None = None) -> Path:
        base = repo_root if repo_root is not None else repo_root_fn()
        if self.config.docs_dir:
            candidate = resolve_config_path(self.config.docs_dir, base=base)
            if candidate.is_dir() and any(candidate.glob("*.md")):
                return candidate.resolve()
        fallback = (base / self.resources.docs_fallback).resolve()
        if fallback.is_dir():
            return fallback
        if self.config.docs_dir:
            return resolve_config_path(self.config.docs_dir, base=base)
        return (self.root / "docs").resolve()

    def eval_path(self, kind: str) -> Path | None:
        raw = getattr(self.config.eval, kind, None)
        if not raw:
            return None
        path = resolve_config_path(raw, base=repo_root())
        return path if path.is_file() else None


def repo_root_fn() -> Path:
    here = Path(__file__).resolve()
    for base in here.parents:
        if (base / "copilot_agent").is_dir() and (base / "config").is_dir():
            return base
        if (base / "copilot_agent").is_dir() and (base / "docs").is_dir():
            return base
    return here.parents[2]


repo_root = repo_root_fn


def config_root(explicit: str | None = None) -> Path:
    if explicit and explicit.strip():
        return Path(explicit.strip()).resolve()
    if settings.config_root.strip():
        return Path(settings.config_root.strip()).resolve()
    return repo_root() / "config"


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


def _read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _load_policy(raw: dict[str, Any], *, base: Path) -> ScenarioPolicyConfig:
    inline = raw.get("policy")
    if isinstance(inline, dict):
        return ScenarioPolicyConfig.model_validate(inline)
    policy_file = raw.get("policy_file") or raw.get("policy")
    if isinstance(policy_file, str) and policy_file.strip():
        path = resolve_config_path(policy_file, base=base)
        return ScenarioPolicyConfig.model_validate(_read_yaml(path))
    return ScenarioPolicyConfig()


def _load_resources(raw: dict[str, Any], *, base: Path) -> ScenarioResourcesConfig:
    inline = raw.get("resources")
    if isinstance(inline, dict):
        return ScenarioResourcesConfig.model_validate(inline)
    if isinstance(inline, str) and inline.strip():
        path = resolve_config_path(inline, base=base)
        return ScenarioResourcesConfig.model_validate(_read_yaml(path))
    return ScenarioResourcesConfig()


def _load_system_prompt(raw: dict[str, Any], config: ScenarioConfig, *, base: Path) -> str:
    if config.prompt and config.prompt.strip():
        return config.prompt.strip()
    if config.prompt_file:
        path = resolve_config_path(config.prompt_file, base=base)
        text = _read_text(path)
        if text:
            return text
    from copilot_agent.agent.prompts import DEFAULT_KERNEL_PROMPT

    log.warning("scenario %s missing prompt; using built-in fallback", config.name)
    return DEFAULT_KERNEL_PROMPT.strip()


def _load_mcp(raw: dict[str, Any], config: ScenarioConfig, *, base: Path) -> McpResourcesConfig | None:
    if "mcp" not in settings.enabled_capabilities():
        return None
    mcp_ref = config.mcp or raw.get("mcp")
    if not mcp_ref:
        return None
    path = resolve_config_path(str(mcp_ref), base=base)
    return load_mcp_config(path)


def _load_router(config: ScenarioConfig, *, base: Path) -> RouterRulesConfig | None:
    if not config.router:
        return None
    path = resolve_config_path(config.router, base=base)
    if not path.is_file():
        log.warning("router file missing: %s", path)
        return None
    return load_router_rules(path)


def _load_memory_policy(
    raw: dict[str, Any],
    config: ScenarioConfig,
    *,
    base: Path,
) -> dict[str, Any] | None:
    inline = raw.get("memory_policy")
    if isinstance(inline, dict):
        return inline
    ref: str | None = None
    if isinstance(inline, str) and inline.strip():
        ref = inline.strip()
    elif isinstance(config.memory_policy, str) and config.memory_policy.strip():
        ref = config.memory_policy.strip()
    if ref:
        path = resolve_config_path(ref, base=base)
        if not path.is_file():
            log.warning("memory policy file missing: %s", path)
            return None
        data = _read_yaml(path)
        return data if data else None
    return None


def _build_loaded(
    *,
    config: ScenarioConfig,
    raw: dict[str, Any],
    base: Path,
    config_path: Path,
) -> LoadedScenario:
    policy = _load_policy(raw, base=base)
    resources = _load_resources(raw, base=base)
    system_prompt = _load_system_prompt(raw, config, base=base)
    mcp_config = _load_mcp(raw, config, base=base)
    router_rules = _load_router(config, base=base)
    if router_rules is not None and policy.dangerous_paths:
        dangerous_path = str(policy.dangerous_paths[0]).strip()
        if dangerous_path:
            router_rules = router_rules.model_copy(update={"dangerous_job_path": dangerous_path})
    memory_overlay = _load_memory_policy(raw, config, base=base)
    rag_rules = None
    diagnosis_templates = None
    if resources.rag_rules:
        rag_rules = load_rag_rules_ref(resources.rag_rules, base=base)
    if resources.diagnosis:
        diagnosis_templates = load_diagnosis_ref(resources.diagnosis, base=base)
    return LoadedScenario(
        config=config,
        root=base.resolve(),
        config_path=config_path,
        policy=policy,
        resources=resources,
        system_prompt=system_prompt,
        tool_grounded_prompt=None,
        mcp=mcp_config,
        router_rules=router_rules,
        memory_policy_overlay=memory_overlay,
        rag_rules=rag_rules,
        diagnosis_templates=diagnosis_templates,
    )


def load_scenario(
    name: str | None = None,
    *,
    scenarios_root_path: str | None = None,
    config_root_path: str | None = None,
) -> LoadedScenario:
    if scenarios_root_path:
        log.warning("scenarios_root_path is ignored; Scenario configs are loaded from config_root only")
    scenario_name = (name or settings.scenario or DEFAULT_SCENARIO).strip() or DEFAULT_SCENARIO
    base = repo_root()
    flat_path = config_root(config_root_path) / f"{scenario_name}.yaml"
    if not flat_path.is_file():
        raise FileNotFoundError(f"scenario config not found: {flat_path}")

    raw = _read_yaml(flat_path)
    config = ScenarioConfig.model_validate(raw)
    if config.name != scenario_name:
        log.warning("config name=%s differs from requested %s", config.name, scenario_name)
    return _build_loaded(
        config=config,
        raw=raw,
        base=base,
        config_path=flat_path.resolve(),
    )


def scenario_status(scenario: LoadedScenario) -> dict[str, object]:
    docs = scenario.docs_dir(repo_root=repo_root())
    return {
        "name": scenario.name,
        "config_path": str(scenario.config_path),
        "root": str(scenario.root),
        "deployment_capabilities": list(scenario.capabilities),
        "docs_dir": str(docs),
        "budgets": scenario.budgets.model_dump(),
        "policy": {
            "tool_allowlist": scenario.policy.tool_allowlist,
            "tool_denylist": scenario.policy.tool_denylist,
            "mcp_server_allowlist": scenario.policy.mcp_server_allowlist,
            "mcp_tool_allowlist": scenario.policy.mcp_tool_allowlist,
        },
        "mcp": {
            "enabled": scenario.mcp is not None,
            "servers": [server.name for server in (scenario.mcp.enabled_servers() if scenario.mcp else [])],
        },
        "router": {
            "enabled": scenario.router_rules is not None,
            "rules": len(scenario.router_rules.rules) if scenario.router_rules else 0,
        },
        "memory_policy": {
            "overlay_keys": sorted((scenario.memory_policy_overlay or {}).keys()),
            "episodic_recall_top_k": scenario.resolve_memory_policy().episodic_recall_top_k,
            "long_term_recall_top_k": scenario.resolve_memory_policy().long_term_recall_top_k,
        },
        "resources": {
            "api_base_url_env": scenario.resources.api_base_url_env,
            "credential_binding": scenario.resources.credential_binding,
            "credential_cookie_name": scenario.resources.credential_cookie_name,
            "rag_rules_loaded": scenario.rag_rules is not None,
            "diagnosis_loaded": scenario.diagnosis_templates is not None,
        },
        "eval": {
            "golden": str(scenario.eval_path("golden") or ""),
            "rag_cases": str(scenario.eval_path("rag_cases") or ""),
        },
    }


def write_scenario_status_json(scenario: LoadedScenario, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(scenario_status(scenario), indent=2, ensure_ascii=False), encoding="utf-8")
