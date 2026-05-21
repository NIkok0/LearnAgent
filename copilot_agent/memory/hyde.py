from __future__ import annotations

import json
import logging
import re
from typing import Any

from copilot_agent.memory.policy import MemoryPolicyConfig
from copilot_agent.settings import settings

log = logging.getLogger(__name__)

_HYDE_RULE_TEMPLATE = (
    "Relevant user memory about: {query}. "
    "May include preferences, recurring tasks, platform usage patterns, and prior troubleshooting goals."
)


def build_hyde_query(
    query: str,
    *,
    policy: MemoryPolicyConfig,
    llm_provider: Any | None = None,
) -> str:
    """Expand a user query into a hypothetical memory document for retrieval."""
    base = (query or "").strip()
    if not base or not policy.hyde_enabled:
        return base
    if policy.hyde_mode == "rule":
        return _HYDE_RULE_TEMPLATE.format(query=base)
    if not settings.openai_api_key.strip():
        return _HYDE_RULE_TEMPLATE.format(query=base)
    try:
        return _hyde_with_llm(base, llm_provider=llm_provider)
    except Exception as exc:
        log.debug("HyDE LLM fallback to rule mode: %s", exc)
        return _HYDE_RULE_TEMPLATE.format(query=base)


def _hyde_with_llm(query: str, *, llm_provider: Any | None) -> str:
    if llm_provider is None:
        from copilot_agent.llm import LLMProvider

        llm_provider = LLMProvider()
    model = llm_provider.get_chat_model()
    prompt = (
        "Write one concise hypothetical memory snippet (<= 80 words) that would help retrieve "
        "stored user/session memories for the query below. Output plain text only.\n\n"
        f"Query: {query}"
    )
    response = model.invoke(prompt)
    content = getattr(response, "content", response)
    text = str(content or "").strip()
    if not text:
        return _HYDE_RULE_TEMPLATE.format(query=query)
    return text[:512]
