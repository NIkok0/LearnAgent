from __future__ import annotations

import logging
import time
from typing import Any, Optional

from langchain_core.runnables.config import RunnableConfig as RunnableConfigType

from copilot_agent.conversation_store import ConversationCookieStore
from copilot_agent.memory import MemoryManager
from copilot_agent.observability import (
    end_tool_span,
    sanitize_observability_payload,
    start_tool_span,
)
from copilot_agent.rag import format_chunks_for_prompt
from copilot_agent.settings import settings
from copilot_agent.tools.audit import sanitize_tool_payload
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
            hits = self._memory.search_docs(query, top_k=8)
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
            result = await self._http.http_get(path, cookie_header=cookie_header, stored_cookie=stored)
            safe = sanitize_observability_payload(result)
            end_tool_span(
                tool_span,
                result={"duration_ms": int((time.perf_counter() - t0) * 1000), **safe},
            )
            return sanitize_tool_payload(result)
        except Exception as e:
            end_tool_span(
                tool_span,
                result={"ok": False, "duration_ms": int((time.perf_counter() - t0) * 1000)},
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
            safe = sanitize_observability_payload(raw)
            end_tool_span(
                tool_span,
                result={"duration_ms": int((time.perf_counter() - t0) * 1000), **safe},
            )
            return sanitize_tool_payload(raw)
        except Exception as e:
            end_tool_span(
                tool_span,
                result={"ok": False, "duration_ms": int((time.perf_counter() - t0) * 1000)},
                error=str(e),
            )
            raise
