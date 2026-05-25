from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from copilot_agent.memory.embedding import cosine_similarity, embed_text
from copilot_agent.memory.hyde import build_hyde_query
from copilot_agent.memory.item_schema import (
    LONG_TERM_MEMORY_PREFIX,
    MemoryItemRecord,
    MemoryScope,
    MemoryType,
    RecalledMemoryItem,
)
from copilot_agent.memory.item_store import MemoryItemStore
from copilot_agent.memory.episodic_recall import tokenize
from copilot_agent.memory.policy_config import MemoryPolicyConfig


def _time_decay_factor(updated_at: str, *, half_life_days: float) -> float:
    if half_life_days <= 0:
        return 1.0
    try:
        updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=UTC)
        age_days = max(0.0, (datetime.now(UTC) - updated).total_seconds() / 86400.0)
    except ValueError:
        return 1.0
    return math.exp(-0.693147 * age_days / half_life_days)


def _access_factor(item: MemoryItemRecord, *, half_life_days: float) -> float:
    access_bonus = min(0.35, math.log1p(max(0, item.access_count)) * 0.08)
    if item.last_accessed_at:
        recency = _time_decay_factor(item.last_accessed_at, half_life_days=half_life_days)
        return min(1.35, 0.65 + access_bonus + 0.35 * recency)
    if item.access_count > 0:
        return min(1.15, 0.85 + access_bonus)
    return 0.7


def _confidence_factor(confidence: float) -> float:
    return max(0.25, min(1.15, confidence))


def recall_long_term_items(
    *,
    store: MemoryItemStore,
    user_id: str,
    thread_id: str,
    query: str,
    policy: MemoryPolicyConfig,
    llm_provider: Any | None = None,
    record_access: bool = True,
    route_kind: str = "",
    recommended_tools: tuple[str, ...] = (),
) -> list[RecalledMemoryItem]:
    recalled, _dropped = recall_long_term_items_with_explain(
        store=store,
        user_id=user_id,
        thread_id=thread_id,
        query=query,
        policy=policy,
        llm_provider=llm_provider,
        record_access=record_access,
        route_kind=route_kind,
        recommended_tools=recommended_tools,
    )
    return recalled


def recall_long_term_items_with_explain(
    *,
    store: MemoryItemStore,
    user_id: str,
    thread_id: str,
    query: str,
    policy: MemoryPolicyConfig,
    llm_provider: Any | None = None,
    record_access: bool = True,
    route_kind: str = "",
    recommended_tools: tuple[str, ...] = (),
) -> tuple[list[RecalledMemoryItem], list[dict[str, Any]]]:
    if not policy.long_term_enabled or not query.strip():
        return [], []

    recall_query = build_hyde_query(query, policy=policy, llm_provider=llm_provider)
    items = store.list_active(
        user_id=user_id,
        thread_id=thread_id,
        scopes=(MemoryScope.USER, MemoryScope.SESSION),
        include_pending=False,
    )
    keyword_query_tokens = tokenize(query)
    query_embedding = embed_text(
        recall_query,
        use_vector=policy.long_term_use_vector,
        deterministic=policy.long_term_embedding_deterministic,
    )
    if not keyword_query_tokens and query_embedding is None:
        return [], []

    scored: list[RecalledMemoryItem] = []
    dropped: list[dict[str, Any]] = []
    for item in items:
        doc_tokens = tokenize(item.content)
        keyword_score = 0.0
        if keyword_query_tokens and doc_tokens:
            overlap = keyword_query_tokens & doc_tokens
            keyword_score = len(overlap) / len(keyword_query_tokens)
        vector_score = 0.0
        if query_embedding is not None and item.embedding:
            vector_score = max(0.0, cosine_similarity(query_embedding, item.embedding))
        type_boost = _memory_type_boost(item.memory_type, route_kind=route_kind, policy=policy)
        wrong_intent = _wrong_intent(item.memory_type, route_kind=route_kind, recommended_tools=recommended_tools)
        if wrong_intent and keyword_score < policy.long_term_recall_min_score * 1.5:
            dropped.append({**item.as_dict(), "reason": "wrong_intent", "route_kind": route_kind})
            continue
        if policy.long_term_use_vector and query_embedding is not None:
            if (
                keyword_score < policy.long_term_recall_min_score
                and vector_score < policy.long_term_vector_min_score
            ):
                dropped.append({**item.as_dict(), "reason": "low_score", "route_kind": route_kind})
                continue
        elif keyword_score < policy.long_term_recall_min_score:
            dropped.append({**item.as_dict(), "reason": "low_score", "route_kind": route_kind})
            continue
        time_factor = _time_decay_factor(item.updated_at, half_life_days=policy.long_term_time_decay_half_life_days)
        aging_factor = time_factor
        confidence_factor = _confidence_factor(item.confidence)
        access_factor = _access_factor(item, half_life_days=policy.access_decay_half_life_days)
        if policy.long_term_use_vector and query_embedding is not None:
            final_score = (
                policy.long_term_keyword_weight * keyword_score
                + policy.long_term_vector_weight * vector_score
                + policy.long_term_time_weight * time_factor
                + policy.long_term_importance_weight * item.importance
                + policy.recall_confidence_weight * confidence_factor
                + policy.recall_access_weight * access_factor
                + type_boost
            )
        else:
            final_score = (
                policy.long_term_keyword_weight * keyword_score
                + policy.long_term_time_weight * time_factor
                + policy.long_term_importance_weight * item.importance
                + policy.recall_confidence_weight * confidence_factor
                + policy.recall_access_weight * access_factor
                + type_boost
            )
        if final_score < policy.long_term_inject_min_score:
            dropped.append(
                {
                    **item.as_dict(),
                    "reason": "low_score",
                    "route_kind": route_kind,
                    "score": round(final_score, 4),
                    "type_boost": round(type_boost, 4),
                    "aging_factor": round(aging_factor, 4),
                    "confidence_factor": round(confidence_factor, 4),
                    "access_factor": round(access_factor, 4),
                }
            )
            continue
        scored.append(
            RecalledMemoryItem(
                item=item,
                score=final_score,
                keyword_score=keyword_score,
                time_factor=time_factor,
                vector_score=vector_score,
                type_boost=type_boost,
                route_kind=route_kind,
                aging_factor=aging_factor,
                confidence_factor=confidence_factor,
                access_factor=access_factor,
            )
        )

    scored.sort(key=lambda row: row.score, reverse=True)
    top = scored[: max(0, policy.long_term_recall_top_k)]
    for row in scored[max(0, policy.long_term_recall_top_k) :]:
        dropped.append({**row.as_dict(), "reason": "budget_exceeded", "route_kind": route_kind})
    if record_access:
        store.touch_access([row.item.id for row in top])
    return top, dropped


