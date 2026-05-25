from __future__ import annotations

import logging
import warnings
from typing import Any

from langchain_core._api import LangChainBetaWarning
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph.message import RemoveMessage

from copilot_agent.memory.policy_config import MemoryPolicyConfig
from copilot_agent.memory.schema import CheckpointCompactionSummary, CheckpointSummarySection

log = logging.getLogger(__name__)

COMPACTION_PREFIX = "[CheckpointCompaction]"
_MEMORY_INJECTION_PREFIXES = ("[MemoryContext]", "[EpisodicMemory]", "[LongTermMemory]")


class CheckpointCompactor:
    """Deterministic checkpoint message compaction for working memory."""

    def __init__(self, graph: Any, *, policy: MemoryPolicyConfig) -> None:
        self._graph = graph
        self._policy = policy

    async def compact_if_needed(self, thread_id: str) -> dict[str, Any]:
        if not self._policy.checkpoint_compact_enabled:
            return {"compacted": False, "reason": "disabled"}

        config = {"configurable": {"thread_id": thread_id}}
        state = await self._graph.aget_state(config)
        if getattr(state, "next", None):
            return {"compacted": False, "reason": "has_interrupt"}

        values = getattr(state, "values", None) or {}
        messages = values.get("messages") if isinstance(values, dict) else []
        if not isinstance(messages, list):
            messages = []

        threshold = self._policy.checkpoint_compact_message_threshold
        if len(messages) <= threshold:
            return {
                "compacted": False,
                "reason": "below_threshold",
                "message_count": len(messages),
                "threshold": threshold,
            }

        keep_turns = max(1, self._policy.checkpoint_compact_keep_recent_turns)
        prefix, suffix = _split_messages_for_compaction(messages, keep_turns=keep_turns)
        if not prefix:
            return {"compacted": False, "reason": "nothing_to_compact", "message_count": len(messages)}

        summary_model = build_checkpoint_summary_model(prefix, policy=self._policy, kept_recent_turns=keep_turns)
        summary = render_checkpoint_summary(summary_model, self._policy.checkpoint_compact_summary_max_chars)
        summary_message = SystemMessage(content=f"{COMPACTION_PREFIX}\n{summary}")
        new_messages = [summary_message, *suffix]

        removals = _remove_messages(messages)
        await self._graph.aupdate_state(config, {"messages": [*removals, *new_messages]})

        after = await self._graph.aget_state(config)
        after_values = getattr(after, "values", None) or {}
        after_messages = after_values.get("messages") if isinstance(after_values, dict) else []
        after_count = len(after_messages) if isinstance(after_messages, list) else 0

        result = {
            "compacted": True,
            "thread_id": thread_id,
            "before_count": len(messages),
            "after_count": after_count,
            "prefix_count": len(prefix),
            "kept_count": len(suffix),
            "summary_format": summary_model.format_version,
            "sections_present": _sections_present(summary_model),
            "summary_chars": len(summary),
            "summary_model": summary_model.model_copy(update={"summary_chars": len(summary)}).model_dump(mode="json"),
        }
        log.info(
            "Compacted checkpoint for thread %s: %d -> %d messages",
            thread_id[:8],
            len(messages),
            after_count,
        )
        return result


def _split_messages_for_compaction(
    messages: list[BaseMessage],
    *,
    keep_turns: int,
) -> tuple[list[BaseMessage], list[BaseMessage]]:
    user_indices = [index for index, message in enumerate(messages) if isinstance(message, HumanMessage)]
    if len(user_indices) <= keep_turns:
        return [], messages

    split_index = user_indices[-keep_turns]
    return messages[:split_index], messages[split_index:]


def _remove_messages(messages: list[BaseMessage]) -> list[RemoveMessage]:
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=LangChainBetaWarning)
        return [RemoveMessage(id=str(message.id)) for message in messages if getattr(message, "id", None)]


