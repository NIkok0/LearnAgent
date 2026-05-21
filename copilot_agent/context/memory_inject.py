from __future__ import annotations

from typing import Any

from langchain_core.messages import SystemMessage


def memory_context_messages(memory_context: dict[str, Any]) -> list[SystemMessage]:
    episodic = memory_context.get("episodic") or {}
    if not episodic.get("enabled", True):
        return []
    inject_preview = str(episodic.get("inject_preview") or "").strip()
    if not inject_preview:
        return []
    return [SystemMessage(content=inject_preview)]
