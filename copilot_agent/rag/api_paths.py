from __future__ import annotations

import re
from dataclasses import dataclass

from copilot_agent.rag.schema import DocChunk
from copilot_agent.tools.whitelist import validate_get_path, validate_post_path

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    flags=re.IGNORECASE,
)
_HEADING_METHOD_PATH = re.compile(
    r"^###\s+(GET|POST)\s+(/[^\s]+)\s*$",
    flags=re.IGNORECASE | re.MULTILINE,
)
_INLINE_METHOD_PATH = re.compile(
    r"\b(GET|POST)\s+(/(?:api|actuator)[^\s`\"']+)",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class ApiPathHint:
    method: str
    path: str
    path_template: str
    source_file: str
    heading_path: str = ""
    score: float = 1.0

    def as_dict(self) -> dict[str, object]:
        return {
            "method": self.method,
            "path": self.path,
            "path_template": self.path_template,
            "source_file": self.source_file,
            "heading_path": self.heading_path,
            "score": self.score,
        }


def _materialize_path(template: str, query: str) -> str | None:
    if "{id}" in template:
        match = _UUID_RE.search(query)
        if not match:
            return None
        return template.replace("{id}", match.group(0))
    return template.split("?", 1)[0]


def _is_whitelisted(method: str, path: str) -> bool:
    if method.upper() == "GET":
        return validate_get_path(path) is None
    if method.upper() == "POST":
        return validate_post_path(path) is None
    return False


def _score_hint(
    *,
    method: str,
    path_template: str,
    chunk: DocChunk,
    query: str,
    structured: bool = False,
) -> float:
    score = 1.0
    if structured:
        score += 0.8
    if chunk.doc_type == "api_contract":
        score += 0.5
    if chunk.source.upper().startswith("API-CONTRACT"):
        score += 0.3
    lowered = query.lower()
    if "health" in lowered and "health" in path_template.lower():
        score += 0.4
    if "job" in lowered and "jobs" in path_template.lower():
        score += 0.4
    if "file" in lowered and "files" in path_template.lower():
        score += 0.3
    if method.upper() == "GET":
        score += 0.05
    return score


def extract_api_paths(
    hits: list[DocChunk],
    *,
    query: str = "",
    max_hints: int = 6,
) -> list[ApiPathHint]:
    seen: set[tuple[str, str]] = set()
    hints: list[ApiPathHint] = []

    for chunk in hits:
        if chunk.api_endpoint is not None:
            method = chunk.api_endpoint.method.upper()
            template = chunk.api_endpoint.path
            concrete = _materialize_path(template, query)
            if concrete and _is_whitelisted(method, concrete):
                key = (method, concrete)
                if key not in seen:
                    seen.add(key)
                    hints.append(
                        ApiPathHint(
                            method=method,
                            path=concrete,
                            path_template=template.split("?", 1)[0],
                            source_file=chunk.source,
                            heading_path=chunk.heading_path or chunk.section_title,
                            score=_score_hint(
                                method=method,
                                path_template=template,
                                chunk=chunk,
                                query=query,
                                structured=True,
                            ),
                        )
                    )

        candidates: list[tuple[str, str]] = []
        for match in _HEADING_METHOD_PATH.finditer(chunk.text):
            candidates.append((match.group(1).upper(), match.group(2)))
        for match in _INLINE_METHOD_PATH.finditer(chunk.text):
            candidates.append((match.group(1).upper(), match.group(2)))

        for method, template in candidates:
            concrete = _materialize_path(template, query)
            if not concrete or not _is_whitelisted(method, concrete):
                continue
            key = (method, concrete)
            if key in seen:
                continue
            seen.add(key)
            hints.append(
                ApiPathHint(
                    method=method,
                    path=concrete,
                    path_template=template.split("?", 1)[0],
                    source_file=chunk.source,
                    heading_path=chunk.heading_path or chunk.section_title,
                    score=_score_hint(method=method, path_template=template, chunk=chunk, query=query),
                )
            )

    hints.sort(key=lambda item: item.score, reverse=True)
    return hints[:max_hints]


def merge_path_strings(*groups: tuple[str, ...]) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for path in group:
            normalized = str(path).split("?", 1)[0]
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            out.append(normalized)
    return tuple(out)
