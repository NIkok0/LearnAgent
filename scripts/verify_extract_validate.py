#!/usr/bin/env python
"""Verify unified ExtractedRecord validation for RAG API fields and Memory candidates."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.contracts.extract import (  # noqa: E402
    ExtractValidationError,
    validate_api_fields,
    validate_memory_candidate,
)
from copilot_agent.rag.api_parse import parse_api_section  # noqa: E402
from copilot_agent.rag.ingest import load_chunks  # noqa: E402


def main() -> int:
    login_text = ""
    health_text = ""
    for chunk in load_chunks(sources=("API-CONTRACT.md",)):
        if chunk.api_endpoint and chunk.api_endpoint.path == "/api/v1/auth/login":
            login_text = chunk.text
        if chunk.api_endpoint and chunk.api_endpoint.path == "/actuator/health":
            health_text = chunk.text

    login_meta = parse_api_section(
        section_title="POST /api/v1/auth/login",
        text=login_text,
        heading_path="Authentication > POST /api/v1/auth/login",
    )
    health_meta = parse_api_section(
        section_title="GET /actuator/health",
        text=health_text,
        heading_path="Health > GET /actuator/health",
    )

    login_record = validate_api_fields(login_meta.response_fields, endpoint_path="/api/v1/auth/login")
    health_record = validate_api_fields(health_meta.response_fields, endpoint_path="/actuator/health")
    memory_record = validate_memory_candidate(
        {
            "content": "User prefers concise deployment steps",
            "type": "preference",
            "scope": "user",
            "importance": 0.8,
            "confidence": 0.9,
        },
        extractor="llm",
    )

    login_names = {field.name for field in login_record.fields}
    health_names = {field.name for field in health_record.fields}

    checks = {
        "login_response_fields": login_names == {"success", "userId"},
        "health_response_fields": health_names == {"status"},
        "memory_record_valid": memory_record.record_type == "memory_item",
        "memory_scope_user": memory_record.scope == "user",
        "invalid_memory_rejected": False,
    }
    try:
        validate_memory_candidate({"content": ""}, extractor="rule")
    except ExtractValidationError:
        checks["invalid_memory_rejected"] = True

    passed = all(checks.values())
    summary = {
        "suite_name": "extract_validate",
        "status": "PASS" if passed else "FAIL",
        "checks": checks,
        "login_response_fields": sorted(login_names),
        "health_response_fields": sorted(health_names),
    }
    summary_path = ROOT / "artifacts/phase4/extract-validate-summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"checks={json.dumps(checks, ensure_ascii=False)}")
    print(f"summary_json={summary_path}")
    print(f"verify_extract_validate={'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
