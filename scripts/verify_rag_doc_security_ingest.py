#!/usr/bin/env python
"""Verify doc_security manifest fields propagate to ingested DocChunk metadata."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.rag.ingest import load_chunks  # noqa: E402
from copilot_agent.rag.security import AUTHORITY_BY_DOC_TYPE  # noqa: E402


def main() -> int:
    docs_dir = ROOT / "scenarios" / "watermark" / "docs"
    os.environ["COPILOT_DOCS_PATH"] = str(docs_dir)
    chunks = load_chunks()
    by_source: dict[str, list] = {}
    for chunk in chunks:
        by_source.setdefault(chunk.source, []).append(chunk)

    api_chunks = by_source.get("API-CONTRACT.md") or []
    security_chunks = by_source.get("SECURITY-BASELINE.md") or []
    deploy_chunks = by_source.get("DEPLOY-SERVER.md") or []

    checks = {
        "chunks_loaded": len(chunks) > 0,
        "api_contract_authority": bool(api_chunks) and all(c.authority == 95 for c in api_chunks),
        "api_contract_tenant": bool(api_chunks) and all(c.tenant_id == "default" for c in api_chunks),
        "security_baseline_acl": bool(security_chunks)
        and all(set(c.acl) == {"group:ops", "group:security"} for c in security_chunks),
        "security_baseline_classification": bool(security_chunks)
        and all(c.classification == "confidential" for c in security_chunks),
        "deploy_default_authority": bool(deploy_chunks)
        and all(c.authority == AUTHORITY_BY_DOC_TYPE["deploy"] for c in deploy_chunks),
        "all_chunks_have_tenant": all(c.tenant_id == "default" for c in chunks),
    }
    overall = all(checks.values())
    print(f"checks={json.dumps(checks, ensure_ascii=False, sort_keys=True)}")
    print(f"rag_doc_security_ingest={'PASS' if overall else 'FAIL'}")
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
