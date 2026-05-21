from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RagRulesOverlay:
    rewrite_rules: tuple[tuple[str, str], ...] = ()
    doc_type_hints: tuple[tuple[str, str, float], ...] = ()


@dataclass(frozen=True)
class DiagnosisTemplate:
    doc_causes: tuple[str, ...]
    doc_sources: tuple[str, ...]
    checklist: tuple[str, ...]
