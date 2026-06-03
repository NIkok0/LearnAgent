from __future__ import annotations

from typing import Any

from copilot_agent.contracts.context import ContextBlock
from copilot_agent.scenario.router.types import ToolRoute


def estimate_tokens(text: str) -> int:
    value = str(text or "")
    return max(0, len(value) // 4)


def build_context_blocks(
    *,
    scenario_name: str,
    system_prompt: str,
    route: ToolRoute,
    memory_injections: list[dict[str, Any]],
    skill_injections: list[dict[str, Any]],
    retrieved_context: list[dict[str, Any]],
    policy_hints: list[dict[str, Any]],
    tool_schema_count: int,
    budget: dict[str, Any],
    truncation_report: dict[str, Any],
) -> list[ContextBlock]:
    """Build v2 provider-style context trace without changing graph input."""
    blocks: list[ContextBlock] = []
    if system_prompt:
        blocks.append(
            ContextBlock(
                source="system_prompt",
                priority=0,
                content=system_prompt,
                token_estimate=max(1, estimate_tokens(system_prompt)),
                policy_tags=["scenario"],
                trace_id="system_prompt:scenario",
                metadata={"scenario": scenario_name},
            )
        )
    blocks.append(
        ContextBlock(
            source="route",
            priority=10,
            token_estimate=0,
            policy_tags=["planner_hint"],
            trace_id="route:tool_route",
            metadata=route.as_dict(),
        )
    )
    for item in memory_injections:
        kind = str(item.get("kind") or "memory")
        blocks.append(
            ContextBlock(
                source=f"memory:{kind}",
                priority=30,
                token_estimate=max(0, int(item.get("preview_chars") or 0) // 4),
                policy_tags=["memory"],
                trace_id=f"memory:{kind}",
                metadata=item,
            )
        )
    for item in skill_injections:
        name = str(item.get("name") or "skill")
        description = str(item.get("description") or "")
        blocks.append(
            ContextBlock(
                source="skill",
                priority=40,
                content=description,
                token_estimate=estimate_tokens(description),
                policy_tags=["skill", "injected" if item.get("injected", True) else "not_injected"],
                trace_id=f"skill:{name}",
                metadata=item,
            )
        )
    if retrieved_context:
        blocks.append(
            ContextBlock(
                source="retrieval",
                priority=50,
                token_estimate=sum(
                    estimate_tokens(str(item.get("text") or item.get("excerpt") or ""))
                    for item in retrieved_context
                    if isinstance(item, dict)
                ),
                policy_tags=["rag", "evidence"],
                trace_id="retrieval:preretrieval",
                metadata={
                    "items": len(retrieved_context),
                    "decision": truncation_report.get("retrieval_decision") or {},
                },
            )
        )
    blocks.append(
        ContextBlock(
            source="tool_schemas",
            priority=60,
            token_estimate=0,
            policy_tags=["capability"],
            trace_id="capability:tool_registry",
            metadata={"tools": tool_schema_count},
        )
    )
    blocks.append(
        ContextBlock(
            source="policy_hints",
            priority=70,
            token_estimate=0,
            policy_tags=["policy"],
            trace_id="policy:hints",
            metadata={"hints": policy_hints},
        )
    )
    blocks.append(
        ContextBlock(
            source="budget_packer",
            priority=90,
            token_estimate=0,
            policy_tags=["budget"],
            trace_id="budget:packing",
            metadata={"budget": budget, "truncation_report": truncation_report},
        )
    )
    return blocks
