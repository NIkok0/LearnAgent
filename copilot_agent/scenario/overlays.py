"""Load scenario overlay YAML (RAG rewrite, diagnosis templates)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from copilot_agent.scenario.overlay_types import DiagnosisTemplate, RagRulesOverlay
from copilot_agent.scenario.paths import resolve_config_path


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


def load_rag_rules(path: Path) -> RagRulesOverlay:
    raw = _read_yaml(path)
    rewrite: list[tuple[str, str]] = []
    for item in raw.get("rewrite_rules") or []:
        if isinstance(item, dict) and item.get("pattern") and item.get("expansion"):
            rewrite.append((str(item["pattern"]), str(item["expansion"])))
    hints: list[tuple[str, str, float]] = []
    for item in raw.get("doc_type_hints") or []:
        if isinstance(item, dict) and item.get("pattern") and item.get("doc_type"):
            hints.append((str(item["pattern"]), str(item["doc_type"]), float(item.get("boost", 1.0))))
    return RagRulesOverlay(
        rewrite_rules=tuple(rewrite),
        doc_type_hints=tuple(hints),
    )


def load_diagnosis_templates(path: Path) -> dict[str, DiagnosisTemplate]:
    raw = _read_yaml(path)
    templates_raw = raw.get("status_templates") or {}
    if not isinstance(templates_raw, dict):
        return {}
    out: dict[str, DiagnosisTemplate] = {}
    for status, body in templates_raw.items():
        if not isinstance(body, dict):
            continue
        out[str(status).upper()] = DiagnosisTemplate(
            doc_causes=tuple(str(x) for x in body.get("doc_causes") or []),
            doc_sources=tuple(str(x) for x in body.get("doc_sources") or []),
            checklist=tuple(str(x) for x in body.get("checklist") or []),
        )
    return out


def load_rag_rules_ref(ref: str, *, base: Path) -> RagRulesOverlay | None:
    path = resolve_config_path(ref, base=base)
    if not path.is_file():
        return None
    return load_rag_rules(path)


def load_diagnosis_ref(ref: str, *, base: Path) -> dict[str, DiagnosisTemplate] | None:
    path = resolve_config_path(ref, base=base)
    if not path.is_file():
        return None
    templates = load_diagnosis_templates(path)
    return templates or None
