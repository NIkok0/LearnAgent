from __future__ import annotations

import re
from dataclasses import dataclass, field, fields, replace
from typing import Any

EPISODIC_MEMORY_PREFIX = "[EpisodicMemory]"


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
    # Structured long-term memory (Phase 1 production patterns).
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


@dataclass
class EpisodicInjectBundle:
    thread_summary: dict[str, Any] | None
    recalled_runs: list[dict[str, Any]]
    dropped_conflicts: list[dict[str, Any]] = field(default_factory=list)
    recalled_long_term: list[dict[str, Any]] = field(default_factory=list)
    inject_preview: str = ""
    budget_applied: dict[str, Any] = field(default_factory=dict)
    sources: dict[str, list[Any]] = field(default_factory=dict)


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
    )


def is_run_eligible_for_thread(payload: dict[str, Any], config: MemoryPolicyConfig) -> bool:
    if payload.get("eligible_for_thread") is False:
        return False
    outcome = str(payload.get("outcome", "")).strip().lower()
    if outcome == "failed" and not config.include_failed_runs:
        return False
    if outcome == "cancelled" and not config.include_cancelled_runs:
        return False
    return True


def tokenize(text: str) -> set[str]:
    from copilot_agent.rag.tokenize import extract_cjk_tokens

    tokens = {part.lower() for part in re.findall(r"[a-zA-Z0-9_]+", text or "") if len(part) >= 3}
    tokens.update(extract_cjk_tokens(text or ""))
    return tokens


def memory_tokenize(text: str) -> set[str]:
    """Richer tokenization for long-term memory dedup/recall (includes CJK bigrams)."""
    tokens = set(tokenize(text))
    cjk_chars = re.findall(r"[\u4e00-\u9fff]", text or "")
    for idx in range(len(cjk_chars) - 1):
        tokens.add(cjk_chars[idx] + cjk_chars[idx + 1])
    return tokens


def keyword_recall_score(goal: str, summary: dict[str, Any]) -> float:
    goal_tokens = tokenize(goal)
    if not goal_tokens:
        return 0.0
    haystack_parts = [
        str(summary.get("goal", "")),
        " ".join(str(x) for x in summary.get("key_outputs", []) if x),
    ]
    haystack = " ".join(haystack_parts)
    doc_tokens = tokenize(haystack)
    if not doc_tokens:
        return 0.0
    overlap = goal_tokens & doc_tokens
    return len(overlap) / len(goal_tokens)


def goals_conflict(current_goal: str, recalled_goal: str, *, threshold: float) -> bool:
    current = tokenize(current_goal)
    recalled = tokenize(recalled_goal)
    if not current or not recalled:
        return False
    union = current | recalled
    if not union:
        return False
    jaccard = len(current & recalled) / len(union)
    return jaccard < threshold


