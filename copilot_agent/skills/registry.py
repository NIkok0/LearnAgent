from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml

from copilot_agent.skills.schema import SkillSpec

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SkillRegistry:
    skills: dict[str, SkillSpec]
    warnings: tuple[str, ...] = ()

    def enabled(self, names: Iterable[str]) -> list[SkillSpec]:
        return [self.skills[name] for name in names if name in self.skills]

    def public_specs(self, names: Iterable[str] | None = None) -> list[dict[str, object]]:
        specs = self.skills.values() if names is None else self.enabled(names)
        return [spec.public_dict() for spec in specs]


def load_skill_registry(*, repo_root: Path, enabled_names: Iterable[str] = ()) -> SkillRegistry:
    specs, warnings = load_skill_specs(repo_root=repo_root)
    missing = [name for name in enabled_names if name not in specs]
    all_warnings = [*warnings, *[f"skill_missing:{name}" for name in missing]]
    for warning in all_warnings:
        log.warning("skill warning: %s", warning)
    return SkillRegistry(skills=specs, warnings=tuple(all_warnings))


def load_skill_specs(*, repo_root: Path) -> tuple[dict[str, SkillSpec], list[str]]:
    root = repo_root / "skills"
    warnings: list[str] = []
    if not root.is_dir():
        return {}, [f"skills_dir_missing:{root}"]
    specs: dict[str, SkillSpec] = {}
    for manifest in sorted(root.glob("*/skill.yaml")):
        try:
            raw = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict):
                warnings.append(f"skill_manifest_invalid:{manifest.parent.name}")
                continue
            spec = SkillSpec.model_validate(raw)
            if spec.name != manifest.parent.name:
                warnings.append(f"skill_name_mismatch:{manifest.parent.name}:{spec.name}")
            specs[spec.name] = spec
        except Exception as exc:
            warnings.append(f"skill_load_failed:{manifest.parent.name}:{type(exc).__name__}")
    return specs, warnings
