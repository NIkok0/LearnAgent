from __future__ import annotations



import logging

import time

from typing import Any, Optional



from langchain_core.runnables.config import RunnableConfig as RunnableConfigType



from copilot_agent.agent.tool_call_context import get_current_call_id

from copilot_agent.contracts.adapters.tool_http import HttpResponseAdapter

from copilot_agent.contracts.adapters.tool_rag import RagSearchAdapter

from copilot_agent.conversation_store import ConversationCookieStore

from copilot_agent.memory import MemoryManager

from copilot_agent.observability import (

    end_tool_span,

    sanitize_observability_payload,

    start_tool_span,

)

from copilot_agent.rag import format_chunks_for_prompt
from copilot_agent.rag.schema import dynamic_search_top_k

from copilot_agent.rag.api_paths import extract_api_paths

from copilot_agent.runtime.event_schema import EVENT_RETRIEVAL_COMPLETED

from copilot_agent.settings import settings

from copilot_agent.tools.http_tools import WatermarkHttpTools, extract_session_cookie_from_set_cookie_headers



log = logging.getLogger(__name__)





class ToolHandlers:

    def __init__(

        self,

        *,

        memory: MemoryManager,

        http: WatermarkHttpTools,

        cookies: ConversationCookieStore,

    ) -> None:

        self._memory = memory

        self._http = http

        self._cookies = cookies



    async def search_docs(self, query: str, config: RunnableConfigType = None) -> dict[str, Any]:

        trace = ((config.get("configurable") or {}).get("trace")) if config else None

        tool_span = start_tool_span(trace, name="search_docs", args={"query": query})

        t0 = time.perf_counter()

        try:

            budget = settings.rag_context_budget_chars
            top_k = dynamic_search_top_k(budget_chars=budget, ceiling=8)
            result = self._memory.search_docs_detailed(query, top_k=top_k)

            hits = result.chunks

            excerpts = format_chunks_for_prompt(hits, max_chars=budget)

            suggested_api_paths = []
            api_field_hints: list[dict[str, object]] = []
            if settings.agent_retrieval_path_inject:
                suggested_api_paths = [hint.as_dict() for hint in extract_api_paths(hits, query=query)]
                for chunk in hits:
                    if not chunk.request_fields and chunk.api_endpoint is None and not chunk.error_codes:
                        continue
                    api_field_hints.append(
                        {
                            "source_file": chunk.source,
                            "http_method": chunk.api_endpoint.method if chunk.api_endpoint else None,
                            "http_path": chunk.api_endpoint.path if chunk.api_endpoint else None,
                            "request_fields": [field.name for field in chunk.request_fields],
                            "error_codes": [code.code for code in chunk.error_codes],
                        }
                    )
            raw = {
                "excerpts_markdown": excerpts,
                "sources": list({c.source for c in hits}),
                "suggested_api_paths": suggested_api_paths,
                "api_field_hints": api_field_hints,
            }

            duration_ms = int((time.perf_counter() - t0) * 1000)

            model = RagSearchAdapter.to_tool_result(raw, duration_ms=duration_ms)

            ctx = (config.get("configurable") or {}) if config else {}

            thread_id = str(ctx.get("conversation_id") or ctx.get("thread_id") or "")

            run_id = str(ctx.get("run_id") or "")

            call_id = get_current_call_id()

            if thread_id and run_id:

                self._memory.append_event(

                    thread_id,

                    run_id,

                    EVENT_RETRIEVAL_COMPLETED,

                    RagSearchAdapter.to_retrieval_completed_payload(

                        query,

                        hits,

                        excerpt_chars=len(excerpts),

                        call_id=call_id,

                        retrieval_mode=result.route.mode,

                        retrieval_route=result.route.as_dict(),

                    ),

                )

            end_tool_span(

                tool_span,

                result={

                    "success": model.success,

                    "sources": model.metadata.get("sources", []),

                    "excerpt_chars": model.metadata.get("excerpt_chars", 0),

                    "duration_ms": duration_ms,

                    "retrieval_mode": result.route.mode,

                    "call_id": call_id,

                    "suggested_api_paths": len(suggested_api_paths),

                },

            )

            return model.to_llm_dict()

        except Exception as e:

            duration_ms = int((time.perf_counter() - t0) * 1000)

            end_tool_span(

                tool_span,

                result={"success": False, "duration_ms": duration_ms},

                error=str(e),

            )

            raise



    async def http_get(

        self,

        path: str,

        cookie_header: Optional[str] = None,

        config: RunnableConfigType = None,

    ) -> dict[str, Any]:

        ctx = (config.get("configurable") or {}) if config else {}

        trace = ctx.get("trace")

        conversation_id = str(ctx.get("conversation_id", ""))

        tool_span = start_tool_span(trace, name="http_get", args={"path": path, "cookie_header": cookie_header})

        t0 = time.perf_counter()

        try:

            stored = self._cookies.get_cookie(conversation_id) if conversation_id else None

            raw = await self._http.http_get(path, cookie_header=cookie_header, stored_cookie=stored)

            duration_ms = int((time.perf_counter() - t0) * 1000)

            model = HttpResponseAdapter.to_tool_result(raw, duration_ms=duration_ms)

            safe = sanitize_observability_payload(model.to_llm_dict())

            end_tool_span(tool_span, result={"duration_ms": duration_ms, **safe})

            return model.to_llm_dict()

        except Exception as e:

            duration_ms = int((time.perf_counter() - t0) * 1000)

            end_tool_span(

                tool_span,

                result={"success": False, "duration_ms": duration_ms},

                error=str(e),

            )

            raise



    async def http_post(

        self,

        path: str,

        json_body: dict[str, Any],

        cookie_header: Optional[str] = None,

        idempotency_key: Optional[str] = None,

        config: RunnableConfigType = None,

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

            duration_ms = int((time.perf_counter() - t0) * 1000)

            model = HttpResponseAdapter.to_tool_result(raw, duration_ms=duration_ms)

            safe = sanitize_observability_payload(model.to_llm_dict())

            end_tool_span(tool_span, result={"duration_ms": duration_ms, **safe})

            return model.to_llm_dict()

        except Exception as e:

            duration_ms = int((time.perf_counter() - t0) * 1000)

            end_tool_span(

                tool_span,

                result={"success": False, "duration_ms": duration_ms},

                error=str(e),

            )

            raise


