from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ContextBlock(BaseModel):
    """Traceable context provider output for LearnAgent v2 context core."""

    source: str
    priority: int = 100
    content: str = ""
    token_estimate: int = 0
    policy_tags: list[str] = Field(default_factory=list)
    trace_id: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")


class ContextBundle(BaseModel):
    """Unified LLM input assembly product (M15 Context Manager)."""

    thread_id: str
    run_id: str | None = None
    user_message: str = ""
    checkpoint_messages: list[dict[str, Any]] = Field(default_factory=list)
    retrieved_context: list[dict[str, Any]] = Field(default_factory=list)
    memory_injections: list[dict[str, Any]] = Field(default_factory=list)
    skill_injections: list[dict[str, Any]] = Field(default_factory=list)
    scenario_prompts: list[str] = Field(default_factory=list)
    enabled_tool_schemas: list[dict[str, Any]] = Field(default_factory=list)
    policy_hints: list[dict[str, Any]] = Field(default_factory=list)
    context_blocks: list[ContextBlock] = Field(default_factory=list)
    budget: dict[str, Any] = Field(default_factory=dict)
    truncation_report: dict[str, Any] = Field(default_factory=dict)
    graph_messages: list[Any] = Field(default_factory=list, exclude=True)

    model_config = ConfigDict(extra="forbid")
