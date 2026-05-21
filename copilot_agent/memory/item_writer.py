from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from typing import Any

from copilot_agent.memory.item_schema import (
    LONG_TERM_MEMORY_PREFIX,
    MemoryItemRecord,
    MemoryScope,
    MemoryType,
    MemoryWriteResult,
    RecalledMemoryItem,
)
from copilot_agent.memory.item_store import MemoryItemStore, content_hash
from copilot_agent.memory.embedding import cosine_similarity, embed_text
from copilot_agent.memory.hyde import build_hyde_query
from copilot_agent.memory.llm_extractor import extract_memories_for_run
from copilot_agent.memory.policy import MemoryPolicyConfig, memory_tokenize, tokenize
from copilot_agent.memory.rule_extract import extract_memory_candidates
from copilot_agent.runtime.event_store import utc_now_iso


def _similarity(a: str, b: str) -> float:
    ta = memory_tokenize(a)
    tb = memory_tokenize(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _expires_at_iso(ttl_days: int | None) -> str | None:
    if ttl_days is None or ttl_days <= 0:
        return None
    expiry = datetime.now(UTC).timestamp() + ttl_days * 86400
    return datetime.fromtimestamp(expiry, UTC).isoformat()


class MemoryItemWriter:
    def __init__(
        self,
        store: MemoryItemStore,
        *,
        policy: MemoryPolicyConfig,
        llm_provider: Any | None = None,
    ) -> None:
        self._store = store
        self._policy = policy
        self._llm_provider = llm_provider

    def upsert_candidate(
        self,
        *,
        user_id: str,
        thread_id: str,
        candidate: dict[str, Any],
    ) -> MemoryWriteResult:
        content = str(candidate.get("content", "")).strip()
        if not content:
            return MemoryWriteResult(action="skip", reason="empty_content")

        importance = float(candidate.get("importance", 0.5))
        if importance < self._policy.long_term_importance_min:
            return MemoryWriteResult(action="skip", reason="below_importance_threshold")

        scope = candidate.get("scope", MemoryScope.SESSION)
        if isinstance(scope, str):
            scope = MemoryScope(scope)
        memory_type = candidate.get("memory_type", MemoryType.FACT)
        if isinstance(memory_type, str):
            memory_type = MemoryType(memory_type)

        existing = self._store.list_active(user_id=user_id, thread_id=thread_id)
        same_hash = next((item for item in existing if item.content_hash == content_hash(content)), None)
        if same_hash is not None:
            return MemoryWriteResult(action="dedup_skip", item=same_hash, reason="identical_content")

        similar = [
            item
            for item in existing
            if item.memory_type == memory_type
            and _similarity(item.content, content) >= self._policy.long_term_dedup_jaccard_threshold
        ]
        now = utc_now_iso()
        pending_confirmation = bool(candidate.get("pending_confirmation", False))
        embedding = embed_text(
            content,
            use_vector=self._policy.long_term_use_vector,
            deterministic=self._policy.long_term_embedding_deterministic,
        )
        if similar:
            old = similar[0]
            if _similarity(old.content, content) >= self._policy.long_term_conflict_jaccard_threshold:
                history = list(old.history)
                history.append(
                    {
                        "version": old.version,
                        "content": old.content,
                        "updated_at": old.updated_at,
                        "action": "superseded",
                    }
                )
                self._store.deprecate(
                    old.id,
                    history_entry={"action": "superseded", "at": now, "by_content_hash": content_hash(content)},
                )
                new_item = MemoryItemRecord(
                    id=self._store.new_id(),
                    user_id=user_id,
                    thread_id=thread_id if scope == MemoryScope.SESSION else None,
                    scope=scope,
                    memory_type=memory_type,
                    content=content,
                    content_hash=content_hash(content),
                    importance=importance,
                    confidence=float(candidate.get("confidence", 0.8)),
                    version=old.version + 1,
                    supersedes_id=old.id,
                    is_deprecated=False,
                    pending_confirmation=pending_confirmation,
                    expires_at=_expires_at_iso(candidate.get("ttl_days")),
                    access_count=0,
                    last_accessed_at=None,
                    created_at=now,
                    updated_at=now,
                    source_run_id=str(candidate.get("source_run_id") or "") or None,
                    history=history,
                    embedding=embedding or old.embedding,
                )
                self._store.insert(new_item)
                return MemoryWriteResult(action="supersede", item=new_item, superseded_id=old.id)

        new_item = MemoryItemRecord(
            id=self._store.new_id(),
            user_id=user_id,
            thread_id=thread_id if scope == MemoryScope.SESSION else None,
            scope=scope,
            memory_type=memory_type,
            content=content,
            content_hash=content_hash(content),
            importance=importance,
            confidence=float(candidate.get("confidence", 0.8)),
            version=1,
            supersedes_id=None,
            is_deprecated=False,
            pending_confirmation=pending_confirmation,
            expires_at=_expires_at_iso(candidate.get("ttl_days")),
            access_count=0,
            last_accessed_at=None,
            created_at=now,
            updated_at=now,
            source_run_id=str(candidate.get("source_run_id") or "") or None,
            history=[],
            embedding=embedding,
        )
        self._store.insert(new_item)
        return MemoryWriteResult(action="insert", item=new_item)

    def persist_run_memories(
        self,
        *,
        user_id: str,
        thread_id: str,
        goal: str,
        key_outputs: list[str],
        outcome: str,
        run_id: str,
    ) -> list[MemoryWriteResult]:
        if not self._policy.long_term_enabled:
            return []
        self._store.delete_expired()
        candidates = extract_memories_for_run(
            goal=goal,
            key_outputs=key_outputs,
            outcome=outcome,
            run_id=run_id,
            policy=self._policy,
            llm_provider=self._llm_provider,
        )
        results: list[MemoryWriteResult] = []
        for candidate in candidates:
            results.append(
                self.upsert_candidate(user_id=user_id, thread_id=thread_id, candidate=candidate)
            )
        if self._policy.long_term_max_items_per_user > 0:
            self._store.evict_lowest_score(
                user_id=user_id,
                keep_count=self._policy.long_term_max_items_per_user,
                protected_importance=self._policy.long_term_protected_importance,
            )
        return results


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


def recall_long_term_items(
    *,
    store: MemoryItemStore,
    user_id: str,
    thread_id: str,
    query: str,
    policy: MemoryPolicyConfig,
    llm_provider: Any | None = None,
) -> list[RecalledMemoryItem]:
    if not policy.long_term_enabled or not query.strip():
        return []

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
        return []

    scored: list[RecalledMemoryItem] = []
    for item in items:
        doc_tokens = tokenize(item.content)
        keyword_score = 0.0
        if keyword_query_tokens and doc_tokens:
            overlap = keyword_query_tokens & doc_tokens
            keyword_score = len(overlap) / len(keyword_query_tokens)
        vector_score = 0.0
        if query_embedding is not None and item.embedding:
            vector_score = max(0.0, cosine_similarity(query_embedding, item.embedding))
        if policy.long_term_use_vector and query_embedding is not None:
            if (
                keyword_score < policy.long_term_recall_min_score
                and vector_score < policy.long_term_vector_min_score
            ):
                continue
        elif keyword_score < policy.long_term_recall_min_score:
            continue
        time_factor = _time_decay_factor(item.updated_at, half_life_days=policy.long_term_time_decay_half_life_days)
        if policy.long_term_use_vector and query_embedding is not None:
            final_score = (
                policy.long_term_keyword_weight * keyword_score
                + policy.long_term_vector_weight * vector_score
                + policy.long_term_time_weight * time_factor
                + policy.long_term_importance_weight * item.importance
            )
        else:
            final_score = (
                policy.long_term_keyword_weight * keyword_score
                + policy.long_term_time_weight * time_factor
                + policy.long_term_importance_weight * item.importance
            )
        scored.append(
            RecalledMemoryItem(
                item=item,
                score=final_score,
                keyword_score=keyword_score,
                time_factor=time_factor,
                vector_score=vector_score,
            )
        )

    scored.sort(key=lambda row: row.score, reverse=True)
    top = scored[: max(0, policy.long_term_recall_top_k)]
    store.touch_access([row.item.id for row in top])
    return top


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
