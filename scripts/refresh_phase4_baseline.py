#!/usr/bin/env python
"""Phase 4 Step 4: refresh baseline metrics from latest overall summary."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh phase4 baseline from overall summary.")
    parser.add_argument(
        "--overall-summary",
        default=str(ROOT / "artifacts/phase4/phase4-overall-summary.json"),
        help="Path to phase4 overall summary JSON.",
    )
    parser.add_argument(
        "--baseline-json",
        default=str(ROOT / "eval/phase4-baseline.json"),
        help="Path to baseline JSON file to update.",
    )
    parser.add_argument("--git-sha", default="", help="Optional git sha to record.")
    parser.add_argument("--run-id", default="", help="Optional CI run id to record.")
    args = parser.parse_args()

    overall_path = Path(args.overall_summary).resolve()
    baseline_path = Path(args.baseline_json).resolve()
    baseline_path.parent.mkdir(parents=True, exist_ok=True)

    overall = _load_json(overall_path, default={})
    if not isinstance(overall, dict):
        print("error=invalid_overall_summary_format")
        print("phase4_baseline_refresh=FAIL")
        return 1

    if str(overall.get("phase4_overall", "FAIL")) != "PASS":
        print("error=phase4_overall_not_pass")
        print("phase4_baseline_refresh=SKIP")
        return 0

    rules = overall.get("rules_metrics", {}) if isinstance(overall.get("rules_metrics"), dict) else {}
    trend = overall.get("trend_vs_baseline", {}) if isinstance(overall.get("trend_vs_baseline"), dict) else {}

    metrics = {
        "docs_search_docs_rate": float(rules.get("docs_search_docs_rate", 0.0)),
        "api_http_tool_rate": float(rules.get("api_http_tool_rate", 0.0)),
        "blocked_case_forbidden_tool_rate": float(rules.get("blocked_case_forbidden_tool_rate", 0.0)),
        "retrieval_hit_rate": float((trend.get("retrieval_hit_rate", {}) or {}).get("current", 0.0)),
        "required_source_full_match_rate": float(
            (trend.get("required_source_full_match_rate", {}) or {}).get("current", 0.0)
        ),
        "avg_required_source_coverage": float((trend.get("avg_required_source_coverage", {}) or {}).get("current", 0.0)),
    }

    existing = _load_json(baseline_path, default={})
    existing_metrics = existing.get("metrics", {}) if isinstance(existing, dict) else {}
    changed = any(float(existing_metrics.get(k, -1.0)) != v for k, v in metrics.items())

    updated = {
        "version": "1.0.0",
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": {
            "overall_summary": str(overall_path),
            "git_sha": args.git_sha,
            "run_id": args.run_id,
        },
        "metrics": metrics,
    }
    baseline_path.write_text(json.dumps(updated, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"baseline_json={baseline_path}")
    print(f"changed={changed}")
    for key, value in metrics.items():
        print(f"metric_{key}={value}")
    print("phase4_baseline_refresh=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
