from __future__ import annotations

from typing import Any

from copilot_agent.memory.item_schema import MemoryType
from copilot_agent.memory.policy_config import MemoryPolicyConfig
from copilot_agent.tools.sanitize import audit_payload_has_secret


def _is_reusable_candidate(candidate: dict[str, Any], *, outcome: str) -> bool:
    if bool(candidate.get("reusable")):
        return True
    memory_type = candidate.get("memory_type", MemoryType.FACT)
    if isinstance(memory_type, str):
        memory_type = MemoryType(memory_type)
    if memory_type in {MemoryType.PREFERENCE, MemoryType.BEHAVIOR}:
        return True
    if memory_type == MemoryType.TASK_SUMMARY and outcome == "completed":
        return True
    content = str(candidate.get("content", "")).lower()
    reusable_markers = {
        "default",
        "always",
        "prefer",
        "redis",
        "stream",
        "endpoint",
        "api",
        "runbook",
        "閮ㄧ讲",
        "鎺掓煡",
        "榛樿",
        "鍋忓ソ",
    }
    return any(marker in content for marker in reusable_markers)


def conversion_skip_reason(candidate: dict[str, Any], *, outcome: str, policy: MemoryPolicyConfig) -> str:
    """Return an explainable reason when a short-term candidate must not become active long-term memory."""
    if not policy.write_gate_enabled:
        return ""
    content = str(candidate.get("content", "")).strip()
    if not content:
        return "empty_content"
    if audit_payload_has_secret(candidate):
        return "sensitive_payload"
    memory_type = candidate.get("memory_type", MemoryType.FACT)
    if isinstance(memory_type, str):
        memory_type = MemoryType(memory_type)
    confidence = float(candidate.get("confidence", 0.8))
    importance = float(candidate.get("importance", 0.5))
    if importance < policy.long_term_importance_min:
        return "below_importance_threshold"
    stable_outcome = outcome == "completed"
    if not stable_outcome:
        high_conf_preference = memory_type in {MemoryType.PREFERENCE, MemoryType.BEHAVIOR} and confidence >= max(
            policy.write_min_confidence,
            policy.llm_confirm_threshold,
        )
        reusable_policy_fact = (
            memory_type == MemoryType.FACT
            and bool(candidate.get("reusable"))
            and str(candidate.get("source_kind") or "") == "policy_decision"
        )
        if not high_conf_preference and not reusable_policy_fact:
            return "unstable_outcome"
    if confidence < policy.write_min_confidence:
        return "low_confidence"
    if policy.write_require_reusable and not _is_reusable_candidate(candidate, outcome=outcome):
        return "non_reusable"
    return ""


__all__ = ["conversion_skip_reason"]
