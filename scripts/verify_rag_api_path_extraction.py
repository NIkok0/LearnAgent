#!/usr/bin/env python
"""Unit tests for API path extraction from RAG chunks."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.rag.api_paths import extract_api_paths, merge_path_strings  # noqa: E402
from copilot_agent.rag.schema import DocChunk  # noqa: E402


def main() -> int:
    job_id = str(uuid.uuid4())
    chunks = [
        DocChunk(
            source="API-CONTRACT.md",
            start_line=49,
            text="## Jobs\n\n### GET /api/v1/jobs/{id}\n\nQuery watermark job status.",
            section_title="GET /api/v1/jobs/{id}",
            heading_path="Jobs > GET /api/v1/jobs/{id}",
            doc_type="api",
        ),
        DocChunk(
            source="API-CONTRACT.md",
            start_line=27,
            text="## Health\n\n### GET /actuator/health\n\nLiveness probe.",
            section_title="GET /actuator/health",
            heading_path="Health",
            doc_type="api",
        ),
    ]

    hints = extract_api_paths(chunks, query=f"job status {job_id}")
    paths = [hint.path for hint in hints]
    checks = {
        "extracts_job_path": f"/api/v1/jobs/{job_id}" in paths,
        "extracts_health_path": "/actuator/health" in paths,
        "merge_paths": merge_path_strings(("/actuator/health",), (f"/api/v1/jobs/{job_id}", "/actuator/health",)) == (
            "/actuator/health",
            f"/api/v1/jobs/{job_id}",
        ),
    }
    passed = all(checks.values())

    print(f"extracts_job_path={checks['extracts_job_path']}")
    print(f"extracts_health_path={checks['extracts_health_path']}")
    print(f"merge_paths={checks['merge_paths']}")
    print(f"verify_rag_api_path_extraction={'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
