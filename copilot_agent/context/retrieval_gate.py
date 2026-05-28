from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Literal

from copilot_agent.context.preretrieval import should_preretrieve
from copilot_agent.context.preretrieval_dedupe import queries_equivalent
from copilot_agent.contracts.retrieval import RetrievalRequest, query_hash
from copilot_agent.scenario.router.types import ToolRoute

RetrievalGateAction = Literal["retrieve", "reuse_cache", "skip_rag", "route_to_tool_api"]

_DOC_INTENT_RE = re.compile(
    r"文档|接口|api|endpoint|排障|故障|部署|deploy|配置|config|日志|log|错误码|error\s*code|runbook|"
    r"QUEUED|PROCESSING|FAILED|status\s*code",
    flags=re.IGNORECASE,
)
_LIVE_INTENT_RE = re.compile(
    r"今天|当前|现在|最新|实时|线上|公网|生效|状态|status|current|latest|today|now|live|dns",
    flags=re.IGNORECASE,
)
_CHITCHAT_RE = re.compile(
    r"^(你好|您好|谢谢|好的|可以|嗯|确认|收到|ok|okay|thanks|thank you)[。！!,.，\s]*$",
    flags=re.IGNORECASE,
)
_FORMAT_RE = re.compile(
    r"格式化|润色|改写|翻译|总结成|整理成|帮我写|resume|简历|markdown|表格|json",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class RetrievalDecision:
    action: RetrievalGateAction
    reason: str
    confidence: float = 1.0
    cache_reused: bool = False
    recommended_next: str = ""
    policy_context_hash: str = ""
    similarity_score: float | None = None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "action": self.action,
            "reason": self.reason,
            "confidence": round(float(self.confidence), 4),
            "cache_reused": bool(self.cache_reused),
            "recommended_next": self.recommended_next,
            "policy_context_hash": self.policy_context_hash,
        }
        if self.similarity_score is not None:
            payload["similarity_score"] = round(float(self.similarity_score), 4)
        return payload


