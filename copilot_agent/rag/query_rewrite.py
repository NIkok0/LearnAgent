from __future__ import annotations

import re

from copilot_agent.scenario.overlay_types import RagRulesOverlay

_ACTIVE_RULES: RagRulesOverlay | None = None


def configure_rag_rules(rules: RagRulesOverlay | None) -> None:
    global _ACTIVE_RULES
    _ACTIVE_RULES = rules


def _rewrite_rules() -> tuple[tuple[str, str], ...]:
    if _ACTIVE_RULES and _ACTIVE_RULES.rewrite_rules:
        return _ACTIVE_RULES.rewrite_rules
    return ()


def _doc_type_hint_rules() -> tuple[tuple[str, str, float], ...]:
    if _ACTIVE_RULES and _ACTIVE_RULES.doc_type_hints:
        return _ACTIVE_RULES.doc_type_hints
    return ()


def rewrite_query(query: str) -> str:
    """Append platform terms when colloquial phrasing matches scenario rewrite rules."""
    text = query.strip()
    if not text:
        return text
    extras: list[str] = []
    for pattern, expansion in _rewrite_rules():
        if re.search(pattern, text, flags=re.IGNORECASE):
            extras.append(expansion)
    if not extras:
        return text
    return f"{text} {' '.join(extras)}"


def query_doc_type_hints(query: str) -> dict[str, float]:
    """Optional per-query doc_type multipliers from scenario overlay."""
    hints: dict[str, float] = {}
    for pattern, doc_type, boost in _doc_type_hint_rules():
        if re.search(pattern, query, flags=re.IGNORECASE):
            hints[doc_type] = max(hints.get(doc_type, 1.0), boost)
    return hints
