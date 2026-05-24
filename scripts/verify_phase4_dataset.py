#!/usr/bin/env python
"""Phase 4 Step 1: verify structured eval dataset integrity and policy coverage."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.scenario import load_scenario  # noqa: E402

CASE_ID_PATTERN = re.compile(r"^P4-\d{3}$")
ALLOWED_CATEGORIES = {"docs", "api", "safety"}
ALLOWED_QUESTION_TYPES = {"factual", "procedural", "troubleshooting", "api_lookup"}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _watermark_dangerous_forbidden_tool() -> str:
    scenario = load_scenario("watermark")
    if scenario.policy.dangerous_paths:
        return f"http_post:{scenario.policy.dangerous_paths[0]}"
    if scenario.router_rules and scenario.router_rules.dangerous_job_path:
        return f"http_post:{scenario.router_rules.dangerous_job_path}"
    raise RuntimeError("watermark scenario must declare a dangerous path")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Phase 4 eval dataset and output summary.")
    parser.add_argument(
        "--dataset",
        default=str(ROOT / "eval/phase4-eval-cases.json"),
        help="Path to Phase 4 structured eval dataset JSON.",
    )
    parser.add_argument(
        "--summary-json",
        default=str(ROOT / "artifacts/phase4/phase4-dataset-summary.json"),
        help="Path to write structured verification summary JSON.",
    )
    args = parser.parse_args()

    dataset_path = Path(args.dataset).resolve()
    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    try:
        payload = json.loads(dataset_path.read_text(encoding="utf-8"))
    except Exception as exc:
        errors.append(f"dataset_read_error: {exc}")
        payload = {}

    cases = payload.get("cases", []) if isinstance(payload, dict) else []
    if not isinstance(cases, list) or not cases:
        errors.append("cases_must_be_non_empty_list")
        cases = []

    ids: list[str] = []
    category_counts: Counter[str] = Counter()
    question_type_counts: Counter[str] = Counter()
    blocked_cases = 0
    dangerous_forbidden_cases = 0
    docs_with_gold_chunks = 0

    for idx, case in enumerate(cases, start=1):
        if not isinstance(case, dict):
            errors.append(f"case_{idx}_must_be_object")
            continue
        case_id = str(case.get("id", ""))
        ids.append(case_id)
        if not CASE_ID_PATTERN.match(case_id):
            errors.append(f"{case_id or f'case_{idx}'}: invalid_id_format")
        question = str(case.get("question", "")).strip()
        if not question:
            errors.append(f"{case_id or f'case_{idx}'}: empty_question")

        category = str(case.get("category", "")).strip()
        category_counts[category] += 1
        if category not in ALLOWED_CATEGORIES:
            errors.append(f"{case_id or f'case_{idx}'}: invalid_category={category}")

        expected_tools = _as_list(case.get("expected_tools"))
        forbidden_tools = _as_list(case.get("forbidden_tools"))
        required_sources = _as_list(case.get("required_sources"))
        expect_blocked = bool(case.get("expect_blocked", False))

        if category == "docs" and "search_docs" not in expected_tools:
            errors.append(f"{case_id}: docs_case_must_include_search_docs")
        if category == "docs" and not required_sources:
            errors.append(f"{case_id}: docs_case_must_have_required_sources")
        if category == "docs":
            question_type = str(case.get("question_type", "")).strip()
            if not question_type:
                errors.append(f"{case_id}: docs_case_must_have_question_type")
            elif question_type not in ALLOWED_QUESTION_TYPES:
                errors.append(f"{case_id}: invalid_question_type={question_type}")
            else:
                question_type_counts[question_type] += 1
            gold_chunks = _as_list(case.get("gold_chunks"))
            if not gold_chunks:
                errors.append(f"{case_id}: docs_case_must_have_gold_chunks")
            else:
                docs_with_gold_chunks += 1
                for gidx, item in enumerate(gold_chunks):
                    if not isinstance(item, dict):
                        errors.append(f"{case_id}: gold_chunks[{gidx}]_must_be_object")
                        continue
                    if not str(item.get("source", "")).strip():
                        errors.append(f"{case_id}: gold_chunks[{gidx}].source_required")
                    try:
                        int(item.get("start_line", 0))
                    except (TypeError, ValueError):
                        errors.append(f"{case_id}: gold_chunks[{gidx}].start_line_must_be_int")
            must_not = _as_list(case.get("must_not_sources"))
            for midx, item in enumerate(must_not):
                if not isinstance(item, str) or not str(item).strip():
                    errors.append(f"{case_id}: must_not_sources[{midx}]_must_be_non_empty_string")
        if category == "api" and not any(str(t).startswith("http_") for t in expected_tools):
            errors.append(f"{case_id}: api_case_must_include_http_tool")
        if category == "safety" and (not expect_blocked and not expected_tools):
            errors.append(f"{case_id}: safety_case_requires_expected_tools_when_not_blocked")

        if expect_blocked:
            blocked_cases += 1
        has_dangerous_forbidden = any(str(t) == _watermark_dangerous_forbidden_tool() for t in forbidden_tools)
        if has_dangerous_forbidden:
            dangerous_forbidden_cases += 1
            if not expect_blocked:
                errors.append(f"{case_id}: dangerous_forbidden_must_be_blocked")

    duplicated = sorted({x for x in ids if x and ids.count(x) > 1})
    if duplicated:
        errors.append(f"duplicate_ids={duplicated}")

    docs_cases = category_counts.get("docs", 0)
    if docs_cases:
        factual_pct = question_type_counts.get("factual", 0) / docs_cases
        procedural_pct = question_type_counts.get("procedural", 0) / docs_cases
        troubleshooting_pct = question_type_counts.get("troubleshooting", 0) / docs_cases
        api_lookup_pct = question_type_counts.get("api_lookup", 0) / docs_cases
        if factual_pct < 0.3 or factual_pct > 0.55:
            errors.append(f"question_type_factual_out_of_range={factual_pct:.2f}")
        if procedural_pct < 0.1 or procedural_pct > 0.4:
            errors.append(f"question_type_procedural_out_of_range={procedural_pct:.2f}")
        if troubleshooting_pct < 0.05 or troubleshooting_pct > 0.3:
            errors.append(f"question_type_troubleshooting_out_of_range={troubleshooting_pct:.2f}")
        if api_lookup_pct < 0.05 or api_lookup_pct > 0.35:
            errors.append(f"question_type_api_lookup_out_of_range={api_lookup_pct:.2f}")

    summary = {
        "dataset_path": str(dataset_path),
        "total_cases": len(cases),
        "category_counts": dict(category_counts),
        "question_type_counts": dict(question_type_counts),
        "docs_with_gold_chunks": docs_with_gold_chunks,
        "blocked_cases": blocked_cases,
        "dangerous_forbidden_cases": dangerous_forbidden_cases,
        "errors": errors,
        "phase4_dataset": "PASS" if not errors else "FAIL",
    }
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"dataset_path={summary['dataset_path']}")
    print(f"total_cases={summary['total_cases']}")
    print(f"category_counts={json.dumps(summary['category_counts'], ensure_ascii=False)}")
    print(f"question_type_counts={json.dumps(summary.get('question_type_counts', {}), ensure_ascii=False)}")
    print(f"docs_with_gold_chunks={summary.get('docs_with_gold_chunks', 0)}")
    print(f"blocked_cases={summary['blocked_cases']}")
    print(f"dangerous_forbidden_cases={summary['dangerous_forbidden_cases']}")
    print(f"errors_count={len(errors)}")
    print(f"summary_json={summary_path}")
    if errors:
        for item in errors:
            print(f"error={item}")
        print("phase4_dataset=FAIL")
        return 1

    print("phase4_dataset=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
