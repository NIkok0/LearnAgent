#!/usr/bin/env python
"""Run lightweight runtime verification cases in one process."""

from __future__ import annotations

import sys
import uuid
from importlib import import_module
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts._domain_verify import run_domain_verifier  # noqa: E402


def _case(module_name: str, args: tuple[str, ...] = ()) -> Callable[[list[str] | None], int]:
    def run(_argv: list[str] | None = None) -> int:
        module = import_module(module_name)
        fn: Callable[[], int] = module.main
        old_argv = sys.argv
        sys.argv = [module_name, *args]
        try:
            return int(fn() or 0)
        finally:
            sys.argv = old_argv

    return run


_RUN_DOMAIN_ID = uuid.uuid4().hex[:8]

CASES = {
    "event_store": _case("scripts.verify_cases.runtime_event_store"),
    "timeline": _case("scripts.verify_cases.runtime_timeline"),
    "execution_engine": _case("scripts.verify_cases.runtime_execution_engine"),
    "durability": _case("scripts.verify_cases.runtime_durability_v1"),
    "run_state_machine": _case(
        "scripts.verify_run_state_machine",
        (
            "--event-store-path",
            f"storage/verify-runtime-domain-run-state-{_RUN_DOMAIN_ID}.sqlite",
            "--thread-prefix",
            f"runtime-domain-run-state-{_RUN_DOMAIN_ID}",
        ),
    ),
    "thread_lifecycle": _case(
        "scripts.verify_thread_lifecycle_cleaner",
        (
            "--event-store-path",
            f"storage/verify-runtime-domain-thread-lifecycle-{_RUN_DOMAIN_ID}.sqlite",
        ),
    ),
    "thread_archive_api": _case(
        "scripts.verify_thread_archive_api",
        (
            "--event-store-path",
            f"storage/verify-runtime-domain-thread-archive-{_RUN_DOMAIN_ID}.sqlite",
        ),
    ),
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
