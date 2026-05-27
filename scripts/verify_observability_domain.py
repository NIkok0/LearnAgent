#!/usr/bin/env python
"""Run observability verification cases in one process."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts._domain_verify import run_domain_verifier  # noqa: E402
from scripts.verify_cases import observability_correlation  # noqa: E402
from scripts.verify_cases import observability_cost_v1  # noqa: E402
from scripts.verify_cases import observability_provider  # noqa: E402

def _case(fn: Callable[[], int]) -> Callable[[list[str] | None], int]:
    def run(_argv: list[str] | None = None) -> int:
        old_argv = sys.argv
        sys.argv = [fn.__module__]
        try:
            return int(fn() or 0)
        finally:
            sys.argv = old_argv

    return run


CASES = {
    "provider": _case(observability_provider.main),
    "correlation": _case(observability_correlation.main),
    "cost": _case(observability_cost_v1.main),
}


def main(argv: list[str] | None = None) -> int:
    return run_domain_verifier(
        suite_name="observability_domain",
        cases=CASES,
        summary_json=str(ROOT / "artifacts/runtime/observability-domain-summary.json"),
        argv=argv,
    )


if __name__ == "__main__":
    raise SystemExit(main())
