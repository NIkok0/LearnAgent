#!/usr/bin/env python
"""Run lightweight runtime verification cases in one process."""

from __future__ import annotations

import sys
import uuid
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
from scripts import verify_run_state_machine  # noqa: E402
from scripts import verify_thread_archive_api  # noqa: E402
from scripts import verify_thread_lifecycle_cleaner  # noqa: E402

def _case(fn: Callable[[], int], args: tuple[str, ...] = ()) -> Callable[[list[str] | None], int]:
    def run(_argv: list[str] | None = None) -> int:
        old_argv = sys.argv
        sys.argv = [fn.__module__, *args]
        try:
            return int(fn() or 0)
        finally:
            sys.argv = old_argv

    return run


_RUN_DOMAIN_ID = uuid.uuid4().hex[:8]

CASES = {
    "event_store": _case(runtime_event_store.main),
    "timeline": _case(runtime_timeline.main),
    "execution_engine": _case(runtime_execution_engine.main),
    "durability": _case(runtime_durability_v1.main),
    "run_state_machine": _case(
        verify_run_state_machine.main,
        (
            "--event-store-path",
            f"storage/verify-runtime-domain-run-state-{_RUN_DOMAIN_ID}.sqlite",
            "--thread-prefix",
            f"runtime-domain-run-state-{_RUN_DOMAIN_ID}",
        ),
    ),
    "thread_lifecycle": _case(
        verify_thread_lifecycle_cleaner.main,
        (
            "--event-store-path",
            f"storage/verify-runtime-domain-thread-lifecycle-{_RUN_DOMAIN_ID}.sqlite",
        ),
    ),
    "thread_archive_api": _case(
        verify_thread_archive_api.main,
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