def build_policy_context_hash(request: RetrievalRequest) -> str:
    parts = [
        request.tenant_id,
        request.user_id,
        request.max_classification,
        "high_pii" if request.allow_high_pii else "no_high_pii",
        ",".join(sorted(str(item) for item in request.allowed_scopes)),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def cache_policy_compatible(cache: dict[str, object] | None, request: RetrievalRequest) -> bool:
    if not cache:
        return False
    expected = build_policy_context_hash(request)
    cached = str(cache.get("policy_context_hash") or "")
    if cached:
        return cached == expected
    return (
        str(cache.get("tenant_id") or "") == request.tenant_id
        and str(cache.get("user_id") or "") == request.user_id
        and str(cache.get("max_classification") or "") == request.max_classification
        and sorted(str(item) for item in (cache.get("allowed_scopes") or []))
        == sorted(str(item) for item in request.allowed_scopes)
    )


def memory_has_high_confidence_answer(memory_dict: dict[str, Any]) -> bool:
    episodic = memory_dict.get("episodic") if isinstance(memory_dict.get("episodic"), dict) else {}
    recalled = episodic.get("recalled_long_term") if isinstance(episodic.get("recalled_long_term"), list) else []
    for item in recalled:
        if not isinstance(item, dict):
            continue
        memory_type = str(item.get("memory_type") or "")
        confidence = _as_float(item.get("confidence"), default=0.0)
        score = _as_float(item.get("score"), default=0.0)
        if memory_type in {"fact", "task_summary", "behavior", "preference"} and confidence >= 0.85 and score >= 0.35:
            return True
    preview = str(episodic.get("inject_preview") or "")
    return bool(preview.strip() and len(preview) >= 40 and recalled)


def decide_retrieval(
    *,
    query: str,
    route: ToolRoute,
    memory_dict: dict[str, Any],
    request: RetrievalRequest,
    previous_cache: dict[str, object] | None = None,
) -> RetrievalDecision:
    policy_hash = build_policy_context_hash(request)
    text = (query or "").strip()
    if not text:
        return RetrievalDecision(
            action="skip_rag",
            reason="empty_query",
            confidence=1.0,
            recommended_next="answer_without_rag",
            policy_context_hash=policy_hash,
        )

    if _has_live_intent(text) or route.kind == "live_status":
        return RetrievalDecision(
            action="route_to_tool_api",
            reason="live_or_freshness_intent",
            confidence=0.92,
            recommended_next="prefer_live_tool_or_api",
            policy_context_hash=policy_hash,
        )

    similarity = _cache_similarity(text, previous_cache)
    if (
        similarity >= 0.75
        and _cache_has_allowed_chunks(previous_cache)
        and cache_policy_compatible(previous_cache, request)
    ):
        return RetrievalDecision(
            action="reuse_cache",
            reason="similar_query_cache_reusable",
            confidence=0.9,
            cache_reused=True,
            recommended_next="reuse_preretrieval_cache",
            policy_context_hash=policy_hash,
            similarity_score=similarity,
        )

    if memory_has_high_confidence_answer(memory_dict):
        return RetrievalDecision(
            action="skip_rag",
            reason="memory_high_confidence_answer",
            confidence=0.86,
            recommended_next="answer_from_memory_context",
            policy_context_hash=policy_hash,
            similarity_score=similarity if similarity > 0 else None,
        )

    if _is_chitchat_or_formatting(text):
        return RetrievalDecision(
            action="skip_rag",
            reason="conversation_or_formatting_intent",
            confidence=0.82,
            recommended_next="answer_without_rag",
            policy_context_hash=policy_hash,
            similarity_score=similarity if similarity > 0 else None,
        )

    if _has_doc_intent(text):
        return RetrievalDecision(
            action="retrieve",
            reason="documentation_intent",
            confidence=0.9,
            recommended_next="policy_aware_retrieval",
            policy_context_hash=policy_hash,
            similarity_score=similarity if similarity > 0 else None,
        )

    if should_preretrieve(route):
        return RetrievalDecision(
            action="retrieve",
            reason="route_recommends_search_docs",
            confidence=0.78,
            recommended_next="policy_aware_retrieval",
            policy_context_hash=policy_hash,
            similarity_score=similarity if similarity > 0 else None,
        )

    return RetrievalDecision(
        action="skip_rag",
        reason="route_does_not_need_rag",
        confidence=0.74,
        recommended_next="answer_without_preretrieval",
        policy_context_hash=policy_hash,
        similarity_score=similarity if similarity > 0 else None,
    )


def retrieval_decision_from_mapping(data: object) -> RetrievalDecision | None:
    if not isinstance(data, dict):
        return None
    action = str(data.get("action") or "")
    if action not in {"retrieve", "reuse_cache", "skip_rag", "route_to_tool_api"}:
        return None
    return RetrievalDecision(
        action=action,  # type: ignore[arg-type]
        reason=str(data.get("reason") or ""),
        confidence=_as_float(data.get("confidence"), default=0.0),
        cache_reused=bool(data.get("cache_reused")),
        recommended_next=str(data.get("recommended_next") or ""),
        policy_context_hash=str(data.get("policy_context_hash") or ""),
        similarity_score=_optional_float(data.get("similarity_score")),
    )


def _cache_similarity(query: str, cache: dict[str, object] | None) -> float:
    if not cache:
        return 0.0
    cached_query = str(cache.get("query") or "")
    if not cached_query:
        return 0.0
    if query_hash(query) == str(cache.get("query_hash") or ""):
        return 1.0
    if queries_equivalent(query, cached_query):
        return 1.0
    return _token_similarity(query, cached_query)


def _cache_has_allowed_chunks(cache: dict[str, object] | None) -> bool:
    if not cache:
        return False
    allowed = cache.get("allowed_chunk_ids")
    if isinstance(allowed, list) and allowed:
        return True
    chunk_keys = cache.get("chunk_keys")
    return isinstance(chunk_keys, list) and bool(chunk_keys)


def _token_similarity(left: str, right: str) -> float:
    from copilot_agent.context.preretrieval_dedupe import normalize_query

    a = normalize_query(left)
    b = normalize_query(right)
    if not a or not b:
        return 0.0
    tokens_a = set(a.split())
    tokens_b = set(b.split())
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


def _has_doc_intent(text: str) -> bool:
    return bool(_DOC_INTENT_RE.search(text))


def _has_live_intent(text: str) -> bool:
    return bool(_LIVE_INTENT_RE.search(text))


def _is_chitchat_or_formatting(text: str) -> bool:
    stripped = text.strip()
    if _CHITCHAT_RE.search(stripped):
        return True
    if _FORMAT_RE.search(stripped) and not _DOC_INTENT_RE.search(stripped):
        return True
    return False


def _as_float(value: object, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _optional_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
