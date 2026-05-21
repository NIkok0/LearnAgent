from __future__ import annotations

from dataclasses import dataclass

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from copilot_agent.context.constants import RAG_PRERETRIEVAL_PREFIX, ROUTER_SYSTEM_PREFIX
from copilot_agent.memory.policy import EPISODIC_MEMORY_PREFIX
from copilot_agent.memory.item_schema import LONG_TERM_MEMORY_PREFIX


@dataclass(frozen=True)
class PackResult:
    messages: list[BaseMessage]
    used_chars: int
    truncated: bool
    steps: tuple[str, ...]


def _content_chars(message: BaseMessage) -> int:
    return len(str(getattr(message, "content", "") or ""))


def _total_chars(messages: list[BaseMessage]) -> int:
    return sum(_content_chars(message) for message in messages)


def _kind(message: BaseMessage) -> str:
    if isinstance(message, HumanMessage):
        return "user"
    if not isinstance(message, SystemMessage):
        return "other"
    content = str(message.content or "")
    if content.startswith(RAG_PRERETRIEVAL_PREFIX):
        return "rag"
    if content.startswith(EPISODIC_MEMORY_PREFIX) or content.startswith(LONG_TERM_MEMORY_PREFIX):
        return "memory"
    if content.startswith(ROUTER_SYSTEM_PREFIX):
        return "router"
    return "system"


def _truncate_system(message: SystemMessage, *, max_chars: int) -> SystemMessage:
    content = str(message.content or "")
    if len(content) <= max_chars:
        return message
    if max_chars <= 3:
        return SystemMessage(content=content[:max_chars])
    return SystemMessage(content=content[: max_chars - 3].rstrip() + "...")


def pack_graph_messages(
    messages: list[BaseMessage],
    *,
    max_chars: int,
    enabled: bool = True,
) -> PackResult:
    # §2.5.4 priority 6: checkpoint history handled in checkpoint_pack; trim memory then RAG here.
    if not enabled or max_chars <= 0:
        used = _total_chars(messages)
        return PackResult(messages=list(messages), used_chars=used, truncated=False, steps=())

    working = list(messages)
    steps: list[str] = []
    used = _total_chars(working)
    if used <= max_chars:
        return PackResult(messages=working, used_chars=used, truncated=False, steps=())

    def drop_kind(kind: str) -> bool:
        nonlocal used
        for idx in range(len(working) - 1, -1, -1):
            if _kind(working[idx]) != kind:
                continue
            used -= _content_chars(working[idx])
            working.pop(idx)
            steps.append(f"dropped_{kind}")
            return True
        return False

    def trim_kind(kind: str, *, target_max: int) -> bool:
        nonlocal used
        for idx, message in enumerate(working):
            if _kind(message) != kind or not isinstance(message, SystemMessage):
                continue
            current = _content_chars(message)
            if current <= target_max:
                return False
            trimmed = _truncate_system(message, max_chars=target_max)
            used += _content_chars(trimmed) - current
            working[idx] = trimmed
            steps.append(f"truncated_{kind}")
            return True
        return False

    # Priority 6: trim memory, then RAG; drop if still over.
    while used > max_chars:
        if trim_kind("memory", target_max=max(200, max_chars // 8)):
            if used <= max_chars:
                break
        if trim_kind("rag", target_max=max(400, max_chars // 4)):
            if used <= max_chars:
                break
        if drop_kind("memory"):
            if used <= max_chars:
                break
        if drop_kind("rag"):
            if used <= max_chars:
                break
        break

    return PackResult(
        messages=working,
        used_chars=used,
        truncated=bool(steps),
        steps=tuple(steps),
    )
