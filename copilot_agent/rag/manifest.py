from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from copilot_agent.rag.ingest import resolve_ingest_source, repo_docs_dir
from copilot_agent.rag.schema import DocChunk
from copilot_agent.settings import settings

log = logging.getLogger(__name__)

MANIFEST_VERSION = 1
MANIFEST_FILENAME = "rag_manifest.json"


@dataclass
class FileManifestEntry:
    file_fp: str
    chunk_ids: list[str] = field(default_factory=list)


@dataclass
class RagManifest:
    version: int
    embedding_model: str
    files: dict[str, FileManifestEntry]

    @classmethod
    def empty(cls) -> RagManifest:
        return cls(version=MANIFEST_VERSION, embedding_model=settings.rag_embedding_model, files={})


@dataclass(frozen=True)
class ManifestDelta:
    changed: tuple[str, ...]
    removed: tuple[str, ...]


def chroma_dir() -> Path:
    if settings.rag_chroma_path.strip():
        return Path(settings.rag_chroma_path)
    return Path(__file__).resolve().parent.parent.parent / "storage" / "chroma"


def manifest_path() -> Path:
    return chroma_dir() / MANIFEST_FILENAME


def file_fingerprint(path: Path) -> str:
    stat = path.stat()
    payload = f"{stat.st_mtime_ns}:{stat.st_size}".encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def compute_delta(manifest: RagManifest, *, docs_dir: Path | None = None) -> ManifestDelta:
    root = docs_dir or repo_docs_dir()
    source = resolve_ingest_source(root)
    current_names = source.list_filenames()
    changed: list[str] = []
    removed: list[str] = []
    known = set(manifest.files.keys())
    current = set(current_names)

    for name in sorted(known - current):
        removed.append(name)

    if root is None:
        return ManifestDelta(changed=tuple(), removed=tuple(removed))

    for name in current_names:
        path = root / name
        entry = manifest.files.get(name)
        if not path.is_file():
            if entry is not None:
                removed.append(name)
            continue
        fp = file_fingerprint(path)
        if entry is None or entry.file_fp != fp:
            changed.append(name)

    ordered_changed = tuple(name for name in current_names if name in changed)
    ordered_removed = tuple(dict.fromkeys(removed))
    return ManifestDelta(changed=ordered_changed, removed=ordered_removed)


def load_manifest() -> RagManifest:
    path = manifest_path()
    if not path.is_file():
        return RagManifest.empty()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.warning("Invalid RAG manifest at %s; starting fresh", path)
        return RagManifest.empty()
    if not isinstance(raw, dict):
        return RagManifest.empty()
    files: dict[str, FileManifestEntry] = {}
    for name, item in (raw.get("files") or {}).items():
        if not isinstance(item, dict):
            continue
        chunk_ids = item.get("chunk_ids") or []
        if not isinstance(chunk_ids, list):
            chunk_ids = []
        files[str(name)] = FileManifestEntry(
            file_fp=str(item.get("file_fp", "")),
            chunk_ids=[str(x) for x in chunk_ids if str(x).strip()],
        )
    return RagManifest(
        version=int(raw.get("version", MANIFEST_VERSION)),
        embedding_model=str(raw.get("embedding_model", settings.rag_embedding_model)),
        files=files,
    )


def save_manifest(manifest: RagManifest) -> None:
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": manifest.version,
        "embedding_model": manifest.embedding_model,
        "files": {
            name: {"file_fp": entry.file_fp, "chunk_ids": entry.chunk_ids}
            for name, entry in sorted(manifest.files.items())
        },
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def update_file_entry(manifest: RagManifest, source: str, chunks: list[DocChunk], *, docs_dir: Path) -> None:
    path = docs_dir / source
    manifest.files[source] = FileManifestEntry(
        file_fp=file_fingerprint(path) if path.is_file() else "",
        chunk_ids=[c.chunk_id for c in chunks],
    )


def remove_file_entry(manifest: RagManifest, source: str) -> list[str]:
    entry = manifest.files.pop(source, None)
    return list(entry.chunk_ids) if entry else []
