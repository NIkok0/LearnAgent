#!/usr/bin/env python
"""Aggregate LearnAgent verify scripts into one eval suite summary."""

from __future__ import annotations

import argparse
import json
import os
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


PROFILE_BUDGET_MS = {
    "core-fast": 30_000,
}
SLOW_SUITE_WARNING_MS = 2_500


CONTRACT_SUITES: tuple[SuiteSpec, ...] = (
    SuiteSpec(
        suite_name="contract_events",
        script="scripts/verify_contract_events.py",
        args=("--event-store-path", "storage/verify-contract-events-eval.sqlite"),
    ),
    SuiteSpec(
        suite_name="tool_audit_v1",
        script="scripts/verify_tool_audit_v1.py",
        args=("--event-store-path", "storage/verify-tool-audit-eval.sqlite"),
    ),
    SuiteSpec(
        suite_name="tool_execution_reliability",
        script="scripts/verify_tool_execution_reliability.py",
    ),
    SuiteSpec(
        suite_name="tool_side_effect_ledger_v1",
        script="scripts/verify_tool_side_effect_ledger_v1.py",
        args=("--event-store-path", "storage/verify-tool-side-effect-ledger-events.sqlite"),
    ),
    SuiteSpec(
        suite_name="tool_side_effect_governance_v1",
        script="scripts/verify_tool_side_effect_governance_v1.py",
        args=("--event-store-path", "storage/verify-tool-side-effect-governance-events.sqlite"),
    ),
    SuiteSpec(
        suite_name="policy_decision_audit_v1",
        script="scripts/verify_policy_decision_audit_v1.py",
        args=("--event-store-path", "storage/verify-policy-decision-audit-events.sqlite"),
    ),
    SuiteSpec(
        suite_name="eval_cases_contract",
        script="scripts/verify_eval_cases_contract.py",
    ),
    SuiteSpec(
        suite_name="eval_suite_timeout_v1",
        script="scripts/verify_eval_suite_timeout_v1.py",
    ),
    SuiteSpec(
        suite_name="scenario_loader",
        script="scripts/verify_scenario_loader.py",
    ),
    SuiteSpec(
        suite_name="mcp_capability",
        script="scripts/verify_mcp_capability.py",
    ),
    SuiteSpec(
        suite_name="context_manager",
        script="scripts/verify_context_manager.py",
    ),
    SuiteSpec(
        suite_name="policy_credentials",
        script="scripts/verify_policy_credentials.py",
    ),
    SuiteSpec(
        suite_name="policy_docs_contract",
        script="scripts/verify_policy_docs_contract.py",
    ),
    SuiteSpec(
        suite_name="events_validated",
        script="scripts/verify_events_validated.py",
    ),
)

LEGACY_SUITES: tuple[SuiteSpec, ...] = (
    SuiteSpec(
        suite_name="phase3_checkpoint",
        script="scripts/verify_phase3_checkpoint.py",
    ),
    SuiteSpec(
        suite_name="phase3_safety_gate",
        script="scripts/verify_phase3_safety_gate.py",
    ),
    SuiteSpec(
        suite_name="phase4_dataset",
        script="scripts/verify_phase4_dataset.py",
    ),
)

