from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph.message import RemoveMessage

from copilot_agent.memory.policy import MemoryPolicyConfig

log = logging.getLogger(__name__)

COMPACTION_PREFIX = "[CheckpointCompaction]"


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

        summary = _build_deterministic_summary(prefix, self._policy.checkpoint_compact_summary_max_chars)
        summary_message = SystemMessage(content=f"{COMPACTION_PREFIX}\n{summary}")
        new_messages = [summary_message, *suffix]

        removals = [RemoveMessage(id=str(message.id)) for message in messages if getattr(message, "id", None)]
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


def _build_deterministic_summary(messages: list[BaseMessage], max_chars: int) -> str:
    lines: list[str] = []
    for message in messages:
        role = _message_role(message)
        text = _message_text(message).strip()
        if not text:
            continue
        lines.append(f"- {role}: {_truncate(text, min(240, max_chars))}")
    body = "\n".join(lines).strip() or "No prior conversational content."
    header = "Earlier conversation summary (deterministic):"
    summary = f"{header}\n{body}"
    return _truncate(summary, max_chars)


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
