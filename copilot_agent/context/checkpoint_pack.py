from __future__ import annotations

import warnings
from typing import Any

from langchain_core._api import LangChainBetaWarning
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph.message import RemoveMessage

from copilot_agent.memory.checkpoint_compactor import (
    COMPACTION_PREFIX,
    CheckpointCompactor,
    _build_deterministic_summary,
    _split_messages_for_compaction,
)
from copilot_agent.memory.policy_config import MemoryPolicyConfig
from copilot_agent.settings import settings


async def load_checkpoint_messages(graph: Any, thread_id: str) -> list[BaseMessage]:
    config = {"configurable": {"thread_id": thread_id}}
    state = await graph.aget_state(config)
    values = getattr(state, "values", None) or {}
    messages = values.get("messages") if isinstance(values, dict) else []
    if not isinstance(messages, list):
        return []
    return list(messages)


def message_content_chars(message: BaseMessage) -> int:
    return len(str(getattr(message, "content", "") or ""))


def total_message_chars(messages: list[BaseMessage]) -> int:
    return sum(message_content_chars(message) for message in messages)


def _clone_message(message: BaseMessage) -> BaseMessage:
    content = getattr(message, "content", "")
    if isinstance(message, HumanMessage):
        return HumanMessage(content=content)
    if isinstance(message, AIMessage):
        return AIMessage(content=content, tool_calls=getattr(message, "tool_calls", None) or [])
    if isinstance(message, ToolMessage):
        return ToolMessage(
            content=content,
            tool_call_id=str(getattr(message, "tool_call_id", "") or ""),
            name=str(getattr(message, "name", "") or ""),
        )
    if isinstance(message, SystemMessage):
        return SystemMessage(content=content)
    return message.__class__(content=content)  # type: ignore[call-arg]


def _is_compaction_summary(message: BaseMessage) -> bool:
    if not isinstance(message, SystemMessage):
        return False
    return str(message.content or "").startswith(COMPACTION_PREFIX)


def _drop_oldest_trimable(messages: list[BaseMessage]) -> str | None:
    for index, message in enumerate(messages):
        if isinstance(message, HumanMessage) or _is_compaction_summary(message):
            continue
        messages.pop(index)
        return message.__class__.__name__.lower()
    return None


def _truncate_oldest_non_user(messages: list[BaseMessage]) -> bool:
    for index, message in enumerate(messages):
        if isinstance(message, HumanMessage) or _is_compaction_summary(message):
            continue
        content = str(message.content or "")
        if len(content) <= 120:
            continue
        target = max(80, len(content) // 2)
        trimmed_content = content[: target - 3].rstrip() + "..."
        if isinstance(message, AIMessage):
            messages[index] = AIMessage(content=trimmed_content, tool_calls=getattr(message, "tool_calls", None) or [])
        elif isinstance(message, ToolMessage):
            messages[index] = ToolMessage(
                content=trimmed_content,
                tool_call_id=str(getattr(message, "tool_call_id", "") or ""),
                name=str(getattr(message, "name", "") or ""),
            )
        elif isinstance(message, SystemMessage):
            messages[index] = SystemMessage(content=trimmed_content)
        return True
    return False


async def _persist_checkpoint_messages(
    graph: Any,
    thread_id: str,
    prior: list[BaseMessage],
    updated: list[BaseMessage],
) -> None:
    config = {"configurable": {"thread_id": thread_id}}
    removals = _remove_messages(prior)
    await graph.aupdate_state(config, {"messages": [*removals, *updated]})


def _remove_messages(messages: list[BaseMessage]) -> list[RemoveMessage]:
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=LangChainBetaWarning)
        return [RemoveMessage(id=str(message.id)) for message in messages if getattr(message, "id", None)]


async def pack_checkpoint_for_budget(
    graph: Any,
    thread_id: str,
    *,
    max_total_chars: int,
    new_turn_chars: int,
    policy: MemoryPolicyConfig,
    persist: bool = True,
) -> dict[str, Any]:
    """§2.5.4: shrink checkpoint working memory when history + new turn exceeds budget."""
    messages = await load_checkpoint_messages(graph, thread_id)
    checkpoint_chars = total_message_chars(messages)
    base = {
        "checkpoint_message_count": len(messages),
        "checkpoint_chars": checkpoint_chars,
        "new_turn_chars": new_turn_chars,
        "truncation_steps": [],
    }

    if not settings.context_checkpoint_pack_enabled or max_total_chars <= 0:
        return {**base, "compacted": False, "reason": "disabled", "persisted": False}

    if checkpoint_chars + new_turn_chars <= max_total_chars:
        return {**base, "compacted": False, "reason": "within_budget", "persisted": False}

    target_checkpoint_chars = max(0, max_total_chars - new_turn_chars)
    steps: list[str] = []

    if persist and policy.checkpoint_compact_enabled and len(messages) > policy.checkpoint_compact_message_threshold:
        compactor = CheckpointCompactor(graph, policy=policy)
        compact_result = await compactor.compact_if_needed(thread_id)
        if compact_result.get("compacted"):
            steps.append("checkpoint_compactor")
            messages = await load_checkpoint_messages(graph, thread_id)
            checkpoint_chars = total_message_chars(messages)
            if checkpoint_chars + new_turn_chars <= max_total_chars:
                return {
                    **base,
                    "compacted": True,
                    "reason": "compactor",
                    "persisted": True,
                    "checkpoint_message_count": len(messages),
                    "checkpoint_chars": checkpoint_chars,
                    "truncation_steps": steps,
                    "compactor": compact_result,
                }

    keep_turns = max(1, policy.checkpoint_compact_keep_recent_turns)
    prefix, suffix = _split_messages_for_compaction(messages, keep_turns=keep_turns)
    if prefix and checkpoint_chars > target_checkpoint_chars:
        summary = _build_deterministic_summary(prefix, policy.checkpoint_compact_summary_max_chars)
        updated = [SystemMessage(content=f"{COMPACTION_PREFIX}\n{summary}"), *[_clone_message(message) for message in suffix]]
        if persist:
            await _persist_checkpoint_messages(graph, thread_id, messages, updated)
        steps.append("summarized_prefix")
        messages = await load_checkpoint_messages(graph, thread_id) if persist else updated
        checkpoint_chars = total_message_chars(messages)

    prior = messages
    working = [_clone_message(message) for message in messages]
    changed = False

    while checkpoint_chars + new_turn_chars > max_total_chars and len(working) > 1:
        dropped = _drop_oldest_trimable(working)
        if dropped is None:
            break
        steps.append(f"dropped_{dropped}")
        checkpoint_chars = total_message_chars(working)
        changed = True

    while checkpoint_chars + new_turn_chars > max_total_chars:
        if not _truncate_oldest_non_user(working):
            break
        steps.append("truncated_old_message")
        checkpoint_chars = total_message_chars(working)
        changed = True

    if changed and persist:
        await _persist_checkpoint_messages(graph, thread_id, prior, working)
    elif changed:
        messages = working

    messages = await load_checkpoint_messages(graph, thread_id) if persist else messages
    checkpoint_chars = total_message_chars(messages)

    return {
        **base,
        "compacted": bool(steps),
        "reason": "char_budget" if steps else "nothing_to_trim",
        "persisted": bool(persist and steps),
        "checkpoint_message_count": len(messages),
        "checkpoint_chars": checkpoint_chars,
        "truncation_steps": steps,
        "target_checkpoint_chars": target_checkpoint_chars,
    }