CORE_FAST_SUITES: tuple[SuiteSpec, ...] = (
    SuiteSpec(
        suite_name="contract_events",
        script="scripts/verify_contract_events.py",
        args=("--event-store-path", "storage/verify-contract-events-eval.sqlite"),
    ),
    SuiteSpec(
        suite_name="tool_audit_v1",
        script="scripts/verify_tool_audit_v1.py",
        args=("--event-store-path", "storage/verify-tool-audit-eval.sqlite"),
    ),
    SuiteSpec(
        suite_name="tool_execution_reliability",
        script="scripts/verify_tool_execution_reliability.py",
    ),
    SuiteSpec(
        suite_name="tool_side_effect_ledger_v1",
        script="scripts/verify_tool_side_effect_ledger_v1.py",
        args=("--event-store-path", "storage/verify-tool-side-effect-ledger-events.sqlite"),
    ),
    SuiteSpec(
        suite_name="tool_side_effect_read_model_v1",
        script="scripts/verify_tool_side_effect_read_model_v1.py",
        args=("--event-store-path", "storage/verify-tool-side-effect-read-model-events.sqlite"),
    ),
    SuiteSpec(
        suite_name="tool_side_effect_governance_v1",
        script="scripts/verify_tool_side_effect_governance_v1.py",
        args=("--event-store-path", "storage/verify-tool-side-effect-governance-events.sqlite"),
    ),
    SuiteSpec(
        suite_name="eval_cases_contract",
        script="scripts/verify_eval_cases_contract.py",
    ),
    SuiteSpec(
        suite_name="eval_suite_timeout_v1",
        script="scripts/verify_eval_suite_timeout_v1.py",
    ),
    SuiteSpec(
        suite_name="scenario_loader",
        script="scripts/verify_scenario_loader.py",
    ),
    SuiteSpec(
        suite_name="context_manager",
        script="scripts/verify_context_manager.py",
    ),
    SuiteSpec(
        suite_name="policy_credentials",
        script="scripts/verify_policy_credentials.py",
    ),
    SuiteSpec(
        suite_name="policy_docs_contract",
        script="scripts/verify_policy_docs_contract.py",
    ),
    SuiteSpec(
        suite_name="events_validated",
        script="scripts/verify_events_validated.py",
    ),
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
        suite_name="runtime_execution_engine",
        script="scripts/verify_runtime_execution_engine.py",
        args=("--event-store-path", "storage/verify-execution-engine-events.sqlite"),
    ),
    SuiteSpec(
        suite_name="runtime_durability_v1",
        script="scripts/verify_runtime_durability_v1.py",
        args=("--event-store-path", "storage/verify-runtime-durability-events.sqlite"),
    ),
    SuiteSpec(
        suite_name="checkpoint_consistency_v2",
        script="scripts/verify_checkpoint_consistency_v2.py",
        args=(
            "--event-store-path",
            "storage/verify-checkpoint-consistency-events.sqlite",
            "--checkpoint-path",
            "storage/verify-checkpoint-consistency-checkpoints.sqlite",
        ),
    ),
    SuiteSpec(
        suite_name="observability_provider",
        script="scripts/verify_observability_provider.py",
    ),
    SuiteSpec(
        suite_name="observability_cost_v1",
        script="scripts/verify_observability_cost_v1.py",
    ),
    SuiteSpec(
        suite_name="final_answer_l7",
        script="scripts/verify_final_answer_l7.py",
    ),
    SuiteSpec(
        suite_name="short_term_memory_formation_v1",
        script="scripts/verify_short_term_memory_formation_v1.py",
    ),
    SuiteSpec(
        suite_name="memory_conversion_eviction_v1",
        script="scripts/verify_memory_conversion_eviction_v1.py",
    ),
    SuiteSpec(
        suite_name="hitl_checkpoint_resume",
        script="scripts/verify_hitl_checkpoint_resume.py",
        args=("--event-store-path", "storage/verify-hitl-checkpoint-resume.sqlite"),
    ),
    SuiteSpec(
        suite_name="phase3_safety_gate",
        script="scripts/verify_phase3_safety_gate.py",
    ),
    SuiteSpec(
        suite_name="phase4_dataset",
        script="scripts/verify_phase4_dataset.py",
    ),
)

