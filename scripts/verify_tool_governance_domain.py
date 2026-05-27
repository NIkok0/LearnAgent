#!/usr/bin/env python
"""Run tool governance verification cases in one process."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts._domain_verify import run_domain_verifier  # noqa: E402
from scripts.verify_cases import tool_side_effect_governance_v1  # noqa: E402
from scripts.verify_cases import tool_side_effect_ledger_v1  # noqa: E402
from scripts.verify_cases import tool_side_effect_read_model_v1  # noqa: E402

CASES = {
    "ledger": tool_side_effect_ledger_v1.main,
    "read_model": tool_side_effect_read_model_v1.main,
    "governance": tool_side_effect_governance_v1.main,
}


def main(argv: list[str] | None = None) -> int:
    return run_domain_verifier(
        suite_name="tool_governance_domain",
        cases=CASES,
        summary_json=str(ROOT / "artifacts/runtime/tool-governance-domain-summary.json"),
        argv=argv,
    )


if __name__ == "__main__":
    raise SystemExit(main())
