#!/usr/bin/env python
"""Run tool governance verification cases in one process."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import time
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import verify_tool_side_effect_governance_v1  # noqa: E402
from scripts import verify_tool_side_effect_ledger_v1  # noqa: E402
from scripts import verify_tool_side_effect_read_model_v1  # noqa: E402

CaseFn = Callable[[list[str] | None], int]

CASES: dict[str, CaseFn] = {
    "ledger": verify_tool_side_effect_ledger_v1.main,
    "read_model": verify_tool_side_effect_read_model_v1.main,
    "governance": verify_tool_side_effect_governance_v1.main,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify tool governance deterministic cases.")
    parser.add_argument("--case", choices=[*CASES.keys(), "all"], default="all")
    parser.add_argument("--summary-json", default=str(ROOT / "artifacts/runtime/tool-governance-domain-summary.json"))
    args = parser.parse_args()

    names = list(CASES) if args.case == "all" else [str(args.case)]
    case_results = [_run_case(name, CASES[name]) for name in names]
    passed = all(item["status"] == "PASS" for item in case_results)
    checks = {str(item["case"]): item["status"] == "PASS" for item in case_results}
    summary = {
        "suite_name": "tool_governance_domain",
        "status": "PASS" if passed else "FAIL",
        "case": args.case,
        "checks": checks,
        "cases": case_results,
        "duration_ms": sum(int(item["duration_ms"]) for item in case_results),
    }
    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"checks={json.dumps(checks, ensure_ascii=False)}")
    print(f"summary_json={summary_path}")
    print(f"tool_governance_domain={'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


def _run_case(name: str, fn: CaseFn) -> dict[str, object]:
    start = time.perf_counter()
    stdout = io.StringIO()
    stderr = io.StringIO()
    status = "PASS"
    return_code = 0
    error = ""
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            return_code = int(fn([]) or 0)
        except SystemExit as exc:
            return_code = int(exc.code or 0) if isinstance(exc.code, int) else 1
        except Exception as exc:
            return_code = 1
            error = f"{type(exc).__name__}: {exc}"
    if return_code != 0:
        status = "FAIL"
    duration_ms = int((time.perf_counter() - start) * 1000)
    return {
        "case": name,
        "status": status,
        "return_code": return_code,
        "duration_ms": duration_ms,
        "stdout_tail": stdout.getvalue().splitlines()[-20:],
        "stderr_tail": stderr.getvalue().splitlines()[-20:],
        "error": error,
    }


if __name__ == "__main__":
    raise SystemExit(main())
