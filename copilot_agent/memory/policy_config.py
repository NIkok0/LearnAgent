from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import Any


@dataclass(frozen=True)
class MemoryPolicyConfig:
    enabled: bool = True
    thread_summary_max_runs: int = 5
    thread_summary_max_chars: int = 1200
    episodic_recall_top_k: int = 2
    include_failed_runs: bool = False
    include_cancelled_runs: bool = False
    key_output_max_chars: int = 800
    conflict_jaccard_threshold: float = 0.15
    checkpoint_compact_enabled: bool = True
    checkpoint_compact_message_threshold: int = 40
    checkpoint_compact_keep_recent_turns: int = 6
    checkpoint_compact_summary_max_chars: int = 2000
    long_term_enabled: bool = True
    long_term_recall_top_k: int = 3
    long_term_recall_min_score: float = 0.2
    long_term_importance_min: float = 0.5
    long_term_max_items_per_user: int = 200
    long_term_protected_importance: float = 0.9
    long_term_dedup_jaccard_threshold: float = 0.25
    long_term_conflict_jaccard_threshold: float = 0.3
    long_term_time_decay_half_life_days: float = 14.0
    long_term_keyword_weight: float = 0.5
    long_term_time_weight: float = 0.25
    long_term_importance_weight: float = 0.25
    inject_dedupe_system_prompt: bool = True
    inject_dedupe_memory_messages: bool = True
    long_term_use_vector: bool = False
    long_term_embedding_deterministic: bool = False
    long_term_vector_weight: float = 0.35
    long_term_vector_min_score: float = 0.55
    hyde_enabled: bool = True
    hyde_mode: str = "rule"
    llm_extract_enabled: bool = True
    llm_confirm_threshold: float = 0.7
    long_term_inject_min_score: float = 0.35
    long_term_max_per_type: int = 2
    memory_type_boost_fact: float = 0.08
    memory_type_boost_preference: float = 0.03
    memory_type_boost_behavior: float = 0.03
    memory_type_boost_task_summary: float = 0.12
    thread_summary_budget_chars: int = 260
    episodic_budget_chars: int = 420
    long_term_budget_chars: int = 520
    write_gate_enabled: bool = True
    write_min_confidence: float = 0.7
    write_require_reusable: bool = True
    recall_confidence_weight: float = 0.15
    recall_access_weight: float = 0.1
    access_decay_half_life_days: float = 30.0
    contradiction_pending_enabled: bool = True
    contradiction_pending_threshold: float = 0.3


def apply_memory_policy_overlay(
    base: MemoryPolicyConfig,
    overlay: dict[str, Any] | None,
) -> MemoryPolicyConfig:
    if not overlay:
        return base
    valid = {item.name for item in fields(MemoryPolicyConfig)}
    updates: dict[str, Any] = {}
    for key, value in overlay.items():
        if key.startswith("#") or key not in valid:
            continue
        updates[key] = value
    if not updates:
        return base
    return replace(base, **updates)


