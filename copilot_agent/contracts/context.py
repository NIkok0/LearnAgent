from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ContextBundle(BaseModel):
    """Unified LLM input assembly product (M15 Context Manager)."""

    thread_id: str
    run_id: str | None = None
    user_message: str = ""
    checkpoint_messages: list[dict[str, Any]] = Field(default_factory=list)
    retrieved_context: list[dict[str, Any]] = Field(default_factory=list)
    memory_injections: list[dict[str, Any]] = Field(default_factory=list)
    scenario_prompts: list[str] = Field(default_factory=list)
    enabled_tool_schemas: list[dict[str, Any]] = Field(default_factory=list)
    policy_hints: list[dict[str, Any]] = Field(default_factory=list)
    budget: dict[str, Any] = Field(default_factory=dict)
    truncation_report: dict[str, Any] = Field(default_factory=dict)
    graph_messages: list[Any] = Field(default_factory=list, exclude=True)

    model_config = ConfigDict(extra="forbid")
