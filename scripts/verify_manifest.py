#!/usr/bin/env python
"""Suite manifest and profile definitions for LearnAgent verify scripts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SuiteSpec:
    suite_name: str
    script: str
    args: tuple[str, ...] = ()
    rag_related: bool = False
    status_key: str | None = None


PROFILE_BUDGET_MS = {
    "core-fast": 35_000,
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
        suite_name="tool_governance_domain",
        script="scripts/verify_tool_governance_domain.py",
        args=("--case", "all"),
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
        suite_name="scripts_manifest",
        script="scripts/verify_scripts_manifest.py",
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
        suite_name="skills_v1",
        script="scripts/verify_skills_v1.py",
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
        suite_name="retrieval_gate_v1",
        script="scripts/verify_retrieval_gate_v1.py",
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
        suite_name="dependency_compat",
        script="scripts/verify_dependency_compat.py",
    ),
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
        suite_name="eval_cases_contract",
        script="scripts/verify_eval_cases_contract.py",
    ),
    SuiteSpec(
        suite_name="scripts_manifest",
        script="scripts/verify_scripts_manifest.py",
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
        suite_name="context_providers_v1",
        script="scripts/verify_context_providers_v1.py",
    ),
    SuiteSpec(
        suite_name="retrieval_gate_v1",
        script="scripts/verify_retrieval_gate_v1.py",
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
        suite_name="runtime_event_store_gate",
        script="scripts/verify_runtime_domain.py",
        args=("--case", "event_store"),
        status_key="runtime_domain",
    ),
    SuiteSpec(
        suite_name="runtime_timeline_gate",
        script="scripts/verify_runtime_domain.py",
        args=("--case", "timeline"),
        status_key="runtime_domain",
    ),
)

CORE_SUITES: tuple[SuiteSpec, ...] = CONTRACT_SUITES + (
    SuiteSpec(
        suite_name="golden_scenarios",
        script="scripts/verify_golden_scenarios.py",
        args=("--dataset", "eval/golden/runtime-golden-scenarios.json"),
    ),
    SuiteSpec(
        suite_name="runtime_domain",
        script="scripts/verify_runtime_domain.py",
        args=("--case", "all"),
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
        suite_name="observability_domain",
        script="scripts/verify_observability_domain.py",
        args=("--case", "all"),
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
        suite_name="memory_domain",
        script="scripts/verify_memory_domain.py",
        args=("--case", "all"),
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

MANUAL_SUITES: tuple[SuiteSpec, ...] = (
    SuiteSpec(
        suite_name="live_llm_e2e_acceptance",
        script="scripts/verify_live_llm_e2e_acceptance.py",
    ),
    SuiteSpec(
        suite_name="deepseek_provider",
        script="scripts/verify_deepseek_provider.py",
    ),
)

EXPORT_SCRIPTS: tuple[str, ...] = (
    "scripts/export_memory_deletion_proof.py",
    "scripts/export_rag_deletion_proof.py",
    "scripts/export_run_debug_bundle.py",
    "scripts/build_index.py",
    "scripts/smoke_chat_api.py",
)


def profiles(enable_ragas: bool) -> dict[str, tuple[SuiteSpec, ...]]:
    rag = tuple(
        SuiteSpec(
            suite_name=spec.suite_name,
            script=spec.script,
            args=_rag_suite_args(spec, enable_ragas=enable_ragas),
            rag_related=spec.rag_related,
            status_key=spec.status_key,
        )
        for spec in RAG_SUITES
    )
    return _with_default_status_keys({
        "core-fast": CORE_FAST_SUITES,
        "core": CORE_SUITES,
        "infra": (
            SuiteSpec(
                suite_name="runtime_domain",
                script="scripts/verify_runtime_domain.py",
                args=("--case", "all"),
            ),
            SuiteSpec(
                suite_name="observability_domain",
                script="scripts/verify_observability_domain.py",
                args=("--case", "all"),
            ),
            SuiteSpec(
                suite_name="tool_execution_reliability",
                script="scripts/verify_tool_execution_reliability.py",
            ),
            SuiteSpec(
                suite_name="tool_governance_domain",
                script="scripts/verify_tool_governance_domain.py",
                args=("--case", "all"),
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
    })


def _rag_suite_args(spec: SuiteSpec, *, enable_ragas: bool) -> tuple[str, ...]:
    if not enable_ragas or spec.suite_name != "phase4_ragas":
        return spec.args
    return ("--mode", "auto", "--disable-vector", "--allow-missing-docs")


def _with_default_status_keys(profiles_map: dict[str, tuple[SuiteSpec, ...]]) -> dict[str, tuple[SuiteSpec, ...]]:
    return {
        name: tuple(_with_status_key(spec) for spec in specs)
        for name, specs in profiles_map.items()
    }


def _with_status_key(spec: SuiteSpec) -> SuiteSpec:
    if spec.status_key:
        return spec
    return SuiteSpec(
        suite_name=spec.suite_name,
        script=spec.script,
        args=spec.args,
        rag_related=spec.rag_related,
        status_key=_default_status_key(spec.suite_name),
    )


def _default_status_key(suite_name: str) -> str | None:
    if suite_name.endswith("_domain"):
        return suite_name
    explicit = {
        "checkpoint_consistency_v2": "checkpoint_consistency_v2",
        "citation_l4": "citation_l4",
        "context_manager": "verify_context_manager",
        "context_providers_v1": "verify_context_providers_v1",
        "contract_events": "contract_events",
        "demo_golden_e2e": "demo_golden_e2e",
        "dependency_compat": "dependency_compat",
        "diagnosis_template": "diagnosis_template",
        "eval_cases_contract": "eval_cases_contract",
        "eval_suite_timeout_v1": "eval_suite_timeout_v1",
        "events_validated": "events_validated",
        "extract_validate": "extract_validate",
        "final_answer_l7": "final_answer_l7",
        "golden_scenarios": "golden_scenarios",
        "hitl_checkpoint_resume": "hitl_checkpoint_resume",
        "memory_checkpoint_consistency": "memory_checkpoint_consistency",
        "memory_context_preview_api": "memory_context_preview_api",
        "memory_production_v1": "memory_production_v1",
        "memory_production_v2": "memory_production_v2",
        "memory_schema": "memory_schema",
        "mcp_capability": "mcp_capability",
        "phase3_checkpoint": "phase3_step4",
        "phase3_safety_gate": "phase3_safety_gate",
        "phase4_dataset": "phase4_dataset",
        "phase4_ragas": "phase4_ragas",
        "phase4_ragas_nightly": "phase4_ragas",
        "phase4_tool_trajectory": "phase4_tool_trajectory",
        "plan_module": "plan_module",
        "policy_aware_rag_v1": "policy_aware_rag_v1",
        "policy_credentials": "verify_policy_credentials",
        "policy_decision_audit_v1": "policy_decision_audit_v1",
        "policy_docs_contract": "policy_docs_contract",
        "private_rag_context_guard_v1": "private_rag_context_guard_v1",
        "private_rag_output_guard_v1": "private_rag_output_guard_v1",
        "rag_document_lifecycle_v1": "rag_document_lifecycle_v1",
        "rag_e2e_ragas": "rag_e2e_ragas",
        "rag_hot_reload": "rag_hot_reload",
        "rag_rerank": "verify_rag_rerank",
        "retrieval_gate_v1": "retrieval_gate_v1",
        "runtime_checkpoint_link": "runtime_checkpoint_link",
        "scenario_loader": "scenario_loader",
        "scripts_manifest": "verify_scripts_manifest",
        "session_mvp": "session_mvp",
        "skills_v1": "skills_v1",
        "thread_archive_api": "thread_archive_api",
        "thread_lifecycle_cleaner": "thread_lifecycle_cleaner",
        "tool_audit_v1": "tool_audit_v1",
        "tool_execution_reliability": "tool_execution_reliability",
        "tool_message_policy": "tool_message_policy",
        "tool_router": "verify_tool_router",
    }
    return explicit.get(suite_name)



def all_suite_specs(*, enable_ragas: bool = False) -> tuple[SuiteSpec, ...]:
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    out: list[SuiteSpec] = []
    for suite_group in profiles(enable_ragas=enable_ragas).values():
        for spec in suite_group:
            key = (spec.suite_name, spec.script, spec.args)
            if key in seen:
                continue
            seen.add(key)
            out.append(spec)
    return tuple(out)


def all_manifest_scripts(*, enable_ragas: bool = False) -> tuple[str, ...]:
    scripts = [spec.script for spec in all_suite_specs(enable_ragas=enable_ragas)]
    scripts.extend(spec.script for spec in MANUAL_SUITES)
    scripts.extend(EXPORT_SCRIPTS)
    return tuple(dict.fromkeys(scripts))


def suite_categories(*, enable_ragas: bool = False) -> dict[str, str]:
    categories: dict[str, str] = {}
    for spec in CONTRACT_SUITES + CORE_FAST_SUITES + CORE_SUITES:
        categories.setdefault(spec.suite_name, "core")
    for spec in RAG_SUITES + RAG_NIGHTLY_SUITES:
        categories[spec.suite_name] = "rag"
    for spec in E2E_SUITES:
        categories[spec.suite_name] = "e2e"
    for spec in LEGACY_SUITES:
        categories[spec.suite_name] = "legacy"
    for spec in MANUAL_SUITES:
        categories[spec.suite_name] = "manual"
    for spec in all_suite_specs(enable_ragas=enable_ragas):
        categories.setdefault(spec.suite_name, "domain" if spec.suite_name.endswith("_domain") else "core")
    return categories


def profile_names() -> tuple[str, ...]:
    return tuple(profiles(enable_ragas=False))
