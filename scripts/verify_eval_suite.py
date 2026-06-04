#!/usr/bin/env python
"""Aggregate LearnAgent verify scripts into one eval suite summary."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from scripts.verify_manifest import (
    CONTRACT_SUITES,
    PROFILE_BUDGET_MS,
    SLOW_SUITE_WARNING_MS,
    SuiteSpec,
    profile_names,
    profiles,
)

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


def _suite_signal_values(kv: dict[str, str]) -> list[str]:
    return [
        value
        for key, value in kv.items()
        if key.lower().endswith("_pass")
        or key.lower().endswith("_mvp")
        or key.lower().endswith("_event_store")
        or key.lower().endswith("_timeline")
        or key.lower().endswith("_manager")
        or key.lower().endswith("_link")
        or key.lower().endswith("_scenarios")
        or key.lower().endswith("_ragas")
        or key.lower().endswith("_contract")
        or key in {
            "contract_events",
            "eval_cases_contract",
            "tool_audit_v1",
            "phase4_tool_trajectory",
            "verify_policy_credentials",
            "verify_context_manager",
            "verify_tool_router",
            "verify_rag_rerank",
            "phase3_step4",
            "phase3_safety_gate",
            "phase4_dataset",
            "rag_hot_reload",
        }
    ]


def _coerce_output_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


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
    if not isinstance(payload, dict):
        return {}
    checks: dict[str, Any] = {}
    nested = payload.get("checks")
    if isinstance(nested, dict):
        checks.update(nested)
    for key, value in payload.items():
        if key == "checks":
            continue
        if isinstance(value, bool) and (
            key.endswith("_ok")
            or key.endswith("_contract")
            or key in {"start_contract", "end_contract", "failure_contract", "timeline_contract", "persisted_sanitized"}
        ):
            checks[key] = value
    return checks


def _load_summary_payload(summary_json: str | None) -> dict[str, Any]:
    if not summary_json:
        return {}
    path = Path(summary_json)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _domain_suite_name(spec_suite_name: str, summary_payload: dict[str, Any]) -> str:
    raw = summary_payload.get("suite_name")
    name = str(raw or "").strip()
    return name or spec_suite_name


def _domain_failed_cases(suite_name: str, summary_payload: dict[str, Any]) -> list[str]:
    raw = summary_payload.get("failed_cases")
    if not isinstance(raw, list):
        return []
    domain_name = _domain_suite_name(suite_name, summary_payload)
    out: list[str] = []
    for item in raw:
        case = str(item or "").strip()
        if case:
            out.append(f"{domain_name}:{case}")
    return out


def _domain_case_reasons(suite_name: str, summary_payload: dict[str, Any]) -> dict[str, Any]:
    raw = summary_payload.get("case_reasons")
    if not isinstance(raw, dict):
        return {}
    domain_name = _domain_suite_name(suite_name, summary_payload)
    return {
        f"{domain_name}:{case}": reason
        for case, reason in raw.items()
        if str(case or "").strip()
    }


def _suite_status(return_code: int, kv: dict[str, str]) -> str:
    status, _source = _suite_status_with_source(return_code, kv, summary_payload={}, spec=None)
    return status


def _suite_status_with_source(
    return_code: int,
    kv: dict[str, str],
    *,
    summary_payload: dict[str, Any],
    spec: SuiteSpec | None,
) -> tuple[str, str]:
    if return_code != 0:
        return "FAIL", "exit_code"

    for key in _status_candidate_keys(spec):
        status = _status_from_value(summary_payload.get(key))
        if status:
            return status, f"summary_json:{key}"
    for key in ("status",):
        status = _status_from_value(summary_payload.get(key))
        if status:
            return status, f"summary_json:{key}"

    for key in _status_candidate_keys(spec):
        status = _status_from_value(kv.get(key))
        if status:
            return status, f"stdout:{key}"

    pass_like = _suite_signal_values(kv)
    if not pass_like:
        return "PASS", "no_status_signal_exit_zero"
    if any(str(item).strip().lower() == "skip" for item in pass_like):
        return "SKIP", "fallback_stdout_heuristic"
    states = [_to_bool(item) for item in pass_like]
    return ("PASS" if all(state is not False for state in states) else "FAIL"), "fallback_stdout_heuristic"


def _timeout_suite_status(kv: dict[str, str]) -> str:
    _ = kv
    return "FAIL"


def _status_candidate_keys(spec: SuiteSpec | None) -> list[str]:
    if spec is None:
        return []
    keys: list[str] = []
    if spec.status_key:
        keys.append(spec.status_key)
    keys.append(spec.suite_name)
    return list(dict.fromkeys(keys))


def _status_from_value(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip().upper()
    if text in {"PASS", "FAIL", "SKIP"}:
        return text
    bool_value = _to_bool(str(value))
    if bool_value is True:
        return "PASS"
    if bool_value is False:
        return "FAIL"
    return None


def _proxy_summary_passed(summary: dict[str, Any]) -> bool:
    if str(summary.get("phase4_ragas") or "").upper() != "PASS":
        return False
    proxy_metrics = summary.get("proxy_metrics")
    if not isinstance(proxy_metrics, dict):
        return False
    return int(proxy_metrics.get("docs_cases") or 0) >= 3


def _nightly_metrics_skip_reason(payload: dict[str, Any]) -> str:
    if str(payload.get("status") or "").upper() != "SKIP":
        return ""
    reason = str(payload.get("skip_reason") or "").strip() or "unknown"
    return f"nightly_metrics_skipped:{reason}"


def _nightly_metrics_timeout_reason(payload: dict[str, Any]) -> str:
    if str(payload.get("status") or "").upper() != "TIMEOUT":
        return ""
    stage = str(payload.get("timeout_stage") or payload.get("stage") or "").strip() or "unknown"
    return f"nightly_metrics_timeout:{stage}"


def _write_nightly_timeout_artifact(path: Path, *, suite_timeout_seconds: int) -> None:
    existing: dict[str, Any] = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    if str(existing.get("status") or "").upper() not in {"", "RUNNING"}:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload = {
        "timestamp": now,
        "started_at": existing.get("started_at", now),
        "profile": existing.get("profile", "nightly"),
        "status": "TIMEOUT",
        "stage": "suite_timeout",
        "timeout_stage": "suite_timeout",
        "timeout_seconds": int(suite_timeout_seconds),
        "skip_reason": "",
        "embedding_model": existing.get("embedding_model", "n/a"),
        "vector_enabled": existing.get("vector_enabled", False),
        "rerank_enabled": existing.get("rerank_enabled", False),
        "proxy_metrics": existing.get("proxy_metrics") if isinstance(existing.get("proxy_metrics"), dict) else {},
        "preconditions": existing.get("preconditions") if isinstance(existing.get("preconditions"), dict) else {},
        "errors": [f"suite_timeout_after_seconds={suite_timeout_seconds}"],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run aggregated eval suite profiles.")
    parser.add_argument(
        "--profile",
        choices=profile_names(),
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

    suites = profiles(enable_ragas=bool(args.enable_ragas))[args.profile]
    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    for spec in suites:
        start = time.perf_counter()
        cmd = [sys.executable, str((ROOT / spec.script).resolve()), *spec.args]
        env = dict(os.environ)
        if spec.rag_related:
            env.setdefault("SCENARIO", "watermark")
            if spec.suite_name != "phase4_ragas_nightly":
                env["RAG_USE_VECTOR"] = "false"
                env["RAG_RERANK_ENABLED"] = "false"
        if spec.suite_name == "phase4_ragas_nightly":
            env["RAG_USE_VECTOR"] = "true"
            env["RAG_RERANK_ENABLED"] = "true"
            env["RAG_EMBEDDING_MODEL"] = "BAAI/bge-large-zh-v1.5"
        if spec.rag_related:
            env.setdefault("SCENARIO", "watermark")
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(ROOT),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=args.suite_timeout_seconds,
            )
            elapsed = int((time.perf_counter() - start) * 1000)
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            kv = _parse_key_values(stdout + ("\n" + stderr if stderr else ""))
            summary_json = _extract_summary_json(kv)
            checks = _load_checks_from_summary(summary_json)
            summary_payload = _load_summary_payload(summary_json)
            suite_failed_cases = _domain_failed_cases(spec.suite_name, summary_payload)
            case_reasons = _domain_case_reasons(spec.suite_name, summary_payload)
            status, status_source = _suite_status_with_source(
                proc.returncode,
                kv,
                summary_payload=summary_payload,
                spec=spec,
            )
            errors: list[str] = []
            if proc.returncode != 0:
                errors.append(f"exit_code={proc.returncode}")
            if stderr.strip():
                errors.append("stderr_present")
            results.append(
                {
                    "suite_name": spec.suite_name,
                    "script": spec.script,
                    "pass": status != "FAIL",
                    "status": status,
                    "status_source": status_source,
                    "duration_ms": elapsed,
                    "summary_json": summary_json,
                    "checks": checks,
                    "failed_cases": suite_failed_cases,
                    "case_reasons": case_reasons,
                    "artifacts": [summary_json] if summary_json else [],
                    "errors": errors,
                    "stdout_tail": stdout.strip().splitlines()[-12:],
                    "stderr_tail": stderr.strip().splitlines()[-12:] if stderr.strip() else [],
                }
            )
        except subprocess.TimeoutExpired as exc:
            elapsed = int((time.perf_counter() - start) * 1000)
            stdout = _coerce_output_text(exc.stdout)
            stderr = _coerce_output_text(exc.stderr)
            if spec.suite_name == "phase4_ragas_nightly":
                _write_nightly_timeout_artifact(
                    ROOT / "artifacts/eval/rag_metrics/nightly-latest.json",
                    suite_timeout_seconds=args.suite_timeout_seconds,
                )
            kv = _parse_key_values(stdout + ("\n" + stderr if stderr else ""))
            status = _timeout_suite_status(kv)
            status_source = "timeout"
            summary_json = _extract_summary_json(kv)
            checks = _load_checks_from_summary(summary_json)
            summary_payload = _load_summary_payload(summary_json)
            suite_failed_cases = _domain_failed_cases(spec.suite_name, summary_payload)
            case_reasons = _domain_case_reasons(spec.suite_name, summary_payload)
            out = stdout.splitlines()[-12:]
            err = stderr.splitlines()[-12:]
            timeout_error = f"timeout_after_seconds={args.suite_timeout_seconds}"
            errors = [timeout_error]
            if spec.suite_name == "phase4_ragas" and _proxy_summary_passed(summary_payload):
                errors.append("timeout_after_proxy_summary_pass_ignored")
            results.append(
                {
                    "suite_name": spec.suite_name,
                    "script": spec.script,
                    "pass": status != "FAIL",
                    "status": status,
                    "status_source": status_source,
                    "duration_ms": elapsed,
                    "summary_json": summary_json,
                    "checks": checks,
                    "failed_cases": suite_failed_cases,
                    "case_reasons": case_reasons,
                    "artifacts": [summary_json] if summary_json else [],
                    "errors": errors,
                    "stdout_tail": out,
                    "stderr_tail": err,
                }
            )
    failed = [item for item in results if item["status"] == "FAIL"]
    skipped = [item for item in results if item["status"] == "SKIP"]
    failed_cases = [
        str(case)
        for item in failed
        for case in (item.get("failed_cases") if isinstance(item.get("failed_cases"), list) else [])
    ]
    failed_case_reasons: dict[str, Any] = {}
    for item in failed:
        suite_name = str(item.get("suite_name") or "")
        reasons = item.get("case_reasons")
        if not isinstance(reasons, dict):
            continue
        for case, reason in reasons.items():
            failed_case_reasons[str(case)] = reason
    slow_suites = sorted(
        (
            {
                "suite_name": str(item["suite_name"]),
                "duration_ms": int(item["duration_ms"]),
                "status": str(item["status"]),
            }
            for item in results
        ),
        key=lambda item: item["duration_ms"],
        reverse=True,
    )[:5]
    overall_pass = not failed
    duration_total_ms = sum(int(item["duration_ms"]) for item in results)
    profile_budget_ms = PROFILE_BUDGET_MS.get(args.profile)
    budget_warnings = [
        f"slow_suite:{item['suite_name']}:{item['duration_ms']}ms"
        for item in results
        if int(item["duration_ms"]) > SLOW_SUITE_WARNING_MS
    ]
    budget_status = "not_enforced"
    if profile_budget_ms is not None:
        budget_status = "PASS" if duration_total_ms <= profile_budget_ms else "FAIL"
        if budget_status == "FAIL":
            overall_pass = False
    failed_scenarios = [
        item["suite_name"]
        for item in failed
        if "scenario" in str(item["suite_name"]) or item["suite_name"] == "golden_scenarios"
    ]
    runtime_contract_breaks: list[str] = []
    for item in failed:
        suite_name = str(item["suite_name"])
        if not (suite_name.startswith("runtime_") or suite_name in {"session_mvp"}):
            continue
        item_failed_cases = item.get("failed_cases")
        if isinstance(item_failed_cases, list) and item_failed_cases:
            runtime_contract_breaks.extend(str(case) for case in item_failed_cases)
        else:
            runtime_contract_breaks.append(suite_name)
    contract_suite_names = {spec.suite_name for spec in CONTRACT_SUITES}
    contract_metrics: dict[str, Any] = {}
    contract_schema_ok = True
    for item in results:
        if item["suite_name"] not in contract_suite_names:
            continue
        checks = item.get("checks") if isinstance(item.get("checks"), dict) else {}
        contract_metrics[item["suite_name"]] = checks
        if not item["pass"]:
            contract_schema_ok = False
        if checks.get("contract_schema_ok") is False:
            contract_schema_ok = False
    collect_rag_metrics = args.profile in {"rag", "full"}
    rag_metrics: dict[str, Any] = {}
    nightly_metrics_status = ""
    nightly_metrics_skip_reason = ""
    nightly_metrics_skipped = False
    nightly_metrics_timeout_reason = ""
    nightly_metrics_timed_out = False
    nightly_metrics_path = ROOT / "artifacts/eval/rag_metrics/nightly-latest.json"
    collect_nightly_metrics = args.profile == "full"
    has_nightly_suite = collect_nightly_metrics and any(item["suite_name"] == "phase4_ragas_nightly" for item in results)
    if collect_nightly_metrics and nightly_metrics_path.is_file():
        try:
            nightly_payload = json.loads(nightly_metrics_path.read_text(encoding="utf-8"))
            nightly_metrics_status = str(nightly_payload.get("status") or "")
            nightly_metrics_skip_reason = _nightly_metrics_skip_reason(nightly_payload)
            nightly_metrics_skipped = nightly_metrics_status == "SKIP"
            nightly_metrics_timeout_reason = _nightly_metrics_timeout_reason(nightly_payload)
            nightly_metrics_timed_out = nightly_metrics_status == "TIMEOUT"
            if isinstance(nightly_payload.get("proxy_metrics"), dict):
                if not nightly_metrics_skipped and not nightly_metrics_timed_out:
                    rag_metrics = nightly_payload["proxy_metrics"]
                    rag_metrics["profile"] = nightly_payload.get("profile", "nightly")
        except Exception:
            pass
    if collect_rag_metrics and not rag_metrics and not nightly_metrics_skipped and not nightly_metrics_timed_out:
        for item in results:
            if item["suite_name"] not in {"phase4_ragas", "phase4_ragas_nightly"}:
                continue
            metrics_json = item.get("summary_json")
            if not metrics_json:
                continue
            try:
                payload = json.loads(Path(str(metrics_json)).read_text(encoding="utf-8"))
            except Exception:
                continue
            proxy_metrics = payload.get("proxy_metrics")
            if isinstance(proxy_metrics, dict):
                has_cases = int(proxy_metrics.get("docs_cases", 0) or 0) > 0
                if has_cases or item["suite_name"] != "phase4_ragas_nightly":
                    rag_metrics = proxy_metrics
            if item["suite_name"] == "phase4_ragas_nightly":
                break
            if item["suite_name"] == "phase4_ragas":
                rag_metrics = proxy_metrics if isinstance(proxy_metrics, dict) else rag_metrics

    rag_regression: dict[str, Any] = {}
    rag_regression_warnings: list[str] = []
    if args.profile == "full" and has_nightly_suite:
        if nightly_metrics_path.is_file() and nightly_metrics_skipped:
            reason = nightly_metrics_skip_reason or "nightly_metrics_skipped:unknown"
            rag_regression = {
                "regression": False,
                "reason": reason,
                "required_path": str(nightly_metrics_path),
            }
            rag_regression_warnings.append(f"rag_regression:{reason}")
            overall_pass = False
        elif nightly_metrics_path.is_file() and nightly_metrics_timed_out:
            reason = nightly_metrics_timeout_reason or "nightly_metrics_timeout:unknown"
            rag_regression = {
                "regression": False,
                "reason": reason,
                "required_path": str(nightly_metrics_path),
            }
            rag_regression_warnings.append(f"rag_regression:{reason}")
            overall_pass = False
        elif not nightly_metrics_path.is_file():
            rag_regression = {
                "regression": False,
                "reason": "nightly_metrics_missing",
                "required_path": str(nightly_metrics_path),
            }
            rag_regression_warnings.append("rag_regression:nightly_metrics_missing")
            overall_pass = False
    if collect_rag_metrics and rag_metrics and not nightly_metrics_skipped and not nightly_metrics_timed_out:
        from copilot_agent.eval.rag_metrics_trend import detect_gold_recall_regression  # noqa: WPS433

        rag_regression = detect_gold_recall_regression(
            current=rag_metrics,
            history_dir=ROOT / "artifacts/eval/rag_metrics/history",
            profile_prefix="nightly",
        )
        if rag_regression.get("regression"):
            overall_pass = False
        reason = str(rag_regression.get("reason") or "")
        if reason in {"no_history", "insufficient_history"}:
            rag_regression_warnings.append(f"rag_regression:{reason}")
        if args.profile == "full" and has_nightly_suite and reason == "metric_missing":
            overall_pass = False
            rag_regression_warnings.append("rag_regression:metric_missing")

    out = {
        "profile": args.profile,
        "enable_ragas": bool(args.enable_ragas),
        "overall_pass": overall_pass,
        "suites_total": len(results),
        "suites_failed": len(failed),
        "failed_suites": [item["suite_name"] for item in failed],
        "failed_cases": failed_cases,
        "failed_case_reasons": failed_case_reasons,
        "skipped_suites": [item["suite_name"] for item in skipped],
        "failed_scenarios": failed_scenarios,
        "runtime_contract_breaks": runtime_contract_breaks,
        "rag_metrics": rag_metrics,
        "rag_regression": rag_regression,
        "contract_schema_ok": contract_schema_ok,
        "contract_metrics": contract_metrics,
        "duration_total_ms": duration_total_ms,
        "perf_summary": {
            "total_duration_ms": duration_total_ms,
            "profile_budget_ms": profile_budget_ms,
            "budget_status": budget_status,
            "slow_suite_warning_ms": SLOW_SUITE_WARNING_MS,
            "warnings": [*budget_warnings, *rag_regression_warnings],
        },
        "slow_suites": slow_suites,
        "results": results,
        "eval_suite": "PASS" if overall_pass else "FAIL",
    }
    summary_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"profile={out['profile']}")
    print(f"enable_ragas={out['enable_ragas']}")
    print(f"suites_total={out['suites_total']}")
    print(f"suites_failed={out['suites_failed']}")
    print(f"failed_suites={','.join(out['failed_suites'])}")
    print(f"failed_cases={','.join(out['failed_cases'])}")
    print(f"failed_case_reasons={json.dumps(out['failed_case_reasons'], ensure_ascii=False)}")
    print(f"skipped_suites={','.join(out['skipped_suites'])}")
    print(f"failed_scenarios={','.join(out['failed_scenarios'])}")
    print(f"runtime_contract_breaks={','.join(out['runtime_contract_breaks'])}")
    print(f"contract_schema_ok={out['contract_schema_ok']}")
    print(f"contract_metrics={json.dumps(out['contract_metrics'], ensure_ascii=False)}")
    print(f"rag_metrics={json.dumps(out['rag_metrics'], ensure_ascii=False)}")
    print(f"rag_regression={json.dumps(out.get('rag_regression', {}), ensure_ascii=False)}")
    print(f"duration_total_ms={out['duration_total_ms']}")
    print(f"perf_summary={json.dumps(out['perf_summary'], ensure_ascii=False)}")
    print(f"slow_suites={json.dumps(out['slow_suites'], ensure_ascii=False)}")
    print(f"summary_json={summary_path}")
    print(f"eval_suite={out['eval_suite']}")
    return 0 if overall_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
