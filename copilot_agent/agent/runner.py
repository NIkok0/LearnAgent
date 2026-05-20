from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.types import Command
from pydantic import BaseModel, Field

from copilot_agent.agent.graph import build_agent_graph
from copilot_agent.agent.nodes import AgentNodes, memory_context_messages
from copilot_agent.agent.prompts import DANGEROUS_JOB_PATH, MAX_ROUNDS, SYSTEM_PROMPT
from copilot_agent.agent.message_utils import last_user_content
from copilot_agent.agent.stream.event_mapper import GraphEventMapper
from copilot_agent.agent.stream.sse import format_sse
from copilot_agent.agent.tool_handlers import ToolHandlers
from copilot_agent.conversation_store import ConversationCookieStore
from copilot_agent.llm import LLMProvider
from copilot_agent.memory import MemoryManager
from copilot_agent.observability import end_chat_trace, flush_langfuse, start_chat_trace
from copilot_agent.policy import PolicyRegistry
from copilot_agent.rag import RagStore
from copilot_agent.runtime.checkpoint_reader import CheckpointReader
from copilot_agent.runtime.event_store import EventStore
from copilot_agent.settings import settings
from copilot_agent.tools.http_tools import WatermarkHttpTools
from copilot_agent.tools.registry import ToolRegistry

log = logging.getLogger(__name__)


class SearchDocsArgs(BaseModel):
    query: str = Field(description="Natural language or keywords")


class HttpGetArgs(BaseModel):
    path: str = Field(description="Path starting with /api/v1/ or /actuator/health")
    cookie_header: Optional[str] = Field(default=None, description="Optional Cookie header")


class HttpPostArgs(BaseModel):
    path: str
    json_body: dict[str, Any]
    cookie_header: Optional[str] = None
    idempotency_key: Optional[str] = None


class ChatRunner:
    def __init__(
        self,
        rag_store: RagStore,
        cookie_store: ConversationCookieStore,
        event_store: EventStore | None = None,
        http: Optional[WatermarkHttpTools] = None,
        memory: MemoryManager | None = None,
        llm_provider: LLMProvider | None = None,
        policy_registry: PolicyRegistry | None = None,
    ) -> None:
        self._cookies = cookie_store
        self._memory = memory or MemoryManager(
            rag_store=rag_store,
            event_store=event_store,
            checkpoint_path=settings.agent_checkpoint_path,
        )
        self._http = http or WatermarkHttpTools()
        self._llm_provider = llm_provider or LLMProvider()
        self._tool_handlers = ToolHandlers(memory=self._memory, http=self._http, cookies=self._cookies)
        self._tool_registry = self._build_tool_registry()
        self._policy = policy_registry or PolicyRegistry(self._tool_registry)
        self._tools = self._tool_registry.tools()
        self._nodes = AgentNodes(
            memory=self._memory,
            llm_provider=self._llm_provider,
            policy=self._policy,
            tool_registry=self._tool_registry,
            tools=self._tools,
        )
        self._graph = build_agent_graph(
            self._nodes.planner,
            self._nodes.assistant,
            self._nodes.safety_gate,
            self._tools,
            checkpoint_path=self._memory.checkpoint_path,
            async_checkpoint=True,
        )
        self._mapper = GraphEventMapper(
            memory=self._memory,
            tool_registry=self._tool_registry,
            checkpoint_reader=CheckpointReader(self._graph),
        )

    @property
    def memory(self) -> MemoryManager:
        return self._memory

    @property
    def graph(self):
        return self._graph

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
            messages=messages,
            confirm_dangerous=confirm_dangerous,
            model=settings.openai_model,
        )
        try:
            goal = last_user_content(messages)
            memory_context = self._memory.build_context(
                thread_id=conversation_id,
                run_id=run_id,
                messages=messages,
                goal=goal,
            )
            if resume is None:
                lc_messages = [
                    SystemMessage(content=SYSTEM_PROMPT),
                    *memory_context_messages(memory_context.as_dict()),
                    *self._to_lc_messages(messages),
                ]
                graph_input = {"messages": lc_messages}
            else:
                graph_input = Command(resume=resume)
            graph_config = {
                "recursion_limit": (MAX_ROUNDS * 2) + 4,
                "configurable": {
                    "thread_id": conversation_id,
                    "conversation_id": conversation_id,
                    "run_id": run_id,
                    "input_messages": messages,
                    "confirm_dangerous": confirm_dangerous,
                    "allow_job_post": settings.copilot_allow_job_post,
                    "trace": trace,
                },
            }
            last_output = ""
            async for domain_event in self._mapper.map(
                graph=self._graph,
                graph_input=graph_input,
                graph_config=graph_config,
                thread_id=conversation_id,
                run_id=run_id,
            ):
                if domain_event["type"] == "token":
                    last_output += str(domain_event["payload"].get("text", ""))
                yield self._emit(conversation_id, run_id, domain_event["type"], domain_event["payload"])
            end_chat_trace(trace, output_preview=last_output)
        except Exception as e:
            end_chat_trace(trace, error=str(e))
            raise
        finally:
            flush_langfuse()

    def _emit(self, thread_id: str, run_id: str | None, event_type: str, payload: dict[str, Any]) -> str:
        self._memory.append_event(thread_id, run_id, event_type, payload)
        return format_sse(event_type, payload)

    def finalize_memory(self, thread_id: str, run_id: str, *, messages: list[dict[str, Any]] | None = None) -> None:
        fallback_goal = last_user_content(messages or [])
        self._memory.summarize_run(thread_id, run_id, fallback_goal=fallback_goal)
        self._memory.update_thread_summary(thread_id, run_id)

    async def aclose(self) -> None:
        checkpointer = getattr(self._graph, "checkpointer", None)
        conn = getattr(checkpointer, "_learnagent_conn", None)
        if conn is not None and hasattr(conn, "close"):
            await conn.close()

    def _build_tool_registry(self) -> ToolRegistry:
        return ToolRegistry.from_agent_tools(
            search_docs=self._tool_handlers.search_docs,
            http_get=self._tool_handlers.http_get,
            http_post=self._tool_handlers.http_post,
            search_docs_args_schema=SearchDocsArgs,
            http_get_args_schema=HttpGetArgs,
            http_post_args_schema=HttpPostArgs,
            dangerous_post_requires_approval=_requires_dangerous_post_approval,
        )

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


def _requires_dangerous_post_approval(args: dict[str, Any]) -> bool:
    return str(args.get("path", "")).split("?", 1)[0] == DANGEROUS_JOB_PATH
