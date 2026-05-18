from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from copilot_agent.agent.graph import build_agent_graph
from copilot_agent.conversation_store import ConversationCookieStore
from copilot_agent.observability import (
    end_chat_trace,
    end_tool_span,
    flush_langfuse,
    sanitize_observability_payload,
    start_chat_trace,
    start_tool_span,
)
from copilot_agent.rag import RagStore, format_chunks_for_prompt
from copilot_agent.runtime.event_store import EventStore
from copilot_agent.settings import settings
from copilot_agent.tools.http_tools import WatermarkHttpTools, extract_session_cookie_from_set_cookie_headers

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the Watermarking platform operations copilot.
Rules:
- Answer using search_docs for deploy, Redis streams, WM_JOBS_*, verify-config, queue JSON fields, and known issues. Cite doc filenames when relevant.
- For live status, use http_get against the Java API only (paths are whitelisted server-side). Never invent API paths or JSON fields.
- If something is not in the docs or API response, say the repository does not document it and manual verification is needed.
- Do not echo or ask the user to paste session cookies; login via http_post stores the session server-side for this conversation.
- POST /api/v1/jobs/watermark is gated: only when the deployment explicitly enables it and the user confirmed — otherwise explain how to check workers and Redis without enqueueing.
"""

MAX_ROUNDS = 12
DANGEROUS_JOB_PATH = "/api/v1/jobs/watermark"


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


def _sanitize_tool_result(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "_raw_set_cookie_for_store_only":
                continue
            out[k] = _sanitize_tool_result(v)
        return out
    if isinstance(obj, list):
        return [_sanitize_tool_result(x) for x in obj]
    return obj


def _extract_text_from_chunk(chunk: Any) -> str:
    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text = ""
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                text += str(p.get("text", ""))
        return text
    return ""


def _extract_blocked_message_text(event: dict[str, Any]) -> str:
    if str(event.get("event", "")) != "on_chain_end":
        return ""
    if str(event.get("name", "")) != "safety_gate":
        return ""
    output = (event.get("data") or {}).get("output", {})
    msgs = output.get("messages") if isinstance(output, dict) else None
    if not isinstance(msgs, list) or not msgs:
        return ""
    last = msgs[-1]
    if isinstance(last, AIMessage) and not last.tool_calls:
        return str(last.content or "")
    return ""


class ChatRunner:
    def __init__(
        self,
        rag_store: RagStore,
        cookie_store: ConversationCookieStore,
        event_store: EventStore | None = None,
        http: Optional[WatermarkHttpTools] = None,
    ) -> None:
        self._rag = rag_store
        self._cookies = cookie_store
        self._events = event_store
        self._http = http or WatermarkHttpTools()
        self._llm: Optional[ChatOpenAI] = None
        self._tools = self._build_tools()
        self._graph = build_agent_graph(
            self._assistant_node,
            self._safety_gate_node,
            self._tools,
            checkpoint_path=settings.agent_checkpoint_path,
        )

    async def run_stream(
        self,
        *,
        conversation_id: str,
        run_id: str | None = None,
        messages: list[dict[str, Any]],
        confirm_dangerous: bool,
    ) -> AsyncIterator[str]:
        trace = start_chat_trace(
            conversation_id=conversation_id,
            messages=messages,
            confirm_dangerous=confirm_dangerous,
            model=settings.openai_model,
        )
        try:
            lc_messages = [SystemMessage(content=SYSTEM_PROMPT), *self._to_lc_messages(messages)]
            graph_input = {"messages": lc_messages}
            graph_config = {
                "recursion_limit": (MAX_ROUNDS * 2) + 4,
                "configurable": {
                    "thread_id": conversation_id,
                    "conversation_id": conversation_id,
                    "confirm_dangerous": confirm_dangerous,
                    "allow_job_post": settings.copilot_allow_job_post,
                    "trace": trace,
                },
            }
            last_assistant_output = ""
            tool_started_at: dict[str, float] = {}
            async for event in self._graph.astream_events(graph_input, config=graph_config, version="v2"):
                kind = str(event.get("event", ""))
                if kind == "on_chat_model_stream":
                    chunk = (event.get("data") or {}).get("chunk")
                    text = _extract_text_from_chunk(chunk)
                    if text:
                        last_assistant_output += text
                        yield self._emit(conversation_id, run_id, "token", {"text": text})
                    continue
                blocked_text = _extract_blocked_message_text(event)
                if blocked_text:
                    if "gated" in blocked_text.lower() and "confirm_dangerous=true" in blocked_text:
                        yield self._emit(
                            conversation_id,
                            run_id,
                            "approval_required",
                            {"required": True, "reason": "dangerous_tool", "message": blocked_text},
                        )
                        continue
                    last_assistant_output += blocked_text
                    yield self._emit(conversation_id, run_id, "token", {"text": blocked_text})
                    continue
                if kind == "on_tool_start":
                    name = str(event.get("name", ""))
                    call_id = _extract_call_id(event)
                    if call_id:
                        tool_started_at[call_id] = time.perf_counter()
                    args = (event.get("data") or {}).get("input", {})
                    yield self._emit(
                        conversation_id,
                        run_id,
                        "tool_start",
                        {"name": name, "call_id": call_id, "arguments": _sanitize_tool_result(args)},
                    )
                    continue
                if kind == "on_tool_end":
                    name = str(event.get("name", ""))
                    call_id = _extract_call_id(event)
                    started_at = tool_started_at.pop(call_id, None) if call_id else None
                    result = (event.get("data") or {}).get("output", {})
                    duration_ms = int((time.perf_counter() - started_at) * 1000) if started_at else None
                    yield self._emit(
                        conversation_id,
                        run_id,
                        "tool_end",
                        {
                            "name": name,
                            "call_id": call_id,
                            "result": _sanitize_tool_result(result),
                            "duration_ms": duration_ms,
                            "success": True,
                        },
                    )
                    continue

            end_chat_trace(trace, output_preview=last_assistant_output)
            yield self._emit(conversation_id, run_id, "done", {})
        except Exception as e:
            end_chat_trace(trace, error=str(e))
            raise
        finally:
            flush_langfuse()

    def _emit(self, thread_id: str, run_id: str | None, event_type: str, payload: dict[str, Any]) -> str:
        if self._events is not None and run_id:
            self._events.append_event(thread_id, run_id, event_type, payload)
        return _sse(event_type, payload)

    def _build_tools(self) -> list[StructuredTool]:
        return [
            StructuredTool.from_function(
                coroutine=self._tool_search_docs,
                func=None,
                name="search_docs",
                description="Keyword search over DEPLOY-SERVER.md, REQUIREMENTS-CHECKLIST-AND-TEST-CASES.md, watermark-java-backend-tech-selection.md.",
                args_schema=SearchDocsArgs,
            ),
            StructuredTool.from_function(
                coroutine=self._tool_http_get,
                func=None,
                name="http_get",
                description="GET from the Watermark Java API (whitelist only). Optional cookie_header overrides the server-stored session for this conversation.",
                args_schema=HttpGetArgs,
            ),
            StructuredTool.from_function(
                coroutine=self._tool_http_post,
                func=None,
                name="http_post",
                description="POST login or (if enabled) enqueue watermark job. Paths strictly whitelisted.",
                args_schema=HttpPostArgs,
            ),
        ]

    async def _assistant_node(self, state) -> dict[str, list[BaseMessage]]:
        llm = self._get_llm().bind_tools(self._tools)
        ai = await llm.ainvoke(state["messages"])
        return {"messages": [ai]}

    async def _safety_gate_node(self, state, config: RunnableConfig) -> dict[str, list[BaseMessage]]:
        msgs = state.get("messages", [])
        if not msgs:
            return {}
        last = msgs[-1]
        if not isinstance(last, AIMessage) or not last.tool_calls:
            return {}
        ctx = (config.get("configurable") or {}) if config else {}
        allow_job_post = bool(ctx.get("allow_job_post", settings.copilot_allow_job_post))
        confirm_dangerous = bool(ctx.get("confirm_dangerous", False))
        for call in last.tool_calls:
            name = str(call.get("name", ""))
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            path = str(args.get("path", ""))
            if name == "http_post" and path.split("?", 1)[0] == DANGEROUS_JOB_PATH:
                if not allow_job_post:
                    return {
                        "messages": [
                            AIMessage(
                                content=(
                                    "POST /api/v1/jobs/watermark is disabled by deployment. "
                                    "Enable COPILOT_ALLOW_JOB_POST=true, then retry with explicit confirmation."
                                )
                            )
                        ]
                    }
                if not confirm_dangerous:
                    return {
                        "messages": [
                            AIMessage(
                                content=(
                                    "This action is gated. Re-send chat request with confirm_dangerous=true "
                                    "if you want to enqueue a watermark job."
                                )
                            )
                        ]
                    }
        return {}

    def _get_llm(self) -> ChatOpenAI:
        if self._llm is None:
            self._llm = ChatOpenAI(
                api_key=settings.openai_api_key or None,
                base_url=settings.openai_base_url,
                model=settings.openai_model,
                streaming=True,
            )
        return self._llm

    async def _tool_search_docs(self, query: str, config: RunnableConfig) -> dict[str, Any]:
        trace = ((config.get("configurable") or {}).get("trace")) if config else None
        tool_span = start_tool_span(trace, name="search_docs", args={"query": query})
        t0 = time.perf_counter()
        try:
            hits = self._rag.search(query, top_k=8)
            excerpts = format_chunks_for_prompt(hits, max_chars=14000)
            result = {
                "excerpts_markdown": excerpts,
                "sources": list({c.source for c in hits}),
            }
            end_tool_span(
                tool_span,
                result={
                    "sources": result["sources"],
                    "excerpt_chars": len(excerpts),
                    "duration_ms": int((time.perf_counter() - t0) * 1000),
                },
            )
            return result
        except Exception as e:
            end_tool_span(
                tool_span,
                result={"ok": False, "duration_ms": int((time.perf_counter() - t0) * 1000)},
                error=str(e),
            )
            raise

    async def _tool_http_get(self, path: str, cookie_header: Optional[str], config: RunnableConfig) -> dict[str, Any]:
        ctx = (config.get("configurable") or {}) if config else {}
        trace = ctx.get("trace")
        conversation_id = str(ctx.get("conversation_id", ""))
        tool_span = start_tool_span(trace, name="http_get", args={"path": path, "cookie_header": cookie_header})
        t0 = time.perf_counter()
        try:
            stored = self._cookies.get_cookie(conversation_id) if conversation_id else None
            result = await self._http.http_get(path, cookie_header=cookie_header, stored_cookie=stored)
            safe = sanitize_observability_payload(result)
            end_tool_span(
                tool_span,
                result={"duration_ms": int((time.perf_counter() - t0) * 1000), **safe},
            )
            return _sanitize_tool_result(result)
        except Exception as e:
            end_tool_span(
                tool_span,
                result={"ok": False, "duration_ms": int((time.perf_counter() - t0) * 1000)},
                error=str(e),
            )
            raise

    async def _tool_http_post(
        self,
        path: str,
        json_body: dict[str, Any],
        cookie_header: Optional[str],
        idempotency_key: Optional[str],
        config: RunnableConfig,
    ) -> dict[str, Any]:
        ctx = (config.get("configurable") or {}) if config else {}
        trace = ctx.get("trace")
        conversation_id = str(ctx.get("conversation_id", ""))
        allow_job_post = bool(ctx.get("allow_job_post", settings.copilot_allow_job_post))
        confirm_dangerous = bool(ctx.get("confirm_dangerous", False))
        tool_span = start_tool_span(
            trace,
            name="http_post",
            args={
                "path": path,
                "json_body": json_body,
                "cookie_header": cookie_header,
                "idempotency_key": idempotency_key,
            },
        )
        t0 = time.perf_counter()
        try:
            stored = self._cookies.get_cookie(conversation_id) if conversation_id else None
            raw = await self._http.http_post(
                path,
                json_body if isinstance(json_body, dict) else {},
                cookie_header=cookie_header,
                stored_cookie=stored,
                idempotency_key=idempotency_key,
                allow_job_post=allow_job_post,
                user_confirmed_dangerous=confirm_dangerous,
            )
            raw_list = raw.pop("_raw_set_cookie_for_store_only", None)
            if raw_list and isinstance(raw_list, list) and conversation_id:
                pair = extract_session_cookie_from_set_cookie_headers(raw_list)
                if pair:
                    self._cookies.set_cookie(conversation_id, pair)
                    log.info("Stored session cookie for conversation (redacted): %s", conversation_id[:8] + "...")
            safe = sanitize_observability_payload(raw)
            end_tool_span(
                tool_span,
                result={"duration_ms": int((time.perf_counter() - t0) * 1000), **safe},
            )
            return _sanitize_tool_result(raw)
        except Exception as e:
            end_tool_span(
                tool_span,
                result={"ok": False, "duration_ms": int((time.perf_counter() - t0) * 1000)},
                error=str(e),
            )
            raise

    def _to_lc_messages(self, messages: list[dict[str, Any]]) -> list[BaseMessage]:
        out: list[BaseMessage] = []
        for m in messages:
            role = str(m.get("role", "")).lower()
            content = str(m.get("content", ""))
            if role == "user":
                out.append(HumanMessage(content=content))
            elif role == "assistant":
                out.append(AIMessage(content=content))
            elif role == "system":
                out.append(SystemMessage(content=content))
            else:
                out.append(HumanMessage(content=content))
        return out


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _extract_call_id(event: dict[str, Any]) -> str:
    run_id = event.get("run_id")
    if run_id:
        return str(run_id)
    data = event.get("data") or {}
    if isinstance(data, dict):
        for key in ("id", "tool_call_id", "run_id"):
            value = data.get(key)
            if value:
                return str(value)
    return ""
