from __future__ import annotations

import re
from typing import Any

from copilot_agent.memory.policy_config import MemoryPolicyConfig


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


__all__ = [
    "goals_conflict",
    "is_run_eligible_for_thread",
    "keyword_recall_score",
    "memory_tokenize",
    "recall_episodic_runs",
    "tokenize",
]