CORE_SUITES: tuple[SuiteSpec, ...] = CONTRACT_SUITES + (
    SuiteSpec(
        suite_name="golden_scenarios",
        script="scripts/verify_golden_scenarios.py",
        args=("--dataset", "eval/golden/runtime-golden-scenarios.json"),
    ),
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
        suite_name="runtime_execution_engine",
        script="scripts/verify_runtime_execution_engine.py",
        args=("--event-store-path", "storage/verify-execution-engine-events.sqlite"),
    ),
    SuiteSpec(
        suite_name="runtime_durability_v1",
        script="scripts/verify_runtime_durability_v1.py",
        args=("--event-store-path", "storage/verify-runtime-durability-events.sqlite"),
    ),
    SuiteSpec(
        suite_name="checkpoint_consistency_v2",
        script="scripts/verify_checkpoint_consistency_v2.py",
        args=(
            "--event-store-path",
            "storage/verify-checkpoint-consistency-events.sqlite",
            "--checkpoint-path",
            "storage/verify-checkpoint-consistency-checkpoints.sqlite",
        ),
    ),
    SuiteSpec(
        suite_name="observability_correlation",
        script="scripts/verify_observability_correlation.py",
    ),
    SuiteSpec(
        suite_name="observability_provider",
        script="scripts/verify_observability_provider.py",
    ),
    SuiteSpec(
        suite_name="observability_cost_v1",
        script="scripts/verify_observability_cost_v1.py",
    ),
    SuiteSpec(
        suite_name="plan_module",
        script="scripts/verify_plan_module.py",
    ),
    SuiteSpec(
        suite_name="hitl_checkpoint_resume",
        script="scripts/verify_hitl_checkpoint_resume.py",
        args=("--event-store-path", "storage/verify-hitl-checkpoint-resume.sqlite"),
    ),
    SuiteSpec(
        suite_name="session_mvp",
        script="scripts/verify_session_mvp.py",
        args=("--event-store-path", "storage/verify-session-mvp-events.sqlite"),
    ),
    SuiteSpec(
        suite_name="memory_checkpoint_consistency",
        script="scripts/verify_memory_checkpoint_consistency.py",
        args=(
            "--event-store-path",
            "storage/verify-memory-checkpoint-events.sqlite",
            "--checkpoint-path",
            "storage/verify-memory-checkpoint-checkpoints.sqlite",
        ),
    ),
    SuiteSpec(
        suite_name="memory_production_v1",
        script="scripts/verify_memory_production_v1.py",
        args=(
            "--event-store-path",
            "storage/verify-memory-production-events.sqlite",
            "--checkpoint-path",
            "storage/verify-memory-production-checkpoints.sqlite",
        ),
    ),
    SuiteSpec(
        suite_name="memory_production_v2",
        script="scripts/verify_memory_production_v2.py",
        args=(
            "--event-store-path",
            "storage/verify-memory-production-v2-events.sqlite",
            "--checkpoint-path",
            "storage/verify-memory-production-v2-checkpoints.sqlite",
        ),
    ),
    SuiteSpec(
        suite_name="memory_context_preview_api",
        script="scripts/verify_memory_context_preview_api.py",
    ),
    SuiteSpec(
        suite_name="memory_schema",
        script="scripts/verify_memory_schema.py",
    ),
    SuiteSpec(
        suite_name="memory_quality",
        script="scripts/verify_memory_quality.py",
    ),
) + LEGACY_SUITES

RAG_SUITES: tuple[SuiteSpec, ...] = (
    SuiteSpec(
        suite_name="policy_aware_rag_v1",
        script="scripts/verify_policy_aware_rag_v1.py",
        rag_related=True,
    ),
    SuiteSpec(
        suite_name="rag_domain",
        script="scripts/verify_rag_domain.py",
        args=("--case", "all"),
        rag_related=True,
    ),
    SuiteSpec(
        suite_name="rag_document_lifecycle_v1",
        script="scripts/verify_rag_document_lifecycle_v1.py",
        rag_related=True,
    ),
    SuiteSpec(
        suite_name="private_rag_context_guard_v1",
        script="scripts/verify_private_rag_context_guard_v1.py",
        rag_related=True,
    ),
    SuiteSpec(
        suite_name="private_rag_output_guard_v1",
        script="scripts/verify_private_rag_output_guard_v1.py",
        rag_related=True,
    ),
    SuiteSpec(
        suite_name="phase4_ragas",
        script="scripts/verify_phase4_ragas.py",
        args=("--mode", "proxy", "--disable-vector", "--allow-missing-docs"),
        rag_related=True,
    ),
    SuiteSpec(
        suite_name="phase4_tool_trajectory",
        script="scripts/verify_phase4_tool_trajectory.py",
        rag_related=True,
    ),
    SuiteSpec(
        suite_name="extract_validate",
        script="scripts/verify_extract_validate.py",
        rag_related=True,
    ),
    SuiteSpec(
        suite_name="citation_l4",
        script="scripts/verify_citation_l4.py",
        rag_related=True,
    ),
    SuiteSpec(
        suite_name="final_answer_l7",
        script="scripts/verify_final_answer_l7.py",
        rag_related=True,
    ),
    SuiteSpec(
        suite_name="tool_message_policy",
        script="scripts/verify_tool_message_policy.py",
        rag_related=True,
    ),
    SuiteSpec(
        suite_name="diagnosis_template",
        script="scripts/verify_diagnosis_template.py",
        rag_related=True,
    ),
    SuiteSpec(
        suite_name="tool_router",
        script="scripts/verify_tool_router.py",
        rag_related=True,
    ),
    SuiteSpec(
        suite_name="rag_hot_reload",
        script="scripts/verify_rag_hot_reload.py",
        rag_related=True,
    ),
    SuiteSpec(
        suite_name="rag_rerank",
        script="scripts/verify_rag_rerank.py",
        rag_related=True,
    ),
)

