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
from copilot_agent.credentials import CredentialManager
from copilot_agent.memory import MemoryManager
from copilot_agent.runtime.event_schema import (
    EVENT_CONTEXT_BUILT,
    EVENT_POLICY_DECISION_RECORDED,
    EVENT_RETRIEVAL_COMPLETED,
)
from copilot_agent.runtime.policy_audit import build_rag_policy_decision_payloads
from copilot_agent.scenario.loader import LoadedScenario
from copilot_agent.scenario.router import RouterEngine
from copilot_agent.scenario.router.types import ToolRoute, build_route_system_message
from copilot_agent.rag.request_context import retrieval_defaults_from_scenario
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
        credential_manager: CredentialManager | None = None,
    ) -> None:
        self._scenario = scenario
        self._memory = memory
        self._tool_registry = tool_registry
        self._router = router_engine or scenario.router_engine
        self._graph = graph
        self._system_prompt = scenario.system_prompt
        self._credential_manager = credential_manager

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
        return self._router.route_detailed(
            goal,
            confirm_dangerous=confirm_dangerous,
            allow_job_post=allow,
        ).route

    async def resolve_route(
        self,
        goal: str,
        *,
        confirm_dangerous: bool = False,
        allow_job_post: bool | None = None,
        classifier: Any | None = None,
    ) -> ToolRoute:
        allow = settings.copilot_allow_job_post if allow_job_post is None else allow_job_post
        decision = self._router.route_detailed(
            goal,
            confirm_dangerous=confirm_dangerous,
            allow_job_post=allow,
        )
        route = decision.route
        if settings.agent_tool_route_llm_fallback and decision.used_defaults:
            from copilot_agent.scenario.router.llm_fallback import refine_route_with_llm

            route = await refine_route_with_llm(goal, route, classifier=classifier)
        return route

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
        emit_events: bool = True,
        persist_checkpoint_pack: bool = True,
        record_memory_access: bool = True,
    ) -> ContextBundle:
        if self._graph is None:
            raise RuntimeError("ContextManager.bind_graph() must be called before assemble()")

        route = await self.resolve_route(goal, confirm_dangerous=confirm_dangerous, allow_job_post=allow_job_post)
        route_message = self.route_system_message(route)
        route_context = {
            "kind": route.kind,
            "recommended_tools": list(route.recommended_tools),
            "forbidden_tools": list(route.forbidden_tools),
        }

        budget_max = self._budget_max_chars()
        memory_context = self._memory.build_context(
            thread_id=thread_id,
            run_id=run_id,
            messages=[{"role": "user", "content": goal}] if goal else [],
            goal=goal,
            record_memory_access=record_memory_access,
            route_context=route_context,
        )
        memory_dict = memory_context.as_dict()
        policy = self._memory.policy
        memory_inject_chars = self._memory_inject_chars(memory_dict)

        hits, rag_message, retrieved_context, pr_meta = preretrieve_docs(
            self._memory,
            query=goal,
            route=route,
            budget_chars=budget_max,
            thread_id=thread_id,
            retrieval_defaults=retrieval_defaults_from_scenario(
                self._scenario,
                credential_manager=self._credential_manager,
                thread_id=thread_id,
                user_id=self._memory.resolve_user_id(thread_id) if thread_id else "",
            ),
        )
        if emit_events and hits and run_id and settings.context_emit_built_event:
            retrieval_payload = RagSearchAdapter.to_retrieval_completed_payload(
                goal,
                hits,
                excerpt_chars=int(pr_meta.get("excerpt_chars") or 0),
                retrieval_mode="preretrieval",
                retrieval_route={"kind": route.kind, "phase": "context_assemble"},
                policy_result=pr_meta.get("policy_result"),
                context_guard=pr_meta.get("context_guard") if isinstance(pr_meta.get("context_guard"), dict) else None,
            )
            event_store = self._memory.event_store
            if event_store is not None:
                retrieval_event = event_store.append_event(thread_id, run_id, EVENT_RETRIEVAL_COMPLETED, retrieval_payload)
                for policy_payload in build_rag_policy_decision_payloads(
                    retrieval_event.get("payload") if isinstance(retrieval_event.get("payload"), dict) else {},
                    related_event_id=int(retrieval_event.get("id") or 0) or None,
                ):
                    self._memory.append_event(thread_id, run_id, EVENT_POLICY_DECISION_RECORDED, policy_payload)
            else:
                self._memory.append_event(thread_id, run_id, EVENT_RETRIEVAL_COMPLETED, retrieval_payload)

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
            persist=persist_checkpoint_pack,
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
        recalled_runs = list(episodic.get("recalled_runs") or [])
        recalled_long_term = list(episodic.get("recalled_long_term") or [])
        dropped_conflicts = list(episodic.get("dropped_conflicts") or [])
        dropped_long_term = list(episodic.get("dropped_long_term") or [])

        bundle = ContextBundle(
            thread_id=thread_id,
            run_id=run_id,
            user_message=goal,
            memory_injections=[
                {
                    "kind": "episodic",
                    "preview_chars": len(str(episodic.get("inject_preview") or "")),
                    "recalled_runs": len(recalled_runs),
                    "dropped_conflicts": len(dropped_conflicts),
                    "dropped_long_term": len(dropped_long_term),
                    "sources": list(episodic.get("sources") or []),
                    "budget": episodic.get("budget_applied") or {},
                },
                {
                    "kind": "long_term",
                    "items": len(recalled_long_term),
                    "pending_excluded": True,
                    "dropped": len(dropped_long_term),
                    "sources": [
                        {
                            "id": item.get("id"),
                            "scope": item.get("scope"),
                            "memory_type": item.get("memory_type"),
                            "score": item.get("score"),
                            "source_run_id": item.get("source_run_id"),
                        }
                        for item in recalled_long_term
                        if isinstance(item, dict)
                    ],
                },
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
        if emit_events:
            self._emit_context_built(thread_id=thread_id, run_id=run_id, goal=goal, bundle=bundle)
        return bundle

    async def preview(
        self,
        *,
        thread_id: str,
        turn_messages: list[BaseMessage],
        goal: str,
        confirm_dangerous: bool = False,
        allow_job_post: bool | None = None,
    ) -> ContextBundle:
        """Assemble a dry-run ContextBundle without RuntimeEvent or checkpoint side effects."""
        bundle = await self.assemble(
            thread_id=thread_id,
            run_id=None,
            turn_messages=turn_messages,
            goal=goal,
            confirm_dangerous=confirm_dangerous,
            allow_job_post=allow_job_post,
            emit_events=False,
            persist_checkpoint_pack=False,
            record_memory_access=False,
        )
        report = dict(bundle.truncation_report)
        report["dry_run"] = True
        report["side_effects"] = {
            "event_store_written": False,
            "checkpoint_persisted": False,
            "tools_executed": False,
            "memory_items_written": False,
        }
        return bundle.model_copy(update={"truncation_report": report})

    def plan_created_payload(
        self,
        *,
        goal: str,
        route: ToolRoute,
    ) -> dict[str, object]:
        if settings.agent_tool_route_enabled:
            return {
                "goal": goal,
                "strategy": "route_first_react",
                "tool_route": route.as_dict(),
                "available_tools": self._tool_registry.public_specs(),
            }
        return {
            "goal": goal,
            "strategy": "react_with_safety_gate",
            "available_tools": self._tool_registry.public_specs(),
        }
