from __future__ import annotations

_LAZY_EXPORTS = {
    "LoadedScenario": ("copilot_agent.scenario.loader", "LoadedScenario"),
    "ScenarioConfig": ("copilot_agent.scenario.schema", "ScenarioConfig"),
    "ScenarioPolicyConfig": ("copilot_agent.scenario.schema", "ScenarioPolicyConfig"),
    "apply_scenario_environment": ("copilot_agent.scenario.bootstrap", "apply_scenario_environment"),
    "config_root": ("copilot_agent.scenario.loader", "config_root"),
    "load_scenario": ("copilot_agent.scenario.loader", "load_scenario"),
    "repo_root": ("copilot_agent.scenario.loader", "repo_root"),
    "scenario_status": ("copilot_agent.scenario.loader", "scenario_status"),
    "scenarios_root": ("copilot_agent.scenario.loader", "scenarios_root"),
}

__all__ = list(_LAZY_EXPORTS.keys())


def __getattr__(name: str):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr = target
    import importlib

    module = importlib.import_module(module_name)
    value = getattr(module, attr)
    globals()[name] = value
    return value
