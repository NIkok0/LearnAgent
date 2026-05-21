from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

MANIFEST_FILENAME = "docs_manifest.json"

_DEFAULT_LOAD_ORDER: tuple[str, ...] = (
    "REQUIREMENTS-CHECKLIST-AND-TEST-CASES.md",
    "API-CONTRACT.md",
    "DEPLOY-SERVER.md",
    "SECURITY-BASELINE.md",
    "RUNBOOK.md",
    "OPERATIONS-SLO-SLA.md",
    "watermark-java-backend-tech-selection.md",
    "README.md",
    "README_ALGORITHM.md",
)

_DEFAULT_DOC_TYPES: dict[str, str] = {
    "API-CONTRACT.md": "api_contract",
    "DEPLOY-SERVER.md": "deploy",
    "SECURITY-BASELINE.md": "security",
    "RUNBOOK.md": "runbook",
    "OPERATIONS-SLO-SLA.md": "operations",
    "REQUIREMENTS-CHECKLIST-AND-TEST-CASES.md": "requirements",
    "watermark-java-backend-tech-selection.md": "tech_selection",
    "README.md": "overview",
    "README_ALGORITHM.md": "algorithm",
}


@dataclass(frozen=True)
class DocsManifest:
    version: int
    load_order: tuple[str, ...]
    doc_types: dict[str, str]
    include_glob: str = "*.md"

    def filenames(self, *, docs_dir: Path | None = None) -> tuple[str, ...]:
        ordered = list(self.load_order)
        if docs_dir is not None and self.include_glob:
            extras = sorted(
                path.name
                for path in docs_dir.glob(self.include_glob)
                if path.is_file() and path.name not in ordered and path.name != MANIFEST_FILENAME
            )
            ordered.extend(extras)
        return tuple(ordered)

    def doc_type_for(self, filename: str) -> str:
        return self.doc_types.get(filename, "doc")


def default_manifest() -> DocsManifest:
    return DocsManifest(
        version=1,
        load_order=_DEFAULT_LOAD_ORDER,
        doc_types=dict(_DEFAULT_DOC_TYPES),
        include_glob="*.md",
    )


def load_docs_manifest(docs_dir: Path | None) -> DocsManifest:
    if docs_dir is None:
        return default_manifest()
    path = docs_dir / MANIFEST_FILENAME
    if not path.is_file():
        return default_manifest()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Invalid docs manifest at %s: %s; using defaults", path, exc)
        return default_manifest()
    if not isinstance(raw, dict):
        return default_manifest()
    load_order_raw = raw.get("load_order") or raw.get("files") or []
    if not isinstance(load_order_raw, list):
        load_order_raw = list(_DEFAULT_LOAD_ORDER)
    load_order = tuple(str(name) for name in load_order_raw if str(name).strip())
    doc_types_raw = raw.get("doc_types") or {}
    doc_types = dict(_DEFAULT_DOC_TYPES)
    if isinstance(doc_types_raw, dict):
        doc_types.update({str(k): str(v) for k, v in doc_types_raw.items()})
    include_glob = str(raw.get("include_glob") or "*.md")
    return DocsManifest(
        version=int(raw.get("version", 1)),
        load_order=load_order or _DEFAULT_LOAD_ORDER,
        doc_types=doc_types,
        include_glob=include_glob,
    )


def register_uploaded_file(docs_dir: Path, filename: str, *, doc_type: str = "doc") -> DocsManifest:
    """Append an uploaded markdown file to the manifest on disk."""
    manifest = load_docs_manifest(docs_dir)
    load_order = list(manifest.load_order)
    doc_types = dict(manifest.doc_types)
    if filename not in load_order:
        load_order.append(filename)
    doc_types.setdefault(filename, doc_type)
    updated = DocsManifest(
        version=manifest.version,
        load_order=tuple(load_order),
        doc_types=doc_types,
        include_glob=manifest.include_glob,
    )
    payload = {
        "version": updated.version,
        "load_order": list(updated.load_order),
        "doc_types": updated.doc_types,
        "include_glob": updated.include_glob,
    }
    path = docs_dir / MANIFEST_FILENAME
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return updated
