#!/usr/bin/env python
"""Verify eval-suite timeout and RAGAS profile handling."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.verify_eval_suite import (  # noqa: E402
    _coerce_output_text,
    _extract_summary_json,
    _load_checks_from_summary,
    _parse_key_values,
    _profiles,
    _proxy_summary_passed,
    _suite_status,
    _timeout_suite_status,
)


def _simulate_timeout_branch(exc: subprocess.TimeoutExpired, *, suite_timeout_seconds: int) -> dict[str, object]:
    stdout = _coerce_output_text(exc.stdout)
    stderr = _coerce_output_text(exc.stderr)
    kv = _parse_key_values(stdout + ("\n" + stderr if stderr else ""))
    status = _timeout_suite_status(kv)
    summary_json = _extract_summary_json(kv)
    checks = _load_checks_from_summary(summary_json)
    errors = [f"timeout_after_seconds={suite_timeout_seconds}"]
    if status in {"PASS", "SKIP"}:
        errors.append(f"timeout_after_{status.lower()}_signal")
    return {
        "status": status,
        "pass": status != "FAIL",
        "summary_json": summary_json,
        "checks": checks,
        "errors": errors,
        "stdout_tail": stdout.splitlines()[-12:],
        "stderr_tail": stderr.splitlines()[-12:],
    }


def main() -> int:
    rag_specs = {spec.suite_name: spec for spec in _profiles(enable_ragas=True)["rag"]}
    phase4_args = rag_specs["phase4_ragas"].args
    non_phase4_changed = {
        name: spec.args
        for name, spec in rag_specs.items()
        if name != "phase4_ragas" and spec.args == ("--mode", "auto")
    }
    timeout_pass = _simulate_timeout_branch(
        subprocess.TimeoutExpired(
            cmd=["python", "fake.py"],
            timeout=3,
            output=b"summary_json=artifacts/fake.json\nphase4_ragas=PASS\n",
            stderr=b"warning=late close\n",
        ),
        suite_timeout_seconds=3,
    )
    timeout_fail = _simulate_timeout_branch(
        subprocess.TimeoutExpired(
            cmd=["python", "fake.py"],
            timeout=5,
            output=b"phase4_ragas=FAIL\n",
            stderr=None,
        ),
        suite_timeout_seconds=5,
    )
    timeout_silent = _simulate_timeout_branch(
        subprocess.TimeoutExpired(
            cmd=["python", "fake.py"],
            timeout=7,
            output=None,
            stderr=None,
        ),
        suite_timeout_seconds=7,
    )
    proxy_pass_summary = {
        "phase4_ragas": "PASS",
        "proxy_metrics": {"docs_cases": 3, "gold_chunk_recall_at_k_avg": 0.9},
    }
    proxy_fail_summary = {
        "phase4_ragas": "PASS",
        "proxy_metrics": {"docs_cases": 0, "gold_chunk_recall_at_k_avg": 0.9},
    }
    rag_history_checks = _verify_rag_regression_detection()
    checks = {
        "bytes_decoded": _coerce_output_text(b"phase4_ragas=PASS\n") == "phase4_ragas=PASS\n",
        "str_preserved": _coerce_output_text("ok") == "ok",
        "none_empty": _coerce_output_text(None) == "",
        "enable_ragas_only_phase4": phase4_args == ("--mode", "auto", "--disable-vector", "--allow-missing-docs")
        and not non_phase4_changed,
        "timeout_pass_no_type_error": timeout_pass["status"] == "PASS"
        and "timeout_after_pass_signal" in timeout_pass["errors"],
        "timeout_fail_no_type_error": timeout_fail["status"] == "FAIL"
        and timeout_fail["errors"] == ["timeout_after_seconds=5"],
        "silent_timeout_fails": timeout_silent["status"] == "FAIL"
        and timeout_silent["errors"] == ["timeout_after_seconds=7"],
        "timeout_tail_decoded": timeout_pass["stderr_tail"] == ["warning=late close"],
        "phase4_proxy_summary_passed": _proxy_summary_passed(proxy_pass_summary),
        "phase4_proxy_summary_requires_cases": not _proxy_summary_passed(proxy_fail_summary),
        **rag_history_checks,
    }
    passed = all(checks.values())
    summary = {
        "suite_name": "eval_suite_timeout_v1",
        "status": "PASS" if passed else "FAIL",
        "checks": checks,
        "timeout_pass": timeout_pass,
        "timeout_fail": timeout_fail,
        "timeout_silent": timeout_silent,
        "phase4_args": list(phase4_args),
        "non_phase4_changed": non_phase4_changed,
    }
    summary_path = ROOT / "artifacts/runtime/eval-suite-timeout-v1-summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"checks={json.dumps(checks, ensure_ascii=False, sort_keys=True)}")
    print(f"summary_json={summary_path}")
    print(f"eval_suite_timeout_v1={'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


def _verify_rag_regression_detection() -> dict[str, bool]:
    from tempfile import TemporaryDirectory

    from copilot_agent.eval.rag_metrics_trend import detect_gold_recall_regression

    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        no_history = detect_gold_recall_regression(
            current={"gold_chunk_recall_at_k_avg": 0.9},
            history_dir=root / "missing",
        )
        history_dir = root / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        insufficient = detect_gold_recall_regression(
            current={"gold_chunk_recall_at_k_avg": 0.9},
            history_dir=history_dir,
        )
        (history_dir / "nightly-20260101T000000Z.json").write_text(
            json.dumps({"proxy_metrics": {"gold_chunk_recall_at_k_avg": 0.95}}, ensure_ascii=False),
            encoding="utf-8",
        )
        (history_dir / "nightly-20260102T000000Z.json").write_text(
            json.dumps({"proxy_metrics": {"gold_chunk_recall_at_k_avg": 0.96}}, ensure_ascii=False),
            encoding="utf-8",
        )
        regression = detect_gold_recall_regression(
            current={"gold_chunk_recall_at_k_avg": 0.88},
            history_dir=history_dir,
        )
    return {
        "rag_regression_no_history_warns": no_history.get("reason") == "no_history",
        "rag_regression_insufficient_history_warns": insufficient.get("reason") == "insufficient_history",
        "rag_regression_detects_drop": regression.get("regression") is True
        and regression.get("previous") == 0.95
        and regression.get("delta") == -0.07,
    }


if __name__ == "__main__":
    raise SystemExit(main())
