from __future__ import annotations

from typing import Any

from langchain_core.messages import BaseMessage, SystemMessage

from copilot_agent.agent.diagnosis import build_diagnosis_outline
from copilot_agent.context.assemble import build_graph_turn_messages
from copilot_agent.context.checkpoint_pack import pack_checkpoint_for_budget, total_message_chars
from copilot_agent.context.events import build_context_built_payload
from copilot_agent.context.preretrieval_dedupe import build_preretrieval_cache
from copilot_agent.context.memory_inject import memory_context_messages
from copilot_agent.context.packing import pack_graph_messages
from copilot_agent.context.preretrieval import preretrieve_docs
from copilot_agent.contracts.adapters.tool_rag import RagSearchAdapter
from copilot_agent.contracts.context import ContextBundle
from copilot_agent.memory import MemoryManager
from copilot_agent.runtime.event_schema import EVENT_CONTEXT_BUILT, EVENT_RETRIEVAL_COMPLETED
from copilot_agent.scenario.loader import LoadedScenario
from copilot_agent.scenario.router import RouterEngine
from copilot_agent.scenario.router.types import ToolRoute, build_route_system_message
from copilot_agent.settings import settings
from copilot_agent.tools.registry import ToolRegistry


class ContextManager:
    """Kernel M15: single entry for per-turn LLM context assembly (memory, router, RAG, packing)."""

    def __init__(
        self,
        *,
        scenario: LoadedScenario,
        memory: MemoryManager,
        tool_registry: ToolRegistry,
        router_engine: RouterEngine | None = None,
        graph: Any | None = None,
    ) -> None:
        self._scenario = scenario
        self._memory = memory
        self._tool_registry = tool_registry
        self._router = router_engine or scenario.router_engine
        self._graph = graph
        self._system_prompt = scenario.system_prompt

    def bind_graph(self, graph: Any) -> None:
        self._graph = graph

    @property
    def router(self) -> RouterEngine:
        return self._router

    def _budget_max_chars(self) -> int:
        budgets = self._scenario.budgets
        return int(budgets.max_context_chars or budgets.max_context_tokens or settings.rag_context_budget_chars)

    def plan_route(
        self,
        goal: str,
        *,
        confirm_dangerous: bool = False,
        allow_job_post: bool | None = None,
    ) -> ToolRoute:
        allow = settings.copilot_allow_job_post if allow_job_post is None else allow_job_post
        return self._router.route(
            goal,
            confirm_dangerous=confirm_dangerous,
            allow_job_post=allow,
        )

    def route_system_message(self, route: ToolRoute) -> SystemMessage | None:
        if not settings.agent_tool_route_enabled:
            return None
        return SystemMessage(content=build_route_system_message(route))

    def build_assistant_injections(
        self,
        *,
        route_kind: str,
        graph_messages: list[BaseMessage],
        question: str,
    ) -> list[SystemMessage]:
        if not settings.agent_diagnosis_template_enabled:
            return []
        outline = build_diagnosis_outline(
            route_kind=route_kind,
            messages=graph_messages,
            question=question,
        )
        if outline is None:
            return []
        return [SystemMessage(content=outline.to_system_message())]

    def _memory_inject_chars(self, memory_dict: dict[str, Any]) -> int:
        total = 0
        for message in memory_context_messages(memory_dict):
            total += len(str(message.content or ""))
        return total

    def _emit_context_built(
        self,
        *,
        thread_id: str,
        run_id: str | None,
        goal: str,
        bundle: ContextBundle,
    ) -> None:
        if not settings.context_emit_built_event or not run_id:
            return
        payload = build_context_built_payload(
            user_message=goal,
            assembled_message_count=int(bundle.truncation_report.get("assembled_message_count") or 0),
            budget_max_chars=int(bundle.budget.get("max_context_chars") or 0),
            used_chars=int(bundle.truncation_report.get("used_chars") or 0),
            truncated=bool(bundle.truncation_report.get("truncated")),
            truncation_steps=list(bundle.truncation_report.get("truncation_steps") or []),
            router_injected=bool(bundle.truncation_report.get("router_injected")),
            preretrieval_enabled=bool(bundle.truncation_report.get("preretrieval_enabled")),
            preretrieval_sources=[
                str(item.get("source", ""))
                for item in bundle.retrieved_context
                if isinstance(item, dict) and item.get("source")
            ],
            preretrieval_excerpt_chars=int(bundle.truncation_report.get("preretrieval_excerpt_chars") or 0),
            memory_inject_chars=int(bundle.truncation_report.get("memory_inject_chars") or 0),
            checkpoint_compacted=bool((bundle.truncation_report.get("checkpoint_pack") or {}).get("compacted")),
            checkpoint_chars=int((bundle.truncation_report.get("checkpoint_pack") or {}).get("checkpoint_chars") or 0),
        )
        self._memory.append_event(thread_id, run_id, EVENT_CONTEXT_BUILT, payload)

    async def assemble(
        self,
        *,
        thread_id: str,
        run_id: str | None,
        turn_messages: list[BaseMessage],
        goal: str,
        confirm_dangerous: bool = False,
        allow_job_post: bool | None = None,
    ) -> ContextBundle:
        if self._graph is None:
            raise RuntimeError("ContextManager.bind_graph() must be called before assemble()")

        budget_max = self._budget_max_chars()
        memory_context = self._memory.build_context(
            thread_id=thread_id,
            run_id=run_id,
            messages=[{"role": "user", "content": goal}] if goal else [],
            goal=goal,
        )
        memory_dict = memory_context.as_dict()
        policy = self._memory.policy
        memory_inject_chars = self._memory_inject_chars(memory_dict)

        route = self.plan_route(goal, confirm_dangerous=confirm_dangerous, allow_job_post=allow_job_post)
        route_message = self.route_system_message(route)

        hits, rag_message, retrieved_context, pr_meta = preretrieve_docs(
            self._memory,
            query=goal,
            route=route,
            budget_chars=budget_max,
            thread_id=thread_id,
        )
        if hits and run_id and settings.context_emit_built_event:
            self._memory.append_event(
                thread_id,
                run_id,
                EVENT_RETRIEVAL_COMPLETED,
                RagSearchAdapter.to_retrieval_completed_payload(
                    goal,
                    hits,
                    excerpt_chars=int(pr_meta.get("excerpt_chars") or 0),
                    retrieval_mode="preretrieval",
                    retrieval_route={"kind": route.kind, "phase": "context_assemble"},
                    policy_result=pr_meta.get("policy_result"),
                    context_guard=pr_meta.get("context_guard") if isinstance(pr_meta.get("context_guard"), dict) else None,
                ),
            )

        extra_system: list[SystemMessage] = []
        if route_message is not None:
            extra_system.append(route_message)
        if rag_message is not None:
            extra_system.append(rag_message)

        graph_messages = await build_graph_turn_messages(
            graph=self._graph,
            thread_id=thread_id,
            system_prompt=self._system_prompt,
            memory_context=memory_dict,
            turn_messages=turn_messages,
            policy=policy,
            extra_system_messages=extra_system,
        )

        new_turn_chars = total_message_chars(graph_messages)
        checkpoint_pack = await pack_checkpoint_for_budget(
            self._graph,
            thread_id,
            max_total_chars=budget_max,
            new_turn_chars=new_turn_chars,
            policy=policy,
        )
        remaining_budget = max(0, budget_max - int(checkpoint_pack.get("checkpoint_chars") or 0))

        packed = pack_graph_messages(
            graph_messages,
            max_chars=remaining_budget,
            enabled=settings.context_packing_enabled,
        )
        truncation_steps = list(checkpoint_pack.get("truncation_steps") or []) + list(packed.steps)
        preretrieval_cache = build_preretrieval_cache(query=goal, hits=hits) if hits else None

        episodic = memory_dict.get("episodic") if isinstance(memory_dict.get("episodic"), dict) else {}
        long_term = memory_dict.get("long_term") if isinstance(memory_dict.get("long_term"), dict) else {}

        bundle = ContextBundle(
            thread_id=thread_id,
            run_id=run_id,
            user_message=goal,
            memory_injections=[
                {"kind": "episodic", "preview_chars": len(str(episodic.get("inject_preview") or ""))},
                {"kind": "long_term", "items": len(long_term.get("items") or [])},
            ],
            retrieved_context=retrieved_context,
            scenario_prompts=[self._system_prompt] if self._system_prompt else [],
            enabled_tool_schemas=self._tool_registry.public_specs(),
            policy_hints=[
                {"tool_allowlist": self._scenario.policy.tool_allowlist},
                {"tool_route": route.as_dict()},
            ],
            budget={
                "max_context_chars": budget_max,
                "max_graph_rounds": self._scenario.budgets.max_graph_rounds,
                "max_tool_calls": self._scenario.budgets.max_tool_calls,
            },
            truncation_report={
                "assembled_message_count": len(packed.messages),
                "used_chars": packed.used_chars + int(checkpoint_pack.get("checkpoint_chars") or 0),
                "truncated": bool(truncation_steps),
                "truncation_steps": truncation_steps,
                "router_injected": route_message is not None,
                "preretrieval_enabled": bool(pr_meta.get("enabled")),
                "preretrieval_excerpt_chars": int(pr_meta.get("excerpt_chars") or 0),
                "preretrieval_cache": preretrieval_cache,
                "memory_inject_chars": memory_inject_chars,
                "checkpoint_pack": checkpoint_pack,
            },
            graph_messages=packed.messages,
        )
        self._emit_context_built(thread_id=thread_id, run_id=run_id, goal=goal, bundle=bundle)
        return bundle

    def plan_created_payload(
        self,
        *,
        goal: str,
        route: ToolRoute,
    ) -> dict[str, object]:
        if settings.agent_tool_route_enabled:
            return {
                "goal": goal,
                "strategy": "tool_grounded_react",
                "tool_route": route.as_dict(),
                "available_tools": self._tool_registry.public_specs(),
            }
        return {
            "goal": goal,
            "strategy": "react_with_safety_gate",
            "available_tools": self._tool_registry.public_specs(),
        }
