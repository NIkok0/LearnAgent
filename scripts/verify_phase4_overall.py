#!/usr/bin/env python
"""Phase 4 Step 3: rule checks + unified summary + trend compare."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _load_json(path: Path, *, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _safe_div(num: int, den: int) -> float:
    if den <= 0:
        return 0.0
    return num / den


def _trend(curr: float, prev: float) -> str:
    if math.isclose(curr, prev, rel_tol=1e-9, abs_tol=1e-9):
        return "flat"
    return "up" if curr > prev else "down"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Phase 4 unified summary and trend deltas.")
    parser.add_argument("--dataset", default=str(ROOT / "eval/phase4-eval-cases.json"))
    parser.add_argument(
        "--dataset-summary",
        default=str(ROOT / "artifacts/phase4/phase4-dataset-summary.json"),
        help="Step1 summary path.",
    )
    parser.add_argument(
        "--ragas-summary",
        default=str(ROOT / "artifacts/phase4/phase4-ragas-summary.json"),
        help="Step2 summary path.",
    )
    parser.add_argument(
        "--baseline-json",
        default=str(ROOT / "eval/phase4-baseline.json"),
        help="Baseline metric snapshot for trend deltas.",
    )
    parser.add_argument(
        "--summary-json",
        default=str(ROOT / "artifacts/phase4/phase4-overall-summary.json"),
        help="Output overall summary path.",
    )
    args = parser.parse_args()

    dataset_path = Path(args.dataset).resolve()
    dataset_summary_path = Path(args.dataset_summary).resolve()
    ragas_summary_path = Path(args.ragas_summary).resolve()
    baseline_path = Path(args.baseline_json).resolve()
    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    dataset = _load_json(dataset_path, default={})
    dataset_summary = _load_json(dataset_summary_path, default={})
    ragas_summary = _load_json(ragas_summary_path, default={})
    baseline = _load_json(baseline_path, default={})

    cases = dataset.get("cases", []) if isinstance(dataset, dict) else []
    if not isinstance(cases, list):
        cases = []
    total_cases = len(cases)

    docs_cases = [c for c in cases if isinstance(c, dict) and str(c.get("category")) == "docs"]
    api_cases = [c for c in cases if isinstance(c, dict) and str(c.get("category")) == "api"]
    safety_cases = [c for c in cases if isinstance(c, dict) and str(c.get("category")) == "safety"]

    docs_search_docs_ok = sum(
        1 for c in docs_cases if "search_docs" in [str(x) for x in c.get("expected_tools", [])]
    )
    api_http_ok = sum(
        1
        for c in api_cases
        if any(str(t).startswith("http_") for t in c.get("expected_tools", []))
    )
    blocked_cases = sum(1 for c in safety_cases if bool(c.get("expect_blocked", False)))
    blocked_forbidden_ok = sum(
        1 for c in safety_cases if bool(c.get("expect_blocked", False)) and bool(c.get("forbidden_tools", []))
    )

    rules_metrics = {
        "total_cases": total_cases,
        "docs_cases": len(docs_cases),
        "api_cases": len(api_cases),
        "safety_cases": len(safety_cases),
        "docs_search_docs_rate": round(_safe_div(docs_search_docs_ok, len(docs_cases)), 4),
        "api_http_tool_rate": round(_safe_div(api_http_ok, len(api_cases)), 4),
        "blocked_case_ratio": round(_safe_div(blocked_cases, len(safety_cases)), 4),
        "blocked_case_forbidden_tool_rate": round(_safe_div(blocked_forbidden_ok, blocked_cases), 4),
    }

    rule_errors: list[str] = []
    if rules_metrics["docs_search_docs_rate"] < 1.0:
        rule_errors.append("docs_search_docs_rate_below_1")
    if rules_metrics["api_http_tool_rate"] < 1.0:
        rule_errors.append("api_http_tool_rate_below_1")
    if blocked_cases == 0:
        rule_errors.append("no_blocked_safety_case")
    elif rules_metrics["blocked_case_forbidden_tool_rate"] < 1.0:
        rule_errors.append("blocked_case_forbidden_tool_rate_below_1")

    dataset_pass = str(dataset_summary.get("phase4_dataset", "FAIL")) == "PASS"
    ragas_pass = str(ragas_summary.get("phase4_ragas", "FAIL")) == "PASS"
    proxy = ragas_summary.get("proxy_metrics", {}) if isinstance(ragas_summary, dict) else {}

    current_snapshot = {
        "docs_search_docs_rate": rules_metrics["docs_search_docs_rate"],
        "api_http_tool_rate": rules_metrics["api_http_tool_rate"],
        "blocked_case_forbidden_tool_rate": rules_metrics["blocked_case_forbidden_tool_rate"],
        "retrieval_hit_rate": float(proxy.get("retrieval_hit_rate", 0.0)),
        "required_source_full_match_rate": float(proxy.get("required_source_full_match_rate", 0.0)),
        "avg_required_source_coverage": float(proxy.get("avg_required_source_coverage", 0.0)),
    }
    baseline_snapshot = baseline.get("metrics", {}) if isinstance(baseline, dict) else {}

    trend = {}
    for key, curr in current_snapshot.items():
        prev = float(baseline_snapshot.get(key, curr))
        trend[key] = {
            "current": round(curr, 4),
            "baseline": round(prev, 4),
            "delta": round(curr - prev, 4),
            "direction": _trend(curr, prev),
        }

    errors: list[str] = []
    if not dataset_pass:
        errors.append("step1_dataset_failed")
    if not ragas_pass:
        errors.append("step2_ragas_failed")
    errors.extend(rule_errors)

    summary = {
        "dataset_summary_path": str(dataset_summary_path),
        "ragas_summary_path": str(ragas_summary_path),
        "baseline_path": str(baseline_path),
        "step_status": {
            "phase4_dataset": "PASS" if dataset_pass else "FAIL",
            "phase4_ragas": "PASS" if ragas_pass else "FAIL",
            "phase4_rules": "PASS" if not rule_errors else "FAIL",
        },
        "rules_metrics": rules_metrics,
        "trend_vs_baseline": trend,
        "errors": errors,
        "phase4_overall": "PASS" if not errors else "FAIL",
    }
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"dataset_summary_path={summary['dataset_summary_path']}")
    print(f"ragas_summary_path={summary['ragas_summary_path']}")
    print(f"baseline_path={summary['baseline_path']}")
    print(f"phase4_rules={summary['step_status']['phase4_rules']}")
    print(f"docs_search_docs_rate={rules_metrics['docs_search_docs_rate']}")
    print(f"api_http_tool_rate={rules_metrics['api_http_tool_rate']}")
    print(f"blocked_case_forbidden_tool_rate={rules_metrics['blocked_case_forbidden_tool_rate']}")
    print(f"retrieval_hit_rate_delta={summary['trend_vs_baseline']['retrieval_hit_rate']['delta']}")
    print(
        "required_source_full_match_rate_delta="
        f"{summary['trend_vs_baseline']['required_source_full_match_rate']['delta']}"
    )
    print(f"summary_json={summary_path}")
    if errors:
        for item in errors:
            print(f"error={item}")
    if summary["phase4_overall"] == "PASS":
        print("phase4_overall=PASS")
        return 0
    print("phase4_overall=FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
