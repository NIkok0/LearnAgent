from __future__ import annotations

from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from copilot_agent.context.memory_inject import memory_context_messages
from copilot_agent.memory.policy import EPISODIC_MEMORY_PREFIX, MemoryPolicyConfig
from copilot_agent.memory.item_schema import LONG_TERM_MEMORY_PREFIX


async def checkpoint_has_prior_turns(graph: Any, thread_id: str) -> bool:
    config = {"configurable": {"thread_id": thread_id}}
    state = await graph.aget_state(config)
    values = getattr(state, "values", None) or {}
    messages = values.get("messages") if isinstance(values, dict) else []
    if not isinstance(messages, list):
        return False
    return any(isinstance(message, HumanMessage) for message in messages)


def _existing_system_contents(messages: list[Any]) -> set[str]:
    contents: set[str] = set()
    for message in messages:
        if isinstance(message, SystemMessage):
            content = str(getattr(message, "content", "") or "").strip()
            if content:
                contents.add(content)
    return contents


async def existing_system_contents(graph: Any, thread_id: str) -> set[str]:
    config = {"configurable": {"thread_id": thread_id}}
    state = await graph.aget_state(config)
    values = getattr(state, "values", None) or {}
    messages = values.get("messages") if isinstance(values, dict) else []
    if not isinstance(messages, list):
        return set()
    return _existing_system_contents(messages)


def _memory_message_seen(existing: set[str], content: str) -> bool:
    text = content.strip()
    if not text:
        return True
    if text in existing:
        return True
    for prior in existing:
        if prior.startswith(EPISODIC_MEMORY_PREFIX) and text.startswith(EPISODIC_MEMORY_PREFIX):
            if prior == text:
                return True
        if prior.startswith(LONG_TERM_MEMORY_PREFIX) and text.startswith(LONG_TERM_MEMORY_PREFIX):
            if prior == text:
                return True
    return False


async def build_graph_turn_messages(
    *,
    graph: Any,
    thread_id: str,
    system_prompt: str,
    memory_context: dict[str, Any],
    turn_messages: list[BaseMessage],
    policy: MemoryPolicyConfig,
    extra_system_messages: list[SystemMessage] | None = None,
) -> list[BaseMessage]:
    """Build per-turn graph input: system prompt, memory, router hints, user turn."""
    has_prior = await checkpoint_has_prior_turns(graph, thread_id)
    existing = await existing_system_contents(graph, thread_id) if policy.inject_dedupe_memory_messages else set()

    out: list[BaseMessage] = []
    if not (policy.inject_dedupe_system_prompt and has_prior):
        prompt = system_prompt.strip()
        if prompt and prompt not in existing:
            out.append(SystemMessage(content=prompt))

    for message in memory_context_messages(memory_context):
        content = str(message.content or "").strip()
        if policy.inject_dedupe_memory_messages and _memory_message_seen(existing, content):
            continue
        out.append(message)

    for message in extra_system_messages or []:
        content = str(message.content or "").strip()
        if not content:
            continue
        if policy.inject_dedupe_memory_messages and content in existing:
            continue
        out.append(message)

    out.extend(turn_messages)
    return out