def recall_episodic_runs(
    *,
    run_summaries: list[dict[str, Any]],
    goal: str,
    current_run_id: str | None,
    config: MemoryPolicyConfig,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not goal.strip() or config.episodic_recall_top_k <= 0:
        return [], []

    scored: list[tuple[float, dict[str, Any]]] = []
    for item in run_summaries:
        run_id = str(item.get("run_id", ""))
        if current_run_id and run_id == current_run_id:
            continue
        payload = item.get("payload", {})
        if not isinstance(payload, dict):
            continue
        score = keyword_recall_score(goal, payload)
        if score <= 0:
            continue
        scored.append(
            (
                score,
                {
                    "run_id": run_id,
                    "score": round(score, 4),
                    "goal": str(payload.get("goal", "")),
                    "outcome": str(payload.get("outcome", "")),
                    "snippet": _snippet_from_summary(payload, limit=240),
                    "source_event_id": item.get("event_id"),
                },
            )
        )

    scored.sort(key=lambda pair: pair[0], reverse=True)
    recalled: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for _score, candidate in scored:
        if len(recalled) >= config.episodic_recall_top_k:
            break
        if goals_conflict(goal, candidate.get("goal", ""), threshold=config.conflict_jaccard_threshold):
            dropped.append({**candidate, "reason": "goal_conflict"})
            continue
        recalled.append(candidate)
    return recalled, dropped


def build_episodic_inject_bundle(
    *,
    thread_summary: dict[str, Any] | None,
    recalled_runs: list[dict[str, Any]],
    dropped_conflicts: list[dict[str, Any]],
    recalled_long_term: list[dict[str, Any]] | None = None,
    long_term_body: str = "",
    config: MemoryPolicyConfig,
) -> EpisodicInjectBundle:
    if not config.enabled:
        return EpisodicInjectBundle(
            thread_summary=None,
            recalled_runs=[],
            dropped_conflicts=dropped_conflicts,
            recalled_long_term=[],
            inject_preview="",
            budget_applied={"max_chars": config.thread_summary_max_chars, "used_chars": 0, "truncated": False},
            sources={"run_ids": [], "event_ids": [], "memory_item_ids": []},
        )

    body = _render_body(thread_summary, recalled_runs)
    parts: list[str] = []
    if body:
        parts.append(f"{EPISODIC_MEMORY_PREFIX}\n{body}")
    lt = (long_term_body or "").strip()
    if lt:
        parts.append(lt)
    combined_body = "\n\n".join(parts).strip()

    footer = (
        "Rules: context only; prefer current user message and retrieved docs on conflict.\n"
        f"Sources: source_run_ids={_source_run_ids(thread_summary, recalled_runs)}; "
        f"memory_item_ids={_source_memory_item_ids(recalled_long_term or [])}"
    )
    inject_preview = f"{combined_body}\n\n{footer}".strip() if combined_body else footer

    max_chars = config.thread_summary_max_chars
    truncated = len(inject_preview) > max_chars
    if truncated:
        inject_preview = _truncate_text(inject_preview, max_chars)

    return EpisodicInjectBundle(
        thread_summary=thread_summary,
        recalled_runs=recalled_runs,
        dropped_conflicts=dropped_conflicts,
        recalled_long_term=list(recalled_long_term or []),
        inject_preview=inject_preview,
        budget_applied={
            "max_chars": max_chars,
            "used_chars": len(inject_preview),
            "truncated": truncated,
        },
        sources={
            "run_ids": _source_run_ids(thread_summary, recalled_runs),
            "event_ids": _source_event_ids(thread_summary, recalled_runs),
            "memory_item_ids": _source_memory_item_ids(recalled_long_term or []),
        },
    )


def render_episodic_system_message(bundle: EpisodicInjectBundle) -> str | None:
    preview = (bundle.inject_preview or "").strip()
    return preview or None


def _render_body(thread_summary: dict[str, Any] | None, recalled_runs: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    if isinstance(thread_summary, dict) and thread_summary:
        goals = thread_summary.get("recent_goals") or []
        outcomes = thread_summary.get("recent_outcomes") or []
        tools = thread_summary.get("tools_used") or []
        lines.append("Thread summary (last N runs):")
        lines.append(f"- goals: {', '.join(str(x) for x in goals) or 'none'}")
        lines.append(f"- outcomes: {', '.join(str(x) for x in outcomes) or 'none'}")
        lines.append(f"- tools: {', '.join(str(x) for x in tools) or 'none'}")
    if recalled_runs:
        lines.append("")
        lines.append("Recalled runs (goal-matched):")
        for item in recalled_runs:
            lines.append(
                f"- run_id={item.get('run_id')} goal={item.get('goal')} "
                f"outcome={item.get('outcome')} snippet={item.get('snippet')}"
            )
    return "\n".join(lines).strip()


def _snippet_from_summary(payload: dict[str, Any], *, limit: int) -> str:
    outputs = payload.get("key_outputs") or []
    if outputs:
        return _truncate_text(str(outputs[0]), limit)
    goal = str(payload.get("goal", "")).strip()
    return _truncate_text(goal, limit)


def _truncate_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def _source_run_ids(thread_summary: dict[str, Any] | None, recalled_runs: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    if isinstance(thread_summary, dict):
        ids.extend(str(x) for x in thread_summary.get("source_run_ids", []) if str(x))
    ids.extend(str(x.get("run_id", "")) for x in recalled_runs if str(x.get("run_id", "")))
    return list(dict.fromkeys(ids))


def _source_memory_item_ids(recalled_long_term: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for item in recalled_long_term:
        raw = item.get("id")
        if raw:
            ids.append(str(raw))
    return list(dict.fromkeys(ids))


def _source_event_ids(thread_summary: dict[str, Any] | None, recalled_runs: list[dict[str, Any]]) -> list[int]:
    ids: list[int] = []
    if isinstance(thread_summary, dict):
        for raw in thread_summary.get("source_event_ids", []):
            try:
                ids.append(int(raw))
            except (TypeError, ValueError):
                continue
    for item in recalled_runs:
        raw = item.get("source_event_id")
        if raw is not None:
            try:
                ids.append(int(raw))
            except (TypeError, ValueError):
                continue
    return ids