def _memory_type_boost(memory_type: MemoryType, *, route_kind: str, policy: MemoryPolicyConfig) -> float:
    base = {
        MemoryType.FACT: policy.memory_type_boost_fact,
        MemoryType.PREFERENCE: policy.memory_type_boost_preference,
        MemoryType.BEHAVIOR: policy.memory_type_boost_behavior,
        MemoryType.TASK_SUMMARY: policy.memory_type_boost_task_summary,
    }.get(memory_type, 0.0)
    if route_kind in {"troubleshooting", "live_status"}:
        if memory_type == MemoryType.TASK_SUMMARY:
            return base + 0.06
        if memory_type == MemoryType.FACT:
            return base + 0.04
        if memory_type in {MemoryType.PREFERENCE, MemoryType.BEHAVIOR}:
            return max(0.0, base - 0.02)
    if route_kind == "knowledge" and memory_type == MemoryType.FACT:
        return base + 0.04
    if route_kind in {"dangerous_execute", "safety_reject"} and memory_type == MemoryType.PREFERENCE:
        return base + 0.02
    return base


def _wrong_intent(
    memory_type: MemoryType,
    *,
    route_kind: str,
    recommended_tools: tuple[str, ...],
) -> bool:
    if route_kind in {"troubleshooting", "live_status"} and memory_type == MemoryType.PREFERENCE:
        return True
    if "http_get" in recommended_tools and memory_type == MemoryType.PREFERENCE:
        return True
    return False


def render_long_term_memory_body(recalled: list[RecalledMemoryItem]) -> str:
    if not recalled:
        return ""
    lines = ["Known user/session facts (retrieved):"]
    for row in recalled:
        item = row.item
        lines.append(
            f"- [{item.memory_type.value}|{item.scope.value}] {item.content} "
            f"(importance={item.importance:.2f}, score={row.score:.2f}, vector={row.vector_score:.2f})"
        )
    footer = "Rules: use only when relevant; prefer current user message on conflict."
    return f"{LONG_TERM_MEMORY_PREFIX}\n" + "\n".join(lines) + f"\n\n{footer}"


__all__ = [
    "recall_long_term_items",
    "recall_long_term_items_with_explain",
    "render_long_term_memory_body",
]