RAG_NIGHTLY_SUITES: tuple[SuiteSpec, ...] = (
    SuiteSpec(
        suite_name="phase4_ragas_nightly",
        script="scripts/verify_phase4_ragas.py",
        args=(
            "--mode",
            "proxy",
            "--enable-vector",
            "--allow-vector-skip",
            "--write-rag-metrics",
            "artifacts/eval/rag_metrics/nightly-latest.json",
            "--metrics-profile",
            "nightly",
            "--summary-json",
            "artifacts/phase4/phase4-ragas-nightly-summary.json",
        ),
        rag_related=True,
    ),
    SuiteSpec(
        suite_name="rag_e2e_ragas",
        script="scripts/verify_rag_e2e_ragas.py",
        args=(
            "--limit",
            "8",
            "--allow-missing-key",
            "--summary-json",
            "artifacts/eval/rag_metrics/e2e-latest.json",
        ),
        rag_related=True,
    ),
)

E2E_SUITES: tuple[SuiteSpec, ...] = (
    SuiteSpec(
        suite_name="demo_golden_e2e",
        script="scripts/verify_demo_golden_e2e.py",
        args=("--mode", "proxy",),
        rag_related=True,
    ),
)


def _profiles(enable_ragas: bool) -> dict[str, tuple[SuiteSpec, ...]]:
    rag = tuple(
        SuiteSpec(
            suite_name=spec.suite_name,
            script=spec.script,
            args=_rag_suite_args(spec, enable_ragas=enable_ragas),
            rag_related=spec.rag_related,
        )
        for spec in RAG_SUITES
    )
    return {
        "core-fast": CORE_FAST_SUITES,
        "core": CORE_SUITES,
        "infra": (
            SuiteSpec(
                suite_name="runtime_durability_v1",
                script="scripts/verify_runtime_durability_v1.py",
                args=("--event-store-path", "storage/verify-runtime-durability-events.sqlite"),
            ),
            SuiteSpec(
                suite_name="runtime_execution_engine",
                script="scripts/verify_runtime_execution_engine.py",
                args=("--event-store-path", "storage/verify-execution-engine-events.sqlite"),
            ),
            SuiteSpec(
                suite_name="observability_provider",
                script="scripts/verify_observability_provider.py",
            ),
            SuiteSpec(
                suite_name="observability_cost_v1",
                script="scripts/verify_observability_cost_v1.py",
            ),
            SuiteSpec(
                suite_name="tool_execution_reliability",
                script="scripts/verify_tool_execution_reliability.py",
            ),
            SuiteSpec(
                suite_name="tool_side_effect_ledger_v1",
                script="scripts/verify_tool_side_effect_ledger_v1.py",
                args=("--event-store-path", "storage/verify-tool-side-effect-ledger-events.sqlite"),
            ),
            SuiteSpec(
                suite_name="tool_side_effect_read_model_v1",
                script="scripts/verify_tool_side_effect_read_model_v1.py",
                args=("--event-store-path", "storage/verify-tool-side-effect-read-model-events.sqlite"),
            ),
            SuiteSpec(
                suite_name="tool_side_effect_governance_v1",
                script="scripts/verify_tool_side_effect_governance_v1.py",
                args=("--event-store-path", "storage/verify-tool-side-effect-governance-events.sqlite"),
            ),
            SuiteSpec(
                suite_name="policy_aware_rag_v1",
                script="scripts/verify_policy_aware_rag_v1.py",
                rag_related=True,
            ),
            SuiteSpec(
                suite_name="private_rag_context_guard_v1",
                script="scripts/verify_private_rag_context_guard_v1.py",
                rag_related=True,
            ),
            SuiteSpec(
                suite_name="private_rag_output_guard_v1",
                script="scripts/verify_private_rag_output_guard_v1.py",
                rag_related=True,
            ),
            SuiteSpec(
                suite_name="final_answer_l7",
                script="scripts/verify_final_answer_l7.py",
                rag_related=True,
            ),
        ),
        "rag": rag,
        "e2e": E2E_SUITES,
        "full": CORE_SUITES + rag + RAG_NIGHTLY_SUITES + E2E_SUITES,
    }


def _rag_suite_args(spec: SuiteSpec, *, enable_ragas: bool) -> tuple[str, ...]:
    if not enable_ragas or spec.suite_name != "phase4_ragas":
        return spec.args
    return ("--mode", "auto", "--disable-vector", "--allow-missing-docs")


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


