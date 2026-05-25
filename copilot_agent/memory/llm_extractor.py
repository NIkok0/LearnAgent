from __future__ import annotations

import json
import logging
import re
from typing import Any

from copilot_agent.memory.item_schema import MemoryScope, MemoryType
from copilot_agent.memory.rule_extract import extract_memory_candidates
from copilot_agent.memory.policy_config import MemoryPolicyConfig
from copilot_agent.settings import settings

log = logging.getLogger(__name__)

_EXTRACT_PROMPT = """Extract durable memories from an agent run. Return JSON array only.
Each item fields:
- type: one of fact|preference|behavior|task_summary
- content: concise memory text (<= 120 chars)
- importance: 0.0-1.0
- confidence: 0.0-1.0
- scope: user|session

Rules:
- Skip transient tool output noise.
- Preferences need explicit user intent.
- Keep at most 5 items.

Run goal: {goal}
Outcome: {outcome}
Assistant output excerpt: {output}
"""


def extract_memories_for_run(
    *,
    goal: str,
    key_outputs: list[str],
    outcome: str,
    run_id: str,
    policy: MemoryPolicyConfig,
    llm_provider: Any | None = None,
    memory_candidates_seed: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    rule_candidates = extract_memory_candidates(
        goal=goal,
        key_outputs=key_outputs,
        outcome=outcome,
        run_id=run_id,
        memory_candidates_seed=memory_candidates_seed,
    )
    if not policy.llm_extract_enabled:
        return _apply_pending_flags(rule_candidates, policy)
    if not settings.openai_api_key.strip():
        return _apply_pending_flags(rule_candidates, policy)
    try:
        llm_candidates = _extract_with_llm(
            goal=goal,
            key_outputs=key_outputs,
            outcome=outcome,
            run_id=run_id,
            llm_provider=llm_provider,
        )
        if llm_candidates:
            merged = _merge_candidates(rule_candidates, llm_candidates)
            return _apply_pending_flags(merged, policy)
    except Exception as exc:
        log.debug("LLM memory extract fallback to rules: %s", exc)
    return _apply_pending_flags(rule_candidates, policy)


def _extract_with_llm(
    *,
    goal: str,
    key_outputs: list[str],
    outcome: str,
    run_id: str,
    llm_provider: Any | None,
) -> list[dict[str, Any]]:
    if llm_provider is None:
        from copilot_agent.llm import LLMProvider

        llm_provider = LLMProvider()
    model = llm_provider.get_chat_model()
    output = " ".join(str(part) for part in key_outputs if str(part).strip())[:1200]
    prompt = _EXTRACT_PROMPT.format(goal=goal or "unknown", outcome=outcome, output=output or "none")
    response = model.invoke(prompt)
    content = getattr(response, "content", response)
    raw = str(content or "").strip()
    payload = _parse_json_array(raw)
    candidates: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        content_text = str(item.get("content", "")).strip()
        if not content_text:
            continue
        memory_type = _parse_memory_type(str(item.get("type", "fact")))
        scope = _parse_scope(str(item.get("scope", "session")))
        importance = _clamp_float(item.get("importance"), default=0.6)
        confidence = _clamp_float(item.get("confidence"), default=0.75)
        candidates.append(
            {
                "scope": scope,
                "memory_type": memory_type,
                "content": content_text[:400],
                "importance": importance,
                "confidence": confidence,
                "source_run_id": run_id,
                "ttl_days": 30 if memory_type == MemoryType.FACT else None,
                "extractor": "llm",
            }
        )
    return candidates


def _parse_json_array(raw: str) -> list[Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    parsed = json.loads(text)
    return parsed if isinstance(parsed, list) else []


def _parse_memory_type(raw: str) -> MemoryType:
    value = raw.strip().lower()
    mapping = {
        "fact": MemoryType.FACT,
        "preference": MemoryType.PREFERENCE,
        "behavior": MemoryType.BEHAVIOR,
        "task_summary": MemoryType.TASK_SUMMARY,
        "task": MemoryType.TASK_SUMMARY,
    }
    return mapping.get(value, MemoryType.FACT)


def _parse_scope(raw: str) -> MemoryScope:
    value = raw.strip().lower()
    if value == "user":
        return MemoryScope.USER
    return MemoryScope.SESSION


def _clamp_float(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, parsed))


def _merge_candidates(
    rule_candidates: list[dict[str, Any]],
    llm_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged = list(llm_candidates)
    seen = {" ".join(str(item.get("content", "")).split()).lower() for item in llm_candidates}
    for candidate in rule_candidates:
        key = " ".join(str(candidate.get("content", "")).split()).lower()
        if key and key not in seen:
            merged.append({**candidate, "extractor": "rule"})
            seen.add(key)
    return merged


def _apply_pending_flags(candidates: list[dict[str, Any]], policy: MemoryPolicyConfig) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for candidate in candidates:
        confidence = float(candidate.get("confidence", 0.8))
        pending = confidence < policy.llm_confirm_threshold
        out.append({**candidate, "pending_confirmation": pending})
    return out
