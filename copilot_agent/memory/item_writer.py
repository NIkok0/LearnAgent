from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from copilot_agent.memory.item_schema import (
    MemoryItemRecord,
    MemoryScope,
    MemoryType,
    MemoryWriteResult,
)
from copilot_agent.memory.item_store import MemoryItemStore, content_hash
from copilot_agent.memory.embedding import embed_text
from copilot_agent.memory.llm_extractor import extract_memories_for_run
from copilot_agent.memory.episodic_recall import memory_tokenize
from copilot_agent.memory.policy_config import MemoryPolicyConfig
from copilot_agent.memory.conversion_policy import conversion_skip_reason
from copilot_agent.memory.recall_policy import (
    recall_long_term_items,
    recall_long_term_items_with_explain,
    render_long_term_memory_body,
)
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
            similarity = _similarity(old.content, content)
            if similarity >= self._policy.long_term_conflict_jaccard_threshold:
                pending_reason = _contradiction_pending_reason(
                    old,
                    content=content,
                    similarity=similarity,
                    policy=self._policy,
                )
                if pending_reason:
                    pending_item = MemoryItemRecord(
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
                        pending_confirmation=True,
                        expires_at=_expires_at_iso(candidate.get("ttl_days")),
                        access_count=0,
                        last_accessed_at=None,
                        created_at=now,
                        updated_at=now,
                        source_run_id=str(candidate.get("source_run_id") or "") or None,
                        history=[
                            *list(old.history),
                            {
                                "action": pending_reason,
                                "at": now,
                                "candidate_content_hash": content_hash(content),
                                "similarity": round(similarity, 4),
                            },
                        ],
                        embedding=embedding or old.embedding,
                    )
                    self._store.insert(pending_item)
                    return MemoryWriteResult(
                        action="pending",
                        item=pending_item,
                        superseded_id=old.id,
                        reason=pending_reason,
                        pending_reason=pending_reason,
                    )
                history = list(old.history)
                history.append(
                    {
                        "version": old.version,
                        "content_hash": old.content_hash,
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
        memory_candidates_seed: list[dict[str, Any]] | None = None,
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
            memory_candidates_seed=memory_candidates_seed,
        )
        results: list[MemoryWriteResult] = []
        for candidate in candidates:
            skip_reason = conversion_skip_reason(candidate, outcome=outcome, policy=self._policy)
            if skip_reason:
                results.append(MemoryWriteResult(action="skip", reason=skip_reason))
                continue
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


_write_gate_skip_reason = conversion_skip_reason


_NEGATION_MARKERS = {
    "not",
    "never",
    "avoid",
    "disable",
    "without",
    "不要",
    "不",
    "避免",
    "禁用",
    "关闭",
}


def _has_negation(text: str) -> bool:
    lower = text.lower()
    return any(marker in lower for marker in _NEGATION_MARKERS)


def _contradiction_pending_reason(
    old: MemoryItemRecord,
    *,
    content: str,
    similarity: float,
    policy: MemoryPolicyConfig,
) -> str:
    if not policy.contradiction_pending_enabled:
        return ""
    if similarity < policy.contradiction_pending_threshold:
        return ""
    if old.memory_type in {MemoryType.PREFERENCE, MemoryType.FACT} and _has_negation(old.content) != _has_negation(content):
        return "contradiction_pending"
    old_tokens = memory_tokenize(old.content)
    new_tokens = memory_tokenize(content)
    polarity_pairs = [
        ({"short", "terse", "简洁", "简短"}, {"long", "detailed", "详细", "长"}),
        ({"allow", "enable", "开启", "允许"}, {"deny", "disable", "关闭", "禁止"}),
    ]
    for left, right in polarity_pairs:
        old_left = bool(old_tokens & left)
        old_right = bool(old_tokens & right)
        new_left = bool(new_tokens & left)
        new_right = bool(new_tokens & right)
        if (old_left and new_right) or (old_right and new_left):
            return "contradiction_pending"
    return ""


