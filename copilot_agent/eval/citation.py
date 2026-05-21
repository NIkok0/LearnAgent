from __future__ import annotations

import re
from dataclasses import dataclass, field


def normalize_source_name(name: str) -> str:
    base = str(name or "").strip().lower()
    base = base.replace("\\", "/").split("/")[-1]
    return re.sub(r"\.md$", "", base)


def _source_tokens(name: str) -> set[str]:
    normalized = normalize_source_name(name)
    tokens = {normalized, normalized.replace("-", ""), normalized.replace("_", "")}
    compact = re.sub(r"[^a-z0-9]", "", normalized)
    if compact:
        tokens.add(compact)
    return {token for token in tokens if token}


def source_mentioned(answer: str, source_file: str) -> bool:
    text = str(answer or "").lower()
    if not text.strip():
        return False
    for token in _source_tokens(source_file):
        if token and token in text:
            return True
        if token and token.replace("-", " ") in text:
            return True
    raw = str(source_file or "").lower()
    return bool(raw and raw in text)


@dataclass(frozen=True)
class CitationVerdict:
    passed: bool
    required_source_coverage: float
    retrieval_citation_rate: float
    required_sources_ok: bool
    retrieval_sources_ok: bool
    missing_required: tuple[str, ...] = field(default_factory=tuple)
    uncited_retrieval: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "required_source_coverage": self.required_source_coverage,
            "retrieval_citation_rate": self.retrieval_citation_rate,
            "required_sources_ok": self.required_sources_ok,
            "retrieval_sources_ok": self.retrieval_sources_ok,
            "missing_required": list(self.missing_required),
            "uncited_retrieval": list(self.uncited_retrieval),
        }


def evaluate_citation(
    *,
    answer: str,
    retrieval_sources: list[str],
    required_sources: list[str] | None = None,
    require_all_retrieval: bool = False,
) -> CitationVerdict:
    required = list(required_sources or [])
    retrieval = list(dict.fromkeys(str(item) for item in retrieval_sources if item))

    missing_required = [src for src in required if not source_mentioned(answer, src)]
    required_coverage = 1.0 if not required else (len(required) - len(missing_required)) / len(required)

    uncited_retrieval = [src for src in retrieval if not source_mentioned(answer, src)]
    retrieval_rate = 1.0 if not retrieval else (len(retrieval) - len(uncited_retrieval)) / len(retrieval)

    required_ok = not missing_required
    retrieval_ok = not uncited_retrieval if require_all_retrieval else True
    passed = required_ok and retrieval_ok

    return CitationVerdict(
        passed=passed,
        required_source_coverage=required_coverage,
        retrieval_citation_rate=retrieval_rate,
        required_sources_ok=required_ok,
        retrieval_sources_ok=retrieval_ok,
        missing_required=tuple(missing_required),
        uncited_retrieval=tuple(uncited_retrieval),
    )
