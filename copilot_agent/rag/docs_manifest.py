from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

MANIFEST_FILENAME = "docs_manifest.json"

_DEFAULT_LOAD_ORDER: tuple[str, ...] = ()
_DEFAULT_DOC_TYPES: dict[str, str] = {}


@dataclass(frozen=True)
class DocsManifest:
    version: int
    load_order: tuple[str, ...]
    doc_types: dict[str, str]
    include_glob: str = "*.md"
    doc_security: dict[str, dict[str, object]] = field(default_factory=dict)

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

    def security_for(self, filename: str) -> dict[str, object]:
        return dict(self.doc_security.get(filename, {}))


def default_manifest() -> DocsManifest:
    return DocsManifest(
        version=1,
        load_order=_DEFAULT_LOAD_ORDER,
        doc_types=dict(_DEFAULT_DOC_TYPES),
        include_glob="*.md",
        doc_security={},
    )


def manifest_from_docs_dir(docs_dir: Path) -> DocsManifest:
    ordered = sorted(
        path.name
        for path in docs_dir.glob("*.md")
        if path.is_file() and path.name != MANIFEST_FILENAME
    )
    return DocsManifest(version=1, load_order=tuple(ordered), doc_types={}, include_glob="*.md", doc_security={})


def load_docs_manifest(docs_dir: Path | None) -> DocsManifest:
    if docs_dir is None:
        return DocsManifest(version=1, load_order=(), doc_types={}, include_glob="*.md", doc_security={})
    path = docs_dir / MANIFEST_FILENAME
    if not path.is_file():
        if any(docs_dir.glob("*.md")):
            return manifest_from_docs_dir(docs_dir)
        return DocsManifest(version=1, load_order=(), doc_types={}, include_glob="*.md", doc_security={})
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Invalid docs manifest at %s: %s; falling back to glob", path, exc)
        if any(docs_dir.glob("*.md")):
            return manifest_from_docs_dir(docs_dir)
        return DocsManifest(version=1, load_order=(), doc_types={}, include_glob="*.md", doc_security={})
    if not isinstance(raw, dict):
        if any(docs_dir.glob("*.md")):
            return manifest_from_docs_dir(docs_dir)
        return DocsManifest(version=1, load_order=(), doc_types={}, include_glob="*.md")
    load_order_raw = raw.get("load_order") or raw.get("files") or []
    if not isinstance(load_order_raw, list):
        load_order_raw = []
    load_order = tuple(str(name) for name in load_order_raw if str(name).strip())
    if not load_order and any(docs_dir.glob("*.md")):
        load_order = manifest_from_docs_dir(docs_dir).load_order
    doc_types_raw = raw.get("doc_types") or {}
    doc_types: dict[str, str] = {}
    if isinstance(doc_types_raw, dict):
        doc_types = {str(k): str(v) for k, v in doc_types_raw.items()}
    security_raw = raw.get("doc_security") or raw.get("security") or {}
    doc_security: dict[str, dict[str, object]] = {}
    if isinstance(security_raw, dict):
        for key, value in security_raw.items():
            if isinstance(value, dict):
                doc_security[str(key)] = dict(value)
    include_glob = str(raw.get("include_glob") or "*.md")
    return DocsManifest(
        version=int(raw.get("version", 1)),
        load_order=load_order,
        doc_types=doc_types,
        include_glob=include_glob,
        doc_security=doc_security,
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
        doc_security=dict(manifest.doc_security),
    )
    payload = {
        "version": updated.version,
        "load_order": list(updated.load_order),
        "doc_types": updated.doc_types,
        "include_glob": updated.include_glob,
        "doc_security": updated.doc_security,
    }
    path = docs_dir / MANIFEST_FILENAME
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return updated