def build_checkpoint_summary_model(
    messages: list[BaseMessage],
    *,
    policy: MemoryPolicyConfig,
    kept_recent_turns: int = 0,
) -> CheckpointCompactionSummary:
    buckets: dict[str, list[str]] = {
        "task_context": [],
        "decisions_made": [],
        "important_facts": [],
        "tool_results": [],
        "open_questions": [],
        "do_not_carry_forward": [],
    }
    dropped_counts = {key: 0 for key in buckets}
    per_section_limit = max(1, policy.checkpoint_compact_keep_recent_turns)
    for message in messages:
        role = _message_role(message)
        text = _message_text(message).strip()
        if not text or _is_memory_injection_text(text):
            continue
        key = _summary_bucket(message, text)
        item = f"{role}: {_truncate(_normalize_summary_text(text), 240)}"
        if len(buckets[key]) >= per_section_limit:
            dropped_counts[key] += 1
            continue
        buckets[key].append(item)

    if not any(buckets.values()):
        buckets["important_facts"].append("No prior conversational content.")

    return CheckpointCompactionSummary(
        task_context=CheckpointSummarySection(
            title="Task Context",
            items=buckets["task_context"],
            dropped_count=dropped_counts["task_context"],
        ),
        decisions_made=CheckpointSummarySection(
            title="Decisions Made",
            items=buckets["decisions_made"],
            dropped_count=dropped_counts["decisions_made"],
        ),
        important_facts=CheckpointSummarySection(
            title="Important Facts",
            items=buckets["important_facts"],
            dropped_count=dropped_counts["important_facts"],
        ),
        tool_results=CheckpointSummarySection(
            title="Tool Results",
            items=buckets["tool_results"],
            dropped_count=dropped_counts["tool_results"],
        ),
        open_questions=CheckpointSummarySection(
            title="Open Questions",
            items=buckets["open_questions"],
            dropped_count=dropped_counts["open_questions"],
        ),
        do_not_carry_forward=CheckpointSummarySection(
            title="Do Not Carry Forward",
            items=buckets["do_not_carry_forward"],
            dropped_count=dropped_counts["do_not_carry_forward"],
        ),
        source_message_count=len(messages),
        kept_recent_turns=kept_recent_turns,
    )


def render_checkpoint_summary(summary: CheckpointCompactionSummary, max_chars: int) -> str:
    return summary.render_for_prompt(max_chars=max_chars)


def _build_deterministic_summary(messages: list[BaseMessage], max_chars: int) -> str:
    policy = MemoryPolicyConfig(checkpoint_compact_summary_max_chars=max_chars)
    summary = build_checkpoint_summary_model(messages, policy=policy)
    return render_checkpoint_summary(summary, max_chars)


def _summary_bucket(message: BaseMessage, text: str) -> str:
    lower = text.lower()
    if _looks_stale_or_failed(lower):
        return "do_not_carry_forward"
    if isinstance(message, ToolMessage):
        return "tool_results"
    if isinstance(message, HumanMessage):
        if "?" in text or "？" in text:
            return "open_questions"
        return "task_context"
    if isinstance(message, AIMessage):
        if _looks_decision(lower):
            return "decisions_made"
        return "important_facts"
    if isinstance(message, SystemMessage):
        return "important_facts"
    return "important_facts"


def _looks_decision(lower: str) -> bool:
    markers = (
        "decide",
        "decision",
        "implemented",
        "completed",
        "fixed",
        "use ",
        "采用",
        "决定",
        "已完成",
        "实现",
        "修复",
    )
    return any(marker in lower for marker in markers)


def _looks_stale_or_failed(lower: str) -> bool:
    markers = (
        "error",
        "failed",
        "cancelled",
        "rejected",
        "obsolete",
        "deprecated",
        "失败",
        "取消",
        "拒绝",
        "过期",
        "废弃",
    )
    return any(marker in lower for marker in markers)


def _is_memory_injection_text(text: str) -> bool:
    return text.startswith(_MEMORY_INJECTION_PREFIXES)


def _normalize_summary_text(text: str) -> str:
    return " ".join(text.split())


def _sections_present(summary: CheckpointCompactionSummary) -> list[str]:
    sections = [
        summary.task_context,
        summary.decisions_made,
        summary.important_facts,
        summary.tool_results,
        summary.open_questions,
        summary.do_not_carry_forward,
    ]
    return [section.title for section in sections if section.items]


def _message_role(message: BaseMessage) -> str:
    if isinstance(message, HumanMessage):
        return "user"
    if isinstance(message, AIMessage):
        return "assistant"
    if isinstance(message, SystemMessage):
        return "system"
    if isinstance(message, ToolMessage):
        return "tool"
    return message.__class__.__name__.lower()


def _message_text(message: BaseMessage) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, str):
                parts.append(item)
        return " ".join(parts)
    return str(content or "")


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."
