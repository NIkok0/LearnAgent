#!/usr/bin/env python
"""Run lightweight deterministic memory verification cases in one process."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["COPILOT_CAPABILITIES"] = "rag,http"
os.environ.setdefault("SCENARIO", "minimal")

from scripts._domain_verify import run_domain_verifier  # noqa: E402
from scripts.verify_cases import memory_conversion_eviction_v1  # noqa: E402
from scripts.verify_cases import memory_governance_v1  # noqa: E402
from scripts.verify_cases import memory_quality  # noqa: E402
from scripts.verify_cases import short_term_memory_formation_v1  # noqa: E402

CASES = {
    "short_term": short_term_memory_formation_v1.main,
    "conversion_eviction": memory_conversion_eviction_v1.main,
    "governance": memory_governance_v1.main,
    "quality": memory_quality.main,
}


def main(argv: list[str] | None = None) -> int:
    return run_domain_verifier(
        suite_name="memory_domain",
        cases=CASES,
        summary_json=str(ROOT / "artifacts/runtime/memory-domain-summary.json"),
        argv=argv,
    )


if __name__ == "__main__":
    raise SystemExit(main())
