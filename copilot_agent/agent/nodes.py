from __future__ import annotations



from typing import Any



from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage

from langchain_core.runnables import RunnableConfig

from langgraph.types import interrupt



from copilot_agent.agent.message_utils import last_user_content

from copilot_agent.context import ContextManager

from copilot_agent.scenario.router.types import ToolRoute, tool_allowed, tool_route_from_mapping

from copilot_agent.llm import LLMProvider

from copilot_agent.memory import MemoryManager

from copilot_agent.policy import PolicyRegistry

from copilot_agent.runtime.event_schema import EVENT_CREDENTIAL_BINDING_AUDIT

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

        context_manager: ContextManager,

    ) -> None:

        self._memory = memory

        self._llm_provider = llm_provider

        self._policy = policy

        self._tool_registry = tool_registry

        self._tools = tools

        self._context = context_manager



    async def planner(self, _state, config: RunnableConfig) -> dict[str, Any]:

        ctx = (config.get("configurable") or {}) if config else {}

        thread_id = str(ctx.get("conversation_id") or ctx.get("thread_id") or "")

        run_id = str(ctx.get("run_id") or "")

        messages = ctx.get("input_messages") if isinstance(ctx.get("input_messages"), list) else []

        goal = last_user_content(messages)

        confirm_dangerous = bool(ctx.get("confirm_dangerous", False))

        allow_job_post = bool(ctx.get("allow_job_post", settings.copilot_allow_job_post))

        route = tool_route_from_mapping(ctx.get("tool_route"))
        if route is None:
            route = self._context.plan_route(
                goal,
                confirm_dangerous=confirm_dangerous,
                allow_job_post=allow_job_post,
            )

        self._memory.append_event(

            thread_id,

            run_id or None,

            "plan_created",

            self._context.plan_created_payload(goal=goal, route=route),

        )

        return {"tool_route": route.as_dict()}



    async def assistant(self, state, config: RunnableConfig) -> dict[str, list[BaseMessage]]:

        messages = list(state.get("messages", []))

        ctx = (config.get("configurable") or {}) if config else {}

        input_messages = ctx.get("input_messages") if isinstance(ctx.get("input_messages"), list) else []

        question = last_user_content(input_messages)

        route_kind = str((state.get("tool_route") or {}).get("kind") or "")

        injected = self._context.build_assistant_injections(

            route_kind=route_kind,

            graph_messages=messages,

            question=question,

        )

        llm = self._llm_provider.get_tool_bound_model(self._tools)

        ai = await llm.ainvoke(messages + injected)

        return {"messages": injected + [ai]}



    async def safety_gate(self, state, config: RunnableConfig) -> dict[str, list[BaseMessage]]:

        msgs = state.get("messages", [])

        if not msgs:

            return {}

        last = msgs[-1]

        if not isinstance(last, AIMessage) or not last.tool_calls:

            return {}

        ctx = (config.get("configurable") or {}) if config else {}

        thread_id = str(ctx.get("conversation_id") or ctx.get("thread_id") or "")

        run_id = str(ctx.get("run_id") or "")

        allow_job_post = bool(ctx.get("allow_job_post", settings.copilot_allow_job_post))

        confirm_dangerous = bool(ctx.get("confirm_dangerous", False))

        decision = self._policy.evaluate_tool_calls(

            list(last.tool_calls),

            allow_job_post=allow_job_post,

            confirm_dangerous=confirm_dangerous,

        )

        if decision.credential_audits and thread_id and run_id:

            for audit in decision.credential_audits:

                self._memory.append_event(thread_id, run_id, EVENT_CREDENTIAL_BINDING_AUDIT, audit)

        if not decision.allowed:

            return {"messages": [AIMessage(content=decision.message)]}



        if settings.agent_tool_route_enforce:

            route_data = state.get("tool_route") or {}

            if route_data:

                route = ToolRoute(

                    kind=route_data.get("kind", "knowledge"),  # type: ignore[arg-type]

                    recommended_tools=tuple(route_data.get("recommended_tools") or ()),

                    forbidden_tools=tuple(route_data.get("forbidden_tools") or ()),

                    suggested_paths=tuple(route_data.get("suggested_paths") or ()),

                    rationale=str(route_data.get("rationale") or ""),

                )

                blocked = [

                    str(call.get("name", ""))

                    for call in last.tool_calls

                    if not tool_allowed(route, str(call.get("name", "")))

                ]

                if blocked:

                    return {

                        "messages": [

                            AIMessage(

                                content=(

                                    f"Tool routing blocked {', '.join(blocked)} for intent '{route.kind}'. "

                                    f"Recommended: {' -> '.join(route.recommended_tools) or 'no tools'}. "

                                    f"{route.rationale}"

                                )

                            )

                        ]

                    }

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


