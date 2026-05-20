#!/usr/bin/env python
"""Aggregate LearnAgent verify scripts into one eval suite summary."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@dataclass(frozen=True)
class SuiteSpec:
    suite_name: str
    script: str
    args: tuple[str, ...] = ()
    rag_related: bool = False


CORE_SUITES: tuple[SuiteSpec, ...] = (
    SuiteSpec(
        suite_name="runtime_event_store",
        script="scripts/verify_runtime_event_store.py",
        args=("--event-store-path", "storage/verify-runtime-events.sqlite"),
    ),
    SuiteSpec(
        suite_name="runtime_timeline",
        script="scripts/verify_runtime_timeline.py",
        args=("--event-store-path", "storage/verify-runtime-timeline-events.sqlite"),
    ),
    SuiteSpec(
        suite_name="runtime_checkpoint_link",
        script="scripts/verify_runtime_checkpoint_link.py",
        args=(
            "--event-store-path",
            "storage/verify-runtime-events.sqlite",
            "--checkpoint-path",
            "storage/verify-runtime-checkpoints.sqlite",
        ),
    ),
    SuiteSpec(
        suite_name="runtime_run_manager",
        script="scripts/verify_runtime_run_manager.py",
        args=("--event-store-path", "storage/verify-run-manager-events.sqlite"),
    ),
    SuiteSpec(
        suite_name="session_mvp",
        script="scripts/verify_session_mvp.py",
        args=("--event-store-path", "storage/verify-session-mvp-events.sqlite"),
    ),
)

RAG_SUITES: tuple[SuiteSpec, ...] = (
    SuiteSpec(
        suite_name="phase4_ragas",
        script="scripts/verify_phase4_ragas.py",
        args=("--mode", "proxy", "--disable-vector"),
        rag_related=True,
    ),
)


def _profiles(enable_ragas: bool) -> dict[str, tuple[SuiteSpec, ...]]:
    rag = tuple(
        SuiteSpec(
            suite_name=spec.suite_name,
            script=spec.script,
            args=("--mode", "auto") if enable_ragas and spec.rag_related else spec.args,
            rag_related=spec.rag_related,
        )
        for spec in RAG_SUITES
    )
    return {
        "core": CORE_SUITES,
        "rag": rag,
        "full": CORE_SUITES + rag,
    }


def _to_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered in {"true", "1", "yes", "pass"}:
        return True
    if lowered in {"false", "0", "no", "fail"}:
        return False
    return None


def _parse_key_values(output: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def _extract_summary_json(kv: dict[str, str]) -> str | None:
    value = kv.get("summary_json")
    if not value:
        return None
    p = Path(value)
    if p.is_absolute():
        return str(p)
    return str((ROOT / p).resolve())


def _load_checks_from_summary(summary_json: str | None) -> dict[str, Any]:
    if not summary_json:
        return {}
    path = Path(summary_json)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    checks = payload.get("checks")
    return checks if isinstance(checks, dict) else {}


def _suite_status(return_code: int, kv: dict[str, str]) -> str:
    if return_code != 0:
        return "FAIL"
    pass_like = [
        value
        for key, value in kv.items()
        if key.lower().endswith("_pass")
        or key.lower().endswith("_mvp")
        or key.lower().endswith("_event_store")
        or key.lower().endswith("_timeline")
        or key.lower().endswith("_manager")
        or key.lower().endswith("_link")
    ]
    if not pass_like:
        return "PASS"
    states = [_to_bool(item) for item in pass_like]
    return "PASS" if all(state is not False for state in states) else "FAIL"


def _run_suite(spec: SuiteSpec) -> dict[str, Any]:
    start = time.perf_counter()
    cmd = [sys.executable, str((ROOT / spec.script).resolve()), *spec.args]
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    elapsed = int((time.perf_counter() - start) * 1000)
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    kv = _parse_key_values(stdout + ("\n" + stderr if stderr else ""))
    summary_json = _extract_summary_json(kv)
    checks = _load_checks_from_summary(summary_json)
    status = _suite_status(proc.returncode, kv)
    errors: list[str] = []
    if proc.returncode != 0:
        errors.append(f"exit_code={proc.returncode}")
    if stderr.strip():
        errors.append("stderr_present")
    return {
        "suite_name": spec.suite_name,
        "script": spec.script,
        "status": status,
        "duration_ms": elapsed,
        "summary_json": summary_json,
        "checks": checks,
        "artifacts": [summary_json] if summary_json else [],
        "errors": errors,
        "stdout_tail": stdout.strip().splitlines()[-12:],
        "stderr_tail": stderr.strip().splitlines()[-12:] if stderr.strip() else [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run aggregated eval suite profiles.")
    parser.add_argument(
        "--profile",
        choices=["core", "rag", "full"],
        default="core",
        help="Which suite profile to execute.",
    )
    parser.add_argument(
        "--enable-ragas",
        action="store_true",
        help="Use --mode auto for rag suite; default uses deterministic proxy mode.",
    )
    parser.add_argument(
        "--summary-json",
        default=str(ROOT / "artifacts/eval/eval-suite-summary.json"),
        help="Path to write aggregated summary json.",
    )
    parser.add_argument(
        "--suite-timeout-seconds",
        type=int,
        default=180,
        help="Timeout per suite subprocess (seconds).",
    )
    args = parser.parse_args()

    suites = _profiles(enable_ragas=bool(args.enable_ragas))[args.profile]
    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    for spec in suites:
        start = time.perf_counter()
        cmd = [sys.executable, str((ROOT / spec.script).resolve()), *spec.args]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=args.suite_timeout_seconds,
            )
            elapsed = int((time.perf_counter() - start) * 1000)
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            kv = _parse_key_values(stdout + ("\n" + stderr if stderr else ""))
            summary_json = _extract_summary_json(kv)
            checks = _load_checks_from_summary(summary_json)
            status = _suite_status(proc.returncode, kv)
            errors: list[str] = []
            if proc.returncode != 0:
                errors.append(f"exit_code={proc.returncode}")
            if stderr.strip():
                errors.append("stderr_present")
            results.append(
                {
                    "suite_name": spec.suite_name,
                    "script": spec.script,
                    "status": status,
                    "duration_ms": elapsed,
                    "summary_json": summary_json,
                    "checks": checks,
                    "artifacts": [summary_json] if summary_json else [],
                    "errors": errors,
                    "stdout_tail": stdout.strip().splitlines()[-12:],
                    "stderr_tail": stderr.strip().splitlines()[-12:] if stderr.strip() else [],
                }
            )
        except subprocess.TimeoutExpired as exc:
            elapsed = int((time.perf_counter() - start) * 1000)
            out = (exc.stdout or "").splitlines()[-12:]
            err = (exc.stderr or "").splitlines()[-12:]
            results.append(
                {
                    "suite_name": spec.suite_name,
                    "script": spec.script,
                    "status": "FAIL",
                    "duration_ms": elapsed,
                    "summary_json": None,
                    "checks": {},
                    "artifacts": [],
                    "errors": [f"timeout_after_seconds={args.suite_timeout_seconds}"],
                    "stdout_tail": out,
                    "stderr_tail": err,
                }
            )
    failed = [item for item in results if item["status"] != "PASS"]
    overall_pass = not failed
    duration_total_ms = sum(int(item["duration_ms"]) for item in results)

    out = {
        "profile": args.profile,
        "enable_ragas": bool(args.enable_ragas),
        "overall_pass": overall_pass,
        "suites_total": len(results),
        "suites_failed": len(failed),
        "failed_suites": [item["suite_name"] for item in failed],
        "duration_total_ms": duration_total_ms,
        "results": results,
        "eval_suite": "PASS" if overall_pass else "FAIL",
    }
    summary_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"profile={out['profile']}")
    print(f"enable_ragas={out['enable_ragas']}")
    print(f"suites_total={out['suites_total']}")
    print(f"suites_failed={out['suites_failed']}")
    print(f"failed_suites={','.join(out['failed_suites'])}")
    print(f"duration_total_ms={out['duration_total_ms']}")
    print(f"summary_json={summary_path}")
    print(f"eval_suite={out['eval_suite']}")
    return 0 if overall_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
