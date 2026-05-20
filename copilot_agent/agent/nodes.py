from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from copilot_agent.agent.message_utils import last_user_content
from copilot_agent.llm import LLMProvider
from copilot_agent.memory import MemoryManager
from copilot_agent.policy import PolicyRegistry
from copilot_agent.settings import settings
from copilot_agent.tools.audit import sanitize_tool_payload
from copilot_agent.tools.registry import ToolRegistry


class AgentNodes:
    def __init__(
        self,
        *,
        memory: MemoryManager,
        llm_provider: LLMProvider,
        policy: PolicyRegistry,
        tool_registry: ToolRegistry,
        tools: list[Any],
    ) -> None:
        self._memory = memory
        self._llm_provider = llm_provider
        self._policy = policy
        self._tool_registry = tool_registry
        self._tools = tools

    async def planner(self, _state, config: RunnableConfig) -> dict[str, list[BaseMessage]]:
        ctx = (config.get("configurable") or {}) if config else {}
        thread_id = str(ctx.get("conversation_id") or ctx.get("thread_id") or "")
        run_id = str(ctx.get("run_id") or "")
        messages = ctx.get("input_messages") if isinstance(ctx.get("input_messages"), list) else []
        goal = last_user_content(messages)
        self._memory.append_event(
            thread_id,
            run_id or None,
            "plan_created",
            {
                "goal": goal,
                "strategy": "react_with_safety_gate",
                "available_tools": self._tool_registry.public_specs(),
            },
        )
        return {}

    async def assistant(self, state) -> dict[str, list[BaseMessage]]:
        llm = self._llm_provider.get_tool_bound_model(self._tools)
        ai = await llm.ainvoke(state["messages"])
        return {"messages": [ai]}

    async def safety_gate(self, state, config: RunnableConfig) -> dict[str, list[BaseMessage]]:
        msgs = state.get("messages", [])
        if not msgs:
            return {}
        last = msgs[-1]
        if not isinstance(last, AIMessage) or not last.tool_calls:
            return {}
        ctx = (config.get("configurable") or {}) if config else {}
        allow_job_post = bool(ctx.get("allow_job_post", settings.copilot_allow_job_post))
        confirm_dangerous = bool(ctx.get("confirm_dangerous", False))
        decision = self._policy.evaluate_tool_calls(
            list(last.tool_calls),
            allow_job_post=allow_job_post,
            confirm_dangerous=confirm_dangerous,
        )
        if not decision.allowed:
            return {"messages": [AIMessage(content=decision.message)]}
        if decision.requires_approval:
            approved = interrupt(
                {
                    "required": True,
                    "reason": decision.reason or "dangerous_tool",
                    "message": decision.message,
                    "tool_calls": [sanitize_tool_payload(call) for call in last.tool_calls],
                }
            )
            if not approved:
                return {"messages": [AIMessage(content="Dangerous tool call was rejected by the user.")]}
        return {}


def memory_context_messages(memory_context: dict[str, Any]) -> list[SystemMessage]:
    episodic = memory_context.get("episodic") or {}
    if not episodic.get("enabled", True):
        return []
    inject_preview = str(episodic.get("inject_preview") or "").strip()
    if not inject_preview:
        return []
    return [SystemMessage(content=inject_preview)]
