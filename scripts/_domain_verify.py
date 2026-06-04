from __future__ import annotations

import argparse
import contextlib
import io
import json
import time
from pathlib import Path
from typing import Callable

CaseFn = Callable[[list[str] | None], int]


def run_domain_verifier(
    *,
    suite_name: str,
    cases: dict[str, CaseFn],
    summary_json: str,
    argv: list[str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=f"Verify {suite_name} deterministic cases.")
    parser.add_argument("--case", choices=[*cases.keys(), "all"], default="all")
    parser.add_argument("--summary-json", default=summary_json)
    args = parser.parse_args(argv)

    names = list(cases) if args.case == "all" else [str(args.case)]
    case_results = [_run_case(name, cases[name]) for name in names]
    passed = all(item["status"] == "PASS" for item in case_results)
    checks = {str(item["case"]): item["status"] == "PASS" for item in case_results}
    failed_cases = [str(item["case"]) for item in case_results if item["status"] != "PASS"]
    case_status = {str(item["case"]): str(item["status"]) for item in case_results}
    case_reasons = {
        str(item["case"]): _case_reason(item)
        for item in case_results
        if item["status"] != "PASS"
    }
    summary = {
        "suite_name": suite_name,
        "status": "PASS" if passed else "FAIL",
        "case": args.case,
        "checks": checks,
        "failed_cases": failed_cases,
        "case_status": case_status,
        "case_reasons": case_reasons,
        "cases": case_results,
        "duration_ms": sum(int(item["duration_ms"]) for item in case_results),
    }
    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"checks={json.dumps(checks, ensure_ascii=False)}")
    print(f"failed_cases={','.join(failed_cases)}")
    print(f"summary_json={summary_path}")
    print(f"{suite_name}={'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


def _case_reason(item: dict[str, object]) -> str:
    error = str(item.get("error") or "").strip()
    if error:
        return error
    stderr_tail = item.get("stderr_tail")
    if isinstance(stderr_tail, list) and stderr_tail:
        return str(stderr_tail[-1])
    stdout_tail = item.get("stdout_tail")
    if isinstance(stdout_tail, list) and stdout_tail:
        return str(stdout_tail[-1])
    return f"return_code={item.get('return_code')}"


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
    return {
        "case": name,
        "status": status,
        "return_code": return_code,
        "duration_ms": int((time.perf_counter() - start) * 1000),
        "stdout_tail": stdout.getvalue().splitlines()[-20:],
        "stderr_tail": stderr.getvalue().splitlines()[-20:],
        "error": error,
    }
