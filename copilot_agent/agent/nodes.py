from __future__ import annotations



from typing import Any



from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage

from langchain_core.runnables import RunnableConfig

from langgraph.types import interrupt



from copilot_agent.agent.plan_builder import (
    build_plan_from_route,
    maybe_replan_troubleshooting,
    update_plan_outcomes,
)
from copilot_agent.agent.message_utils import last_user_content
from copilot_agent.agent.tool_route_merge import (
    extract_suggested_paths_from_messages,
    merge_api_paths_into_route,
)
from copilot_agent.context import ContextManager
from copilot_agent.context.assemble import _existing_system_contents
from copilot_agent.scenario.router.types import ToolRoute, build_route_system_message, tool_allowed, tool_route_from_mapping

from copilot_agent.llm import LLMProvider

from copilot_agent.memory import MemoryManager

from copilot_agent.policy import PolicyRegistry

from copilot_agent.contracts.plan import PlanModel
from copilot_agent.runtime.event_schema import (
    EVENT_CREDENTIAL_BINDING_AUDIT,
    EVENT_PLAN_UPDATED,
    EVENT_TOOL_SIDE_EFFECT_RECORDED,
)

from copilot_agent.settings import settings

from copilot_agent.tools.audit import build_blocked_tool_side_effect_payload, sanitize_tool_payload

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
            route = await self._context.resolve_route(
                goal,
                confirm_dangerous=confirm_dangerous,
                allow_job_post=allow_job_post,
            )

        plan = build_plan_from_route(route, goal=goal)

        plan_payload = self._context.plan_created_payload(goal=goal, route=route)
        plan_payload["plan"] = plan.as_dict()

        self._memory.append_event(

            thread_id,

            run_id or None,

            "plan_created",

            plan_payload,

        )

        return {"tool_route": route.as_dict(), "plan": plan.as_dict()}



    async def assistant(self, state, config: RunnableConfig) -> dict[str, list[BaseMessage]]:

        messages = list(state.get("messages", []))

        ctx = (config.get("configurable") or {}) if config else {}

        thread_id = str(ctx.get("conversation_id") or ctx.get("thread_id") or "")

        run_id = str(ctx.get("run_id") or "")

        input_messages = ctx.get("input_messages") if isinstance(ctx.get("input_messages"), list) else []

        question = last_user_content(input_messages)

        route_data = state.get("tool_route") or ctx.get("tool_route") or {}
        route = tool_route_from_mapping(route_data)
        state_updates: dict[str, Any] = {}
        plan_data = state.get("plan") if isinstance(state.get("plan"), dict) else {}
        plan = PlanModel.model_validate(plan_data) if plan_data else None

        if route is not None and settings.agent_retrieval_path_inject:
            api_paths = extract_suggested_paths_from_messages(messages)
            merged_route, changed = merge_api_paths_into_route(route, api_paths)
            if changed:
                route = merged_route
                state_updates["tool_route"] = route.as_dict()
                if thread_id and run_id:
                    self._memory.append_event(
                        thread_id,
                        run_id,
                        EVENT_PLAN_UPDATED,
                        {
                            "update_reason": "path_merge",
                            "tool_route": route.as_dict(),
                            "merged_paths": list(api_paths),
                            "plan": plan.as_dict() if plan is not None else {},
                        },
                    )

        route_kind = str((route.kind if route is not None else route_data.get("kind")) or "")

        if plan is not None:
            updated_plan = update_plan_outcomes(plan, messages)
            replanned = maybe_replan_troubleshooting(updated_plan, route_kind, messages)
            if replanned is not None and replanned.model_dump() != updated_plan.model_dump():
                updated_plan = replanned
                if thread_id and run_id:
                    self._memory.append_event(
                        thread_id,
                        run_id,
                        EVENT_PLAN_UPDATED,
                        {
                            "update_reason": "replan",
                            "route_kind": route_kind,
                            "plan": updated_plan.as_dict(),
                        },
                    )
            if updated_plan.model_dump() != plan.model_dump():
                state_updates["plan"] = updated_plan.as_dict()
                plan = updated_plan

        injected = self._context.build_assistant_injections(

            route_kind=route_kind,

            graph_messages=messages,

            question=question,

        )

        if route is not None and state_updates.get("tool_route"):
            route_message = SystemMessage(content=build_route_system_message(route))
            existing = _existing_system_contents(messages)
            if str(route_message.content or "").strip() not in existing:
                injected = [route_message, *injected]

        existing = _existing_system_contents(messages)
        injected = [
            message
            for message in injected
            if str(getattr(message, "content", "") or "").strip() not in existing
        ]

        llm = self._llm_provider.get_tool_bound_model(self._tools)

        ai = await llm.ainvoke(messages + injected)

        result: dict[str, Any] = {"messages": injected + [ai]}
        result.update(state_updates)
        return result



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
            self._append_blocked_side_effects(
                thread_id=thread_id,
                run_id=run_id,
                tool_calls=list(last.tool_calls),
                reason=decision.reason or "policy_blocked",
                policy_source=decision.policy_source,
            )

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
                    self._append_blocked_side_effects(
                        thread_id=thread_id,
                        run_id=run_id,
                        tool_calls=[
                            call
                            for call in last.tool_calls
                            if str(call.get("name", "")) in blocked
                        ],
                        reason="policy_blocked",
                        policy_source="tool_route_policy",
                    )

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

    def _append_blocked_side_effects(
        self,
        *,
        thread_id: str,
        run_id: str,
        tool_calls: list[dict[str, Any]],
        reason: str,
        policy_source: str,
    ) -> None:
        if not thread_id or not run_id:
            return
        existing_call_ids = {
            str((event.get("payload") or {}).get("call_id") or "")
            for event in self._memory.get_thread_events(thread_id, run_id=run_id)
            if event.get("type") == EVENT_TOOL_SIDE_EFFECT_RECORDED
        }
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            call_id = str(call.get("id") or call.get("call_id") or "").strip()
            if call_id and call_id in existing_call_ids:
                continue
            payload = build_blocked_tool_side_effect_payload(
                tool_call=call,
                reason=reason or "policy_blocked",
                policy_source=policy_source or "policy",
                requires_approval=_requires_approval_for(self._tool_registry, call),
            )
            if payload is None:
                continue
            self._memory.append_event(thread_id, run_id, EVENT_TOOL_SIDE_EFFECT_RECORDED, payload)


def _requires_approval_for(registry: ToolRegistry, call: dict[str, Any]) -> bool:
    name = str(call.get("name") or "")
    args = call.get("args") if isinstance(call.get("args"), dict) else {}
    spec = registry.get_spec(name)
    if spec is None:
        return name == "http_post"
    return bool(spec.requires_approval_for(args))


