from __future__ import annotations

from copilot_agent.contracts.events.payloads import ContextBuiltPayload


def build_context_built_payload(
    *,
    user_message: str,
    assembled_message_count: int,
    budget_max_chars: int,
    used_chars: int,
    truncated: bool,
    truncation_steps: list[str] | tuple[str, ...] | None = None,
    router_injected: bool = False,
    preretrieval_enabled: bool = False,
    preretrieval_sources: list[str] | None = None,
    preretrieval_excerpt_chars: int = 0,
    memory_inject_chars: int = 0,
    checkpoint_compacted: bool = False,
    checkpoint_chars: int = 0,
    retrieval_decision: dict[str, object] | None = None,
) -> dict[str, object]:
    payload = ContextBuiltPayload(
        user_message_chars=len(user_message or ""),
        assembled_message_count=assembled_message_count,
        budget_max_chars=budget_max_chars,
        used_chars=used_chars,
        truncated=truncated,
        truncation_steps=list(truncation_steps or []),
        router_injected=router_injected,
        preretrieval_enabled=preretrieval_enabled,
        preretrieval_sources=list(preretrieval_sources or []),
        preretrieval_excerpt_chars=preretrieval_excerpt_chars,
        memory_inject_chars=memory_inject_chars,
        checkpoint_compacted=checkpoint_compacted,
        checkpoint_chars=checkpoint_chars,
        retrieval_decision=retrieval_decision,
    )
    return payload.model_dump(exclude_none=True)
