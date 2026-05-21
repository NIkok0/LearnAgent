from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path

from copilot_agent.rag.docs_manifest import DocsManifest, load_docs_manifest

log = logging.getLogger(__name__)


class IngestSource(ABC):
    """L1 ingestion abstraction: file / url / api implementations share one surface."""

    @abstractmethod
    def docs_dir(self) -> Path | None:
        raise NotImplementedError

    @abstractmethod
    def manifest(self) -> DocsManifest:
        raise NotImplementedError

    @abstractmethod
    def list_filenames(self) -> tuple[str, ...]:
        raise NotImplementedError

    @abstractmethod
    def read_text(self, filename: str) -> str | None:
        raise NotImplementedError


class FileIngestSource(IngestSource):
    """Load markdown from a local directory (env `WATERMARK_DOCS_PATH` or repo fallback)."""

    def __init__(self, base: Path | None) -> None:
        self._base = base
        self._manifest = load_docs_manifest(base)

    def docs_dir(self) -> Path | None:
        return self._base

    def manifest(self) -> DocsManifest:
        return self._manifest

    def list_filenames(self) -> tuple[str, ...]:
        return self._manifest.filenames(docs_dir=self._base)

    def read_text(self, filename: str) -> str | None:
        if self._base is None:
            return None
        path = self._base / filename
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")

    def refresh_manifest(self) -> None:
        self._manifest = load_docs_manifest(self._base)


class UrlIngestSource(IngestSource):
    """Placeholder for wave-4 web crawl ingest."""

    def __init__(self, urls: tuple[str, ...] = ()) -> None:
        self._urls = urls

    def docs_dir(self) -> Path | None:
        return None

    def manifest(self) -> DocsManifest:
        from copilot_agent.rag.docs_manifest import default_manifest

        return default_manifest()

    def list_filenames(self) -> tuple[str, ...]:
        return tuple()

    def read_text(self, filename: str) -> str | None:
        log.debug("UrlIngestSource.read_text not implemented for %s", filename)
        return None


class ApiIngestSource(IngestSource):
    """Placeholder for wave-4 remote API pull ingest."""

    def docs_dir(self) -> Path | None:
        return None

    def manifest(self) -> DocsManifest:
        from copilot_agent.rag.docs_manifest import default_manifest

        return default_manifest()

    def list_filenames(self) -> tuple[str, ...]:
        return tuple()

    def read_text(self, filename: str) -> str | None:
        log.debug("ApiIngestSource.read_text not implemented for %s", filename)
        return None
