#!/usr/bin/env python
"""Validate golden scenario dataset shape for eval gating."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


REQUIRED_CASE_KEYS = {
    "id",
    "input",
    "must_have_events",
    "must_not_have_events",
    "expected_run_status",
    "notes",
}


def _is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _load_dataset(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify golden runtime scenario dataset schema.")
    parser.add_argument(
        "--dataset",
        default=str(ROOT / "eval/golden/runtime-golden-scenarios.json"),
        help="Path to golden scenarios JSON dataset.",
    )
    parser.add_argument(
        "--summary-json",
        default=str(ROOT / "artifacts/eval/golden-scenarios-summary.json"),
        help="Path to write structured summary.",
    )
    parser.add_argument("--min-cases", type=int, default=8, help="Minimum case count required.")
    args = parser.parse_args()

    dataset_path = Path(args.dataset).resolve()
    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    payload: dict[str, Any] = {}
    try:
        payload = _load_dataset(dataset_path)
    except Exception as exc:
        errors.append(f"dataset_read_error: {exc}")

    cases = payload.get("cases", []) if isinstance(payload, dict) else []
    if not isinstance(cases, list):
        errors.append("cases_not_list")
        cases = []

    duplicate_ids: list[str] = []
    missing_keys: list[str] = []
    invalid_fields: list[str] = []
    seen_ids: set[str] = set()

    for idx, case in enumerate(cases):
        case_name = f"case[{idx}]"
        if not isinstance(case, dict):
            invalid_fields.append(f"{case_name}:not_object")
            continue
        missing = REQUIRED_CASE_KEYS - set(case.keys())
        if missing:
            missing_keys.append(f"{case_name}:{','.join(sorted(missing))}")
            continue
        case_id = case.get("id")
        if not _is_non_empty_string(case_id):
            invalid_fields.append(f"{case_name}:invalid_id")
            continue
        cid = str(case_id).strip()
        if cid in seen_ids:
            duplicate_ids.append(cid)
        seen_ids.add(cid)

        if not isinstance(case.get("input"), dict):
            invalid_fields.append(f"{cid}:input_not_object")
        if not isinstance(case.get("must_have_events"), list):
            invalid_fields.append(f"{cid}:must_have_events_not_list")
        if not isinstance(case.get("must_not_have_events"), list):
            invalid_fields.append(f"{cid}:must_not_have_events_not_list")
        if not _is_non_empty_string(case.get("expected_run_status")):
            invalid_fields.append(f"{cid}:expected_run_status_invalid")
        if not _is_non_empty_string(case.get("notes")):
            invalid_fields.append(f"{cid}:notes_invalid")

    checks = {
        "dataset_exists": dataset_path.is_file(),
        "case_count_minimum": len(cases) >= int(args.min_cases),
        "duplicate_ids_none": len(duplicate_ids) == 0,
        "missing_required_keys_none": len(missing_keys) == 0,
        "field_shape_valid": len(invalid_fields) == 0,
    }
    summary = {
        "dataset_path": str(dataset_path),
        "case_count": len(cases),
        "min_cases": int(args.min_cases),
        "duplicate_ids": duplicate_ids,
        "missing_required_keys": missing_keys,
        "invalid_fields": invalid_fields,
        "errors": errors,
        "checks": checks,
        "golden_scenarios": "PASS" if all(checks.values()) and not errors else "FAIL",
    }
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"dataset_path={summary['dataset_path']}")
    print(f"case_count={summary['case_count']}")
    print(f"min_cases={summary['min_cases']}")
    print(f"duplicate_ids={len(summary['duplicate_ids'])}")
    print(f"missing_required_keys={len(summary['missing_required_keys'])}")
    print(f"invalid_fields={len(summary['invalid_fields'])}")
    print(f"summary_json={summary_path}")
    print(f"golden_scenarios={summary['golden_scenarios']}")
    return 0 if summary["golden_scenarios"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
