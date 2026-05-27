from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.types import Command

from copilot_agent.agent.graph import build_agent_graph, close_graph_checkpointer
from copilot_agent.agent.nodes import AgentNodes
from copilot_agent.agent.message_utils import current_turn_messages, last_user_content
from copilot_agent.agent.stream.event_mapper import GraphEventMapper
from copilot_agent.context import ContextManager
from copilot_agent.contracts.adapters.event_store import EventStoreAdapter
from copilot_agent.contracts.adapters.sse import SseAdapter
from copilot_agent.contracts.base import RuntimeEvent
from copilot_agent.kernel import KernelDeps, build_kernel_components
from copilot_agent.memory.checkpoint_compactor import CheckpointCompactor
from copilot_agent.memory.manager import CHECKPOINT_COMPACTED_EVENT
from copilot_agent.observability import (
    end_chat_trace,
    flush_observability,
    observability_trace_metadata,
    resolve_observability_trace_id,
    start_chat_trace,
)
from copilot_agent.rag import RagStore
from copilot_agent.runtime.checkpoint_reader import CheckpointReader
from copilot_agent.runtime.event_store import EventStore
from copilot_agent.scenario import load_scenario
from copilot_agent.scenario.loader import LoadedScenario
from copilot_agent.settings import settings
from copilot_agent.tools.extensions.mcp import McpRuntime
from copilot_agent.credentials import CredentialManager
from copilot_agent.rag.request_context import merge_retrieval_scopes

log = logging.getLogger(__name__)