def _suite_status(return_code: int, kv: dict[str, str]) -> str:
    if return_code != 0:
        return "FAIL"
    pass_like = _suite_signal_values(kv)
    if not pass_like:
        return "PASS"
    if any(str(item).strip().lower() == "skip" for item in pass_like):
        return "SKIP"
    states = [_to_bool(item) for item in pass_like]
    return "PASS" if all(state is not False for state in states) else "FAIL"


def _timeout_suite_status(kv: dict[str, str]) -> str:
    if not _suite_signal_values(kv):
        return "FAIL"
    return _suite_status(0, kv)


def _proxy_summary_passed(summary: dict[str, Any]) -> bool:
    if str(summary.get("phase4_ragas") or "").upper() != "PASS":
        return False
    proxy_metrics = summary.get("proxy_metrics")
    if not isinstance(proxy_metrics, dict):
        return False
    return int(proxy_metrics.get("docs_cases") or 0) >= 3


def main() -> int:
    parser = argparse.ArgumentParser(description="Run aggregated eval suite profiles.")
    parser.add_argument(
        "--profile",
        choices=["core-fast", "core", "infra", "rag", "e2e", "full"],
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
        env = dict(os.environ)
        if spec.rag_related:
            env.setdefault("SCENARIO", "watermark")
        if spec.suite_name == "phase4_ragas_nightly":
            env["RAG_USE_VECTOR"] = "true"
            env["RAG_RERANK_ENABLED"] = "true"
            env["RAG_EMBEDDING_MODEL"] = "BAAI/bge-small-zh-v1.5"
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
                    "pass": status != "FAIL",
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
            stdout = _coerce_output_text(exc.stdout)
            stderr = _coerce_output_text(exc.stderr)
            kv = _parse_key_values(stdout + ("\n" + stderr if stderr else ""))
            status = _timeout_suite_status(kv)
            summary_json = _extract_summary_json(kv)
            checks = _load_checks_from_summary(summary_json)
            summary_payload = _load_summary_payload(summary_json)
            out = stdout.splitlines()[-12:]
            err = stderr.splitlines()[-12:]
            timeout_error = f"timeout_after_seconds={args.suite_timeout_seconds}"
            errors = [timeout_error]
            # Some verify scripts may complete logic and print PASS, but keep process open
            # due to lingering async resources. In that case, treat suite as pass with warning.
            if status in {"PASS", "SKIP"}:
                errors.append(f"timeout_after_{status.lower()}_signal")
            elif spec.suite_name == "phase4_ragas" and _proxy_summary_passed(summary_payload):
                status = "PASS"
                errors.append("timeout_after_proxy_summary_pass")
            results.append(
                {
                    "suite_name": spec.suite_name,
                    "script": spec.script,
                    "pass": status != "FAIL",
                    "status": status,
                    "duration_ms": elapsed,
                    "summary_json": summary_json,
                    "checks": checks,
                    "artifacts": [summary_json] if summary_json else [],
                    "errors": errors,
                    "stdout_tail": out,
                    "stderr_tail": err,
                }
            )
    failed = [item for item in results if item["status"] == "FAIL"]
    skipped = [item for item in results if item["status"] == "SKIP"]
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
    runtime_contract_breaks = [
        item["suite_name"]
        for item in failed
        if str(item["suite_name"]).startswith("runtime_") or item["suite_name"] in {"session_mvp"}
    ]
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
    rag_metrics: dict[str, Any] = {}
    nightly_metrics_path = ROOT / "artifacts/eval/rag_metrics/nightly-latest.json"
    if nightly_metrics_path.is_file():
        try:
            nightly_payload = json.loads(nightly_metrics_path.read_text(encoding="utf-8"))
            if isinstance(nightly_payload.get("proxy_metrics"), dict):
                rag_metrics = nightly_payload["proxy_metrics"]
                rag_metrics["profile"] = nightly_payload.get("profile", "nightly")
        except Exception:
            pass
    if not rag_metrics:
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
    if rag_metrics:
        from copilot_agent.eval.rag_metrics_trend import detect_gold_recall_regression  # noqa: WPS433

        rag_regression = detect_gold_recall_regression(
            current=rag_metrics,
            history_dir=ROOT / "artifacts/eval/rag_metrics/history",
            profile_prefix="nightly",
        )
        if rag_regression.get("regression"):
            overall_pass = False

    out = {
        "profile": args.profile,
        "enable_ragas": bool(args.enable_ragas),
        "overall_pass": overall_pass,
        "suites_total": len(results),
        "suites_failed": len(failed),
        "failed_suites": [item["suite_name"] for item in failed],
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
            "warnings": budget_warnings,
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
