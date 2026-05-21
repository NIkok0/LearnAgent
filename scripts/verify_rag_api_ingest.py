#!/usr/bin/env python
"""Verify structured API metadata parsed during Markdown ingest."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("SCENARIO", "watermark")

from copilot_agent.rag.api_parse import parse_api_section  # noqa: E402
from copilot_agent.rag.ingest import load_chunks  # noqa: E402


def _find_login_chunk(chunks):
    for chunk in chunks:
        if chunk.api_endpoint and chunk.api_endpoint.path == "/api/v1/auth/login":
            return chunk
    return None


def _find_health_chunk(chunks):
    for chunk in chunks:
        if chunk.api_endpoint and chunk.api_endpoint.path == "/actuator/health":
            return chunk
    return None


def _find_error_chunk(chunks):
    for chunk in chunks:
        if any(code.code == "UNAUTHORIZED" for code in chunk.error_codes):
            return chunk
    return None


def main() -> int:
    chunks = load_chunks(sources=("API-CONTRACT.md",))
    login = _find_login_chunk(chunks)
    health = _find_health_chunk(chunks)
    error_chunk = _find_error_chunk(chunks)

    login_fields = {field.name for field in login.request_fields} if login else set()
    login_response = {field.name for field in login.response_fields} if login else set()
    health_response = {field.name for field in health.response_fields} if health else set()
    parse_login = parse_api_section(
        section_title="POST /api/v1/auth/login",
        text=(login.text if login else ""),
        heading_path="Authentication > POST /api/v1/auth/login",
    )

    checks = {
        "chunks_loaded": len(chunks) > 0,
        "login_endpoint": login is not None and login.api_endpoint.method == "POST",
        "login_fields": login_fields == {"username", "password"},
        "login_response_fields": login_response == {"success", "userId"},
        "health_response_fields": health_response == {"status"},
        "health_endpoint": health is not None and health.api_endpoint.method == "GET",
        "error_codes": error_chunk is not None and any(
            code.code == "UNAUTHORIZED" for code in error_chunk.error_codes
        ),
        "chunk_index_present": all(chunk.chunk_index >= 0 for chunk in chunks),
        "updated_at_present": all(bool(chunk.updated_at) for chunk in chunks),
        "parser_unit_login_fields": {field.name for field in parse_login.request_fields} == {"username", "password"},
    }
    passed = all(checks.values())

    summary = {
        "suite_name": "rag_api_ingest",
        "status": "PASS" if passed else "FAIL",
        "checks": checks,
        "api_contract_chunks": len(chunks),
    }
    summary_path = ROOT / "artifacts/phase4/rag-api-ingest-summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"api_contract_chunks={len(chunks)}")
    print(f"login_response_fields={sorted(login_response)}")
    print(f"health_response_fields={sorted(health_response)}")
    print(f"checks={json.dumps(checks, ensure_ascii=False)}")
    print(f"summary_json={summary_path}")
    print(f"verify_rag_api_ingest={'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
