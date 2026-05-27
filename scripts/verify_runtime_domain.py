#!/usr/bin/env python
"""Run lightweight runtime verification cases in one process."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts._domain_verify import run_domain_verifier  # noqa: E402
from scripts.verify_cases import runtime_durability_v1  # noqa: E402
from scripts.verify_cases import runtime_event_store  # noqa: E402
from scripts.verify_cases import runtime_execution_engine  # noqa: E402
from scripts.verify_cases import runtime_timeline  # noqa: E402

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
    "event_store": _case(runtime_event_store.main),
    "timeline": _case(runtime_timeline.main),
    "execution_engine": _case(runtime_execution_engine.main),
    "durability": _case(runtime_durability_v1.main),
}


def main(argv: list[str] | None = None) -> int:
    return run_domain_verifier(
        suite_name="runtime_domain",
        cases=CASES,
        summary_json=str(ROOT / "artifacts/runtime/runtime-domain-summary.json"),
        argv=argv,
    )


if __name__ == "__main__":
    raise SystemExit(main())
