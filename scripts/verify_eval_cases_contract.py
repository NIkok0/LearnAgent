#!/usr/bin/env python
"""Validate eval case datasets and golden scenario events against RuntimeEvent contract."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.contracts.base import RuntimeEvent  # noqa: E402
from copilot_agent.contracts.validate import validate_event_kinds, validate_stored_event  # noqa: E402
from copilot_agent.tools.audit import build_tool_end_payload, build_tool_start_payload  # noqa: E402

PHASE4_CASE_KEYS = {
    "id",
    "question",
    "category",
    "expected_tools",
    "required_sources",
    "forbidden_tools",
    "expect_blocked",
}


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_phase4_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    shape_errors: list[str] = []
    for case in cases:
        case_id = str(case.get("id", ""))
        missing = PHASE4_CASE_KEYS - set(case.keys())
        if missing:
            shape_errors.append(f"{case_id}: missing {sorted(missing)}")
            continue
        try:
            if case.get("expect_blocked"):
                validate_stored_event(
                    kind="approval_required",
                    payload={"required": True, "reason": "dangerous_tool", "message": "blocked"},
                )
            for tool in case.get("expected_tools") or []:
                if tool == "search_docs":
                    validate_stored_event(
                        kind="retrieval_completed",
                        payload={
                            "query": case.get("question", ""),
                            "sources": [],
                            "source_count": 0,
                            "excerpt_chars": 0,
                        },
                    )
                    validate_stored_event(
                        kind="tool_start",
                        payload=build_tool_start_payload(
                            name="search_docs",
                            call_id=f"{case_id}-search",
                            category="memory",
                            risk_level="low",
                            requires_approval=False,
                            arguments={"query": case.get("question", "")},
                        ),
                    )
                elif tool in {"http_get", "http_post"}:
                    validate_stored_event(
                        kind="tool_start",
                        payload=build_tool_start_payload(
                            name=tool,
                            call_id=f"{case_id}-{tool}",
                            category="http",
                            risk_level="medium",
                            requires_approval=False,
                            arguments={"path": "/actuator/health"},
                        ),
                    )
        except Exception as exc:
            shape_errors.append(f"{case_id}: model_validate: {exc}")
    return {
        "case_count": len(cases),
        "shape_errors": shape_errors,
        "shape_ok": not shape_errors,
        "model_validate_ok": not shape_errors,
    }


def _validate_golden_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    errors: list[str] = []
    for case in cases:
        case_id = str(case.get("id", ""))
        for kind in case.get("must_have_events") or []:
            kind_text = str(kind)
            result = validate_event_kinds([kind_text])
            if not result["kinds_ok"]:
                errors.append(f"{case_id}: unknown must_have_event {kind_text}")
                continue
            # Golden cases only assert kind registration; empty payloads skip shape validation.
            if kind_text in {"tool_start", "tool_end", "retrieval_completed", "token"}:
                continue
            try:
                validate_stored_event(kind=kind_text, payload={})
            except Exception as exc:
                errors.append(f"{case_id}: {kind_text}: {exc}")
        for kind in case.get("must_not_have_events") or []:
            result = validate_event_kinds([str(kind)])
            if not result["kinds_ok"]:
                errors.append(f"{case_id}: unknown must_not_have_event {kind}")
    return {
        "case_count": len(cases),
        "golden_event_errors": errors,
        "golden_events_ok": not errors,
    }


def verify(phase4_path: Path, golden_path: Path) -> dict[str, Any]:
    phase4_payload = _load_json(phase4_path)
    golden_payload = _load_json(golden_path)
    phase4_cases = phase4_payload.get("cases") if isinstance(phase4_payload, dict) else []
    golden_cases = golden_payload.get("cases") if isinstance(golden_payload, dict) else []
    if not isinstance(phase4_cases, list):
        phase4_cases = []
    if not isinstance(golden_cases, list):
        golden_cases = []

    phase4_result = _validate_phase4_cases([c for c in phase4_cases if isinstance(c, dict)])
    golden_result = _validate_golden_cases([c for c in golden_cases if isinstance(c, dict)])

    passed = (
        phase4_result["shape_ok"]
        and phase4_result["model_validate_ok"]
        and golden_result["golden_events_ok"]
        and len(phase4_cases) >= 14
        and len(golden_cases) >= 8
    )
    return {
        "phase4_path": str(phase4_path),
        "golden_path": str(golden_path),
        "phase4": phase4_result,
        "golden": golden_result,
        "checks": {
            "phase4_shape_ok": phase4_result["shape_ok"],
            "phase4_model_validate_ok": phase4_result["model_validate_ok"],
            "golden_events_ok": golden_result["golden_events_ok"],
            "phase4_case_count_ok": len(phase4_cases) >= 14,
            "golden_case_count_ok": len(golden_cases) >= 8,
            "contract_schema_ok": passed,
        },
        "eval_cases_contract": "PASS" if passed else "FAIL",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify eval datasets against RuntimeEvent contract.")
    parser.add_argument(
        "--phase4-cases",
        default=str(ROOT / "eval/phase4-eval-cases.json"),
    )
    parser.add_argument(
        "--golden-scenarios",
        default=str(ROOT / "eval/golden/runtime-golden-scenarios.json"),
    )
    parser.add_argument(
        "--summary-json",
        default=str(ROOT / "artifacts/eval/eval-cases-contract-summary.json"),
    )
    args = parser.parse_args()

    summary = verify(Path(args.phase4_cases).resolve(), Path(args.golden_scenarios).resolve())
    summary_path = Path(args.summary_json).resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    checks = summary.get("checks") or {}
    print(f"contract_schema_ok={checks.get('contract_schema_ok')}")
    print(f"phase4_cases={summary['phase4']['case_count']}")
    print(f"golden_cases={summary['golden']['case_count']}")
    print(f"summary_json={summary_path}")
    print(f"eval_cases_contract={summary['eval_cases_contract']}")
    return 0 if summary["eval_cases_contract"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
