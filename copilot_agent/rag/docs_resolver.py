from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from copilot_agent.rag.docs_manifest import MANIFEST_FILENAME


@dataclass(frozen=True)
class ResolvedDocsSource:
    docs_dir: Path | None
    manifest_path: Path | None
    scenario_name: str
    source_kind: str
    override_env: str

    @property
    def available(self) -> bool:
        return self.docs_dir is not None and self.docs_dir.is_dir()

    def as_dict(self) -> dict[str, str | bool | None]:
        return {
            "docs_dir": str(self.docs_dir) if self.docs_dir is not None else None,
            "manifest_path": str(self.manifest_path) if self.manifest_path is not None else None,
            "scenario_name": self.scenario_name,
            "source_kind": self.source_kind,
            "override_env": self.override_env,
            "available": self.available,
        }


def resolve_docs_source(*, scenario_name: str | None = None) -> ResolvedDocsSource:
    """Resolve RAG docs as an explicit Scenario-bound source.

    Order:
    1. Scenario-specific docs env, then COPILOT_DOCS_PATH.
    2. Active Scenario docs_dir/resources.docs_fallback.
    3. Legacy backend-java/docs fallback.
    """
    scenario = None
    env_names = ["COPILOT_DOCS_PATH"]
    resolved_name = scenario_name or ""
    try:
        from copilot_agent.scenario import load_scenario
        from copilot_agent.scenario.loader import repo_root
        from copilot_agent.scenario.resources import resolve_docs_path_env_name

        scenario = load_scenario(scenario_name)
        resolved_name = scenario.name
        scenario_env = resolve_docs_path_env_name(scenario.resources)
        if scenario_env and scenario_env not in env_names:
            env_names.insert(0, scenario_env)
    except Exception:
        repo_root = _repo_root_fallback

    for env_name in env_names:
        raw = os.environ.get(env_name, "").strip()
        if not raw:
            continue
        candidate = Path(raw)
        if candidate.is_dir():
            return _resolved(candidate.resolve(), resolved_name, "env", env_name)

    if scenario is not None:
        docs = scenario.docs_dir(repo_root=repo_root())
        if docs.is_dir() and any(docs.glob("*.md")):
            return _resolved(docs.resolve(), resolved_name, "scenario", "")

    for base in _repo_root_fallback().parents:
        candidate = base / "backend-java" / "docs"
        if candidate.is_dir():
            return _resolved(candidate.resolve(), resolved_name, "backend_fallback", "")

    return ResolvedDocsSource(
        docs_dir=None,
        manifest_path=None,
        scenario_name=resolved_name,
        source_kind="missing",
        override_env="",
    )


def _resolved(docs_dir: Path, scenario_name: str, source_kind: str, override_env: str) -> ResolvedDocsSource:
    manifest = docs_dir / MANIFEST_FILENAME
    return ResolvedDocsSource(
        docs_dir=docs_dir,
        manifest_path=manifest if manifest.is_file() else None,
        scenario_name=scenario_name,
        source_kind=source_kind,
        override_env=override_env,
    )


def _repo_root_fallback() -> Path:
    here = Path(__file__).resolve()
    for base in here.parents:
        if (base / "copilot_agent").is_dir():
            return base
    return here.parents[2]