class ChatRunner:
    """Kernel orchestration entry: Run loop + LangGraph, built on K/C/S layers."""

    def __init__(
        self,
        rag_store: RagStore,
        credential_manager: CredentialManager,
        event_store: EventStore | None = None,
        scenario: LoadedScenario | None = None,
        mcp_runtime: McpRuntime | None = None,
        *,
        kernel_deps: KernelDeps | None = None,
    ) -> None:
        scenario = scenario or load_scenario(
            settings.scenario,
            scenarios_root_path=settings.scenarios_root or None,
        )
        deps = kernel_deps or KernelDeps(
            rag_store=rag_store,
            credential_manager=credential_manager,
            event_store=event_store,
            mcp_runtime=mcp_runtime,
        )
        kernel = build_kernel_components(scenario, deps)

        self._scenario = kernel.scenario
        self._credential_manager = deps.credential_manager
        self._mcp_runtime = kernel.mcp_runtime
        self._max_rounds = kernel.scenario.budgets.max_graph_rounds
        self._memory = kernel.memory
        self._llm_provider = kernel.llm_provider
        self._tool_registry = kernel.tool_registry
        self._policy = kernel.policy
        self._tools = kernel.tools

        self._context_manager = ContextManager(
            scenario=kernel.scenario,
            memory=kernel.memory,
            tool_registry=kernel.tool_registry,
            router_engine=kernel.scenario.router_engine,
            credential_manager=deps.credential_manager,
        )
        self._nodes = AgentNodes(
            memory=kernel.memory,
            llm_provider=kernel.llm_provider,
            policy=kernel.policy,
            tool_registry=kernel.tool_registry,
            tools=self._tools,
            context_manager=self._context_manager,
        )
        self._graph = build_agent_graph(
            self._nodes.planner,
            self._nodes.assistant,
            self._nodes.safety_gate,
            self._tools,
            checkpoint_path=kernel.memory.checkpoint_path,
            async_checkpoint=True,
        )
        self._context_manager.bind_graph(self._graph)
        self._mapper = GraphEventMapper(
            memory=kernel.memory,
            tool_registry=kernel.tool_registry,
            checkpoint_reader=CheckpointReader(self._graph),
        )
        self._checkpoint_compactor = CheckpointCompactor(self._graph, policy=kernel.memory.policy)

    @property
    def scenario(self) -> LoadedScenario:
        return self._scenario

    @property
    def memory(self):
        return self._memory

    @property
    def context_manager(self) -> ContextManager:
        return self._context_manager

    @property
    def graph(self):
        return self._graph

    async def preview_context(
        self,
        *,
        thread_id: str,
        messages: list[dict[str, Any]],
        confirm_dangerous: bool = False,
    ) -> dict[str, Any]:
        turn_messages = current_turn_messages(messages)
        goal = last_user_content(turn_messages)
        bundle = await self._context_manager.preview(
            thread_id=thread_id,
            turn_messages=self._to_lc_messages(turn_messages),
            goal=goal,
            confirm_dangerous=confirm_dangerous,
            allow_job_post=settings.copilot_allow_job_post,
        )
        return {
            **bundle.model_dump(exclude={"graph_messages"}),
            "graph_messages_preview": [
                {
                    "type": message.__class__.__name__,
                    "content": str(getattr(message, "content", "") or ""),
                }
                for message in bundle.graph_messages
            ],
        }

    async def run_stream(
        self,
        *,
        conversation_id: str,
        run_id: str | None = None,
        messages: list[dict[str, Any]],
        confirm_dangerous: bool,
        resume: bool | None = None,
    ) -> AsyncIterator[str]:
        trace = start_chat_trace(
            conversation_id=conversation_id,
            run_id=run_id,
            messages=messages,
            confirm_dangerous=confirm_dangerous,
            model=settings.openai_model,
        )
        trace_id = resolve_observability_trace_id(trace, thread_id=conversation_id, run_id=run_id)
        try:
            turn_messages = current_turn_messages(messages)
            goal = last_user_content(turn_messages)
            if resume is None:
                lc_turn = self._to_lc_messages(turn_messages)
                bundle = await self._context_manager.assemble(
                    thread_id=conversation_id,
                    run_id=run_id,
                    turn_messages=lc_turn,
                    goal=goal,
                    confirm_dangerous=confirm_dangerous,
                    allow_job_post=settings.copilot_allow_job_post,
                )
                graph_input = {"messages": bundle.graph_messages}
                preretrieval_cache = bundle.truncation_report.get("preretrieval_cache")
                tool_route = next(
                    (
                        hint.get("tool_route")
                        for hint in bundle.policy_hints
                        if isinstance(hint, dict) and hint.get("tool_route")
                    ),
                    None,
                )
            else:
                graph_input = Command(resume=resume)
                preretrieval_cache = None
                tool_route = None
            user_id = self._memory.resolve_user_id(conversation_id)
            allowed_scopes = merge_retrieval_scopes(
                credential_manager=self._credential_manager,
                scenario=self._scenario,
                user_id=user_id,
            )
            graph_config = {
                "recursion_limit": (self._max_rounds * 2) + 4,
                "configurable": {
                    "thread_id": conversation_id,
                    "conversation_id": conversation_id,
                    "run_id": run_id,
                    "input_messages": turn_messages,
                    "confirm_dangerous": confirm_dangerous,
                    "allow_job_post": settings.copilot_allow_job_post,
                    "preretrieval_cache": preretrieval_cache,
                    "tool_route": tool_route,
                    "trace": trace,
                    "trace_id": trace_id,
                    **observability_trace_metadata(trace),
                    "tenant_id": self._scenario.resources.default_tenant_id,
                    "max_classification": self._scenario.resources.default_max_classification,
                    "allowed_scopes": allowed_scopes,
                },
            }
            last_output = ""
            async for runtime_event in self._mapper.map(
                graph=self._graph,
                graph_input=graph_input,
                graph_config=graph_config,
                thread_id=conversation_id,
                run_id=run_id,
            ):
                if not runtime_event.correlation.trace_id:
                    runtime_event = runtime_event.model_copy(
                        update={
                            "correlation": runtime_event.correlation.model_copy(
                                update={"trace_id": trace_id}
                            )
                        }
                    )
                if runtime_event.kind == "token":
                    text = runtime_event.content or str(runtime_event.data.get("text", ""))
                    last_output += str(text)
                if not runtime_event.correlation.thread_id:
                    runtime_event = runtime_event.model_copy(
                        update={
                            "correlation": runtime_event.correlation.model_copy(
                                update={"thread_id": conversation_id, "run_id": run_id}
                            )
                        }
                    )
                yield self._emit(runtime_event)
            end_chat_trace(trace, output_preview=last_output)
        except Exception as e:
            end_chat_trace(trace, error=str(e))
            raise
        finally:
            flush_observability()

    def _emit(self, event: RuntimeEvent) -> str:
        EventStoreAdapter.append_memory(self._memory, event)
        return SseAdapter.encode(event)

    def finalize_memory(self, thread_id: str, run_id: str, *, messages: list[dict[str, Any]] | None = None) -> None:
        turn_messages = current_turn_messages(messages or [])
        fallback_goal = last_user_content(turn_messages)
        self._memory.summarize_run(thread_id, run_id, fallback_goal=fallback_goal)
        self._memory.update_thread_summary(thread_id, run_id)

    async def compact_checkpoint(self, thread_id: str, *, run_id: str | None = None) -> dict[str, Any]:
        result = await self._checkpoint_compactor.compact_if_needed(thread_id)
        if result.get("compacted") and settings.memory_emit_checkpoint_compacted:
            effective_run_id = run_id
            if not effective_run_id and self._memory.event_store is not None:
                effective_run_id = self._memory.event_store.latest_run_id(thread_id)
            if effective_run_id:
                payload = {
                    **result,
                    "checkpoint_path": self._memory.checkpoint_path,
                }
                self._memory.append_event(thread_id, effective_run_id, CHECKPOINT_COMPACTED_EVENT, payload)
        return result

    async def aclose(self) -> None:
        if self._mcp_runtime is not None:
            await self._mcp_runtime.aclose()
        await close_graph_checkpointer(self._graph)

    def _to_lc_messages(self, messages: list[dict[str, Any]]) -> list[BaseMessage]:
        out: list[BaseMessage] = []
        for message in messages:
            role = str(message.get("role", "")).lower()
            content = str(message.get("content", ""))
            if role == "user":
                out.append(HumanMessage(content=content))
            elif role == "assistant":
                additional_kwargs: dict[str, Any] = {}
                reasoning_content = message.get("reasoning_content")
                if reasoning_content:
                    additional_kwargs["reasoning_content"] = str(reasoning_content)
                out.append(AIMessage(content=content, additional_kwargs=additional_kwargs))
            elif role == "system":
                out.append(SystemMessage(content=content))
            else:
                out.append(HumanMessage(content=content))
        return out
