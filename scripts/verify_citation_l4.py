#!/usr/bin/env python
"""L4 citation coverage checks (deterministic, no LLM judge)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.eval.citation import evaluate_citation, evaluate_structured_citations  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify L4 citation heuristics.")
    parser.add_argument(
        "--summary-json",
        default=str(ROOT / "artifacts/phase4/citation-l4-summary.json"),
    )
    args = parser.parse_args()

    good = evaluate_citation(
        answer="See DEPLOY-SERVER.md and watermark-java-backend-tech-selection.md for QUEUED troubleshooting.",
        retrieval_sources=["DEPLOY-SERVER.md", "watermark-java-backend-tech-selection.md"],
        required_sources=["DEPLOY-SERVER.md"],
    )
    bad = evaluate_citation(
        answer="Worker may be down.",
        retrieval_sources=["DEPLOY-SERVER.md"],
        required_sources=["DEPLOY-SERVER.md"],
    )
    structured = evaluate_structured_citations(
        citations=[
            {
                "source_file": "API-CONTRACT.md",
                "heading_path": "Auth",
                "start_line": 12,
                "chunk_id": "API-CONTRACT.md:12:abc",
                "authority": 95,
            }
        ],
        required_sources=["API-CONTRACT.md"],
    )

    checks = {
        "good_answer_passes": good.passed,
        "bad_answer_fails": not bad.passed,
        "required_coverage_good": good.required_source_coverage == 1.0,
        "missing_required_bad": "DEPLOY-SERVER.md" in bad.missing_required,
        "structured_citations_pass": structured.passed and structured.required_source_coverage == 1.0,
    }
    passed = all(checks.values())
    summary = {
        "suite_name": "citation_l4",
        "status": "PASS" if passed else "FAIL",
        "checks": checks,
        "good": good.as_dict(),
        "bad": bad.as_dict(),
        "structured": structured.as_dict(),
    }

    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"citation_l4={'PASS' if passed else 'FAIL'}")
    print(f"summary_json={summary_path}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