def memory_policy_from_settings(settings: Any) -> MemoryPolicyConfig:
    return MemoryPolicyConfig(
        enabled=bool(getattr(settings, "memory_enabled", True)),
        thread_summary_max_runs=int(getattr(settings, "memory_thread_summary_max_runs", 5)),
        thread_summary_max_chars=int(getattr(settings, "memory_thread_summary_max_chars", 1200)),
        episodic_recall_top_k=int(getattr(settings, "memory_episodic_recall_top_k", 2)),
        include_failed_runs=bool(getattr(settings, "memory_include_failed_runs", False)),
        include_cancelled_runs=bool(getattr(settings, "memory_include_cancelled_runs", False)),
        key_output_max_chars=int(getattr(settings, "memory_key_output_max_chars", 800)),
        checkpoint_compact_enabled=bool(getattr(settings, "memory_checkpoint_compact_enabled", True)),
        checkpoint_compact_message_threshold=int(
            getattr(settings, "memory_checkpoint_compact_message_threshold", 40)
        ),
        checkpoint_compact_keep_recent_turns=int(getattr(settings, "memory_checkpoint_compact_keep_recent_turns", 6)),
        checkpoint_compact_summary_max_chars=int(
            getattr(settings, "memory_checkpoint_compact_summary_max_chars", 2000)
        ),
        long_term_enabled=bool(getattr(settings, "memory_long_term_enabled", True)),
        long_term_recall_top_k=int(getattr(settings, "memory_long_term_recall_top_k", 3)),
        long_term_recall_min_score=float(getattr(settings, "memory_long_term_recall_min_score", 0.2)),
        long_term_importance_min=float(getattr(settings, "memory_long_term_importance_min", 0.5)),
        long_term_max_items_per_user=int(getattr(settings, "memory_long_term_max_items_per_user", 200)),
        long_term_protected_importance=float(getattr(settings, "memory_long_term_protected_importance", 0.9)),
        long_term_dedup_jaccard_threshold=float(
            getattr(settings, "memory_long_term_dedup_jaccard_threshold", 0.25)
        ),
        long_term_conflict_jaccard_threshold=float(
            getattr(settings, "memory_long_term_conflict_jaccard_threshold", 0.3)
        ),
        long_term_time_decay_half_life_days=float(
            getattr(settings, "memory_long_term_time_decay_half_life_days", 14.0)
        ),
        long_term_keyword_weight=float(getattr(settings, "memory_long_term_keyword_weight", 0.5)),
        long_term_time_weight=float(getattr(settings, "memory_long_term_time_weight", 0.25)),
        long_term_importance_weight=float(getattr(settings, "memory_long_term_importance_weight", 0.25)),
        inject_dedupe_system_prompt=bool(getattr(settings, "memory_inject_dedupe_system_prompt", True)),
        inject_dedupe_memory_messages=bool(getattr(settings, "memory_inject_dedupe_memory_messages", True)),
        long_term_use_vector=bool(getattr(settings, "memory_long_term_use_vector", False)),
        long_term_embedding_deterministic=bool(getattr(settings, "memory_embedding_deterministic", False)),
        long_term_vector_weight=float(getattr(settings, "memory_long_term_vector_weight", 0.35)),
        long_term_vector_min_score=float(getattr(settings, "memory_long_term_vector_min_score", 0.55)),
        hyde_enabled=bool(getattr(settings, "memory_hyde_enabled", True)),
        hyde_mode=str(getattr(settings, "memory_hyde_mode", "rule")),
        llm_extract_enabled=bool(getattr(settings, "memory_llm_extract_enabled", True)),
        llm_confirm_threshold=float(getattr(settings, "memory_llm_confirm_threshold", 0.7)),
        long_term_inject_min_score=float(getattr(settings, "memory_long_term_inject_min_score", 0.35)),
        long_term_max_per_type=int(getattr(settings, "memory_long_term_max_per_type", 2)),
        memory_type_boost_fact=float(getattr(settings, "memory_type_boost_fact", 0.08)),
        memory_type_boost_preference=float(getattr(settings, "memory_type_boost_preference", 0.03)),
        memory_type_boost_behavior=float(getattr(settings, "memory_type_boost_behavior", 0.03)),
        memory_type_boost_task_summary=float(getattr(settings, "memory_type_boost_task_summary", 0.12)),
        thread_summary_budget_chars=int(getattr(settings, "memory_thread_summary_budget_chars", 260)),
        episodic_budget_chars=int(getattr(settings, "memory_episodic_budget_chars", 420)),
        long_term_budget_chars=int(getattr(settings, "memory_long_term_budget_chars", 520)),
        write_gate_enabled=bool(getattr(settings, "memory_write_gate_enabled", True)),
        write_min_confidence=float(getattr(settings, "memory_write_min_confidence", 0.7)),
        write_require_reusable=bool(getattr(settings, "memory_write_require_reusable", True)),
        recall_confidence_weight=float(getattr(settings, "memory_recall_confidence_weight", 0.15)),
        recall_access_weight=float(getattr(settings, "memory_recall_access_weight", 0.1)),
        access_decay_half_life_days=float(getattr(settings, "memory_access_decay_half_life_days", 30.0)),
        contradiction_pending_enabled=bool(getattr(settings, "memory_contradiction_pending_enabled", True)),
        contradiction_pending_threshold=float(getattr(settings, "memory_contradiction_pending_threshold", 0.3)),
    )


__all__ = ["MemoryPolicyConfig", "apply_memory_policy_overlay", "memory_policy_from_settings"]
