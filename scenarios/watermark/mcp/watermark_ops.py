from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

log = logging.getLogger(__name__)

mcp = FastMCP("watermark_ops")


def _api_base() -> str:
    for env_name in ("WATERMARK_API_BASE_URL", "API_BASE_URL"):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            return raw.rstrip("/")
    return "http://127.0.0.1:8080"


def _docs_dir() -> Path:
    for env_name in ("COPILOT_DOCS_PATH", "WATERMARK_DOCS_PATH", "WATERMARK_DOCS_DIR"):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            candidate = Path(raw)
            if candidate.is_dir():
                return candidate.resolve()
    here = Path(__file__).resolve()
    scenario_docs = here.parent.parent / "docs"
    if scenario_docs.is_dir() and any(scenario_docs.glob("*.md")):
        return scenario_docs.resolve()
    for base in here.parents:
        fallback = base / "docs" / "source"
        if fallback.is_dir():
            return fallback.resolve()
    return scenario_docs.resolve()


_chunks_cache = None


def _search_docs(query: str, top_k: int = 5) -> str:
    global _chunks_cache
    from copilot_agent.rag import format_chunks_for_prompt
    from copilot_agent.rag.ingest import load_chunks
    from copilot_agent.rag.ingest_source import FileIngestSource
    from copilot_agent.rag.keyword import keyword_search

    docs_root = _docs_dir()
    if _chunks_cache is None:
        source = FileIngestSource(docs_root if docs_root.is_dir() else None)
        _chunks_cache = load_chunks(ingest_source=source)
    limit = max(1, min(int(top_k), 10))
    hits = keyword_search(_chunks_cache, query, top_k=limit)
    if not hits:
        return f"No documentation matches for query: {query!r}"
    return format_chunks_for_prompt(hits)


@mcp.tool()
def check_api_health() -> str:
    """Check scenario HTTP API health via GET /actuator/health."""
    url = f"{_api_base()}/actuator/health"
    try:
        response = httpx.get(url, timeout=10.0)
        body = response.text[:800]
        return f"status={response.status_code} url={url} body={body}"
    except Exception as exc:
        return f"error contacting {url}: {exc}"


@mcp.tool()
def search_platform_docs(query: str, top_k: int = 5) -> str:
    """Keyword search scenario documentation corpus (API, deploy, runbook, security)."""
    return _search_docs(query, top_k=top_k)


if __name__ == "__main__":
    mcp.run(transport="stdio")
