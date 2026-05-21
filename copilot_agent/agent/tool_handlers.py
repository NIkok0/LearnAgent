from __future__ import annotations



import logging

import time

from typing import Any, Optional



from langchain_core.runnables.config import RunnableConfig as RunnableConfigType



from copilot_agent.agent.tool_call_context import get_current_call_id

from copilot_agent.contracts.adapters.tool_http import HttpResponseAdapter

from copilot_agent.contracts.adapters.tool_rag import RagSearchAdapter
from copilot_agent.contracts.retrieval import RetrievalRequest

from copilot_agent.credentials import CredentialManager
from copilot_agent.credentials.audit import build_credential_audit_payload

from copilot_agent.memory import MemoryManager

from copilot_agent.observability import (

    end_tool_span,

    sanitize_observability_payload,

    start_tool_span,

)

from copilot_agent.rag import format_chunks_for_prompt
from copilot_agent.rag.context_guard import build_guarded_context
from copilot_agent.rag.schema import dynamic_search_top_k

from copilot_agent.context.retrieval import enrich_retrieval_payload
from copilot_agent.context.preretrieval_dedupe import apply_preretrieval_dedupe

from copilot_agent.runtime.event_schema import EVENT_CREDENTIAL_BINDING_AUDIT, EVENT_RETRIEVAL_COMPLETED

from copilot_agent.settings import settings

from copilot_agent.tools.http_tools import ScenarioHttpClient, extract_session_cookie_from_set_cookie_headers



log = logging.getLogger(__name__)


def _retrieval_request_from_context(
    *,
    query: str,
    ctx: dict[str, Any],
    user_id: str,
) -> RetrievalRequest:
    tenant_id = str(ctx.get("tenant_id") or "default")
    allowed_raw = ctx.get("allowed_scopes")
    allowed_scopes = [str(item) for item in allowed_raw] if isinstance(allowed_raw, list) else []
    if user_id:
        allowed_scopes.append(f"user:{user_id}")
    if tenant_id:
        allowed_scopes.append(f"tenant:{tenant_id}")
    return RetrievalRequest(
        tenant_id=tenant_id,
        user_id=user_id or "local_user",
        query=query,
        purpose=str(ctx.get("retrieval_purpose") or "agent_context"),
        max_classification=str(ctx.get("max_classification") or "internal"),  # type: ignore[arg-type]
        allowed_scopes=list(dict.fromkeys(allowed_scopes)),
        allow_high_pii=bool(ctx.get("allow_high_pii", False)),
    )





class ToolHandlers:

    def __init__(

        self,

        *,

        memory: MemoryManager,

        http: ScenarioHttpClient,

        cookies: CredentialManager,

    ) -> None:

        self._memory = memory

        self._http = http

        self._cookies = cookies



    def _emit_credential_audit(
        self,
        *,
        thread_id: str,
        run_id: str,
        action: str,
        required_scopes: tuple[str, ...] | list[str] = (),
        tool_name: str = "",
        reason: str = "",
        user_id: str = "",
    ) -> None:
        if not thread_id or not run_id:
            return
        payload = build_credential_audit_payload(
            action=action,  # type: ignore[arg-type]
            binding=self._cookies.binding,
            tool_name=tool_name,
            required_scopes=required_scopes,
            reason=reason,
            user_id=user_id,
        )
        self._memory.append_event(thread_id, run_id, EVENT_CREDENTIAL_BINDING_AUDIT, payload)



    async def search_docs(self, query: str, config: RunnableConfigType = None) -> dict[str, Any]:

        trace = ((config.get("configurable") or {}).get("trace")) if config else None

        tool_span = start_tool_span(trace, name="search_docs", args={"query": query})

        t0 = time.perf_counter()

        try:

            ctx = (config.get("configurable") or {}) if config else {}
            budget = settings.rag_context_budget_chars
            top_k = dynamic_search_top_k(budget_chars=budget, ceiling=8)
            thread_id = str(ctx.get("conversation_id") or ctx.get("thread_id") or "")
            run_id = str(ctx.get("run_id") or "")
            request = _retrieval_request_from_context(
                query=query,
                ctx=ctx,
                user_id=self._memory.resolve_user_id(thread_id) if thread_id else "local_user",
            )
            result, policy_result = self._memory.policy_aware_search_docs(request, top_k=top_k)

            hits = result.chunks
            cache = ctx.get("preretrieval_cache") if isinstance(ctx.get("preretrieval_cache"), dict) else None
            hits, dedupe_meta = apply_preretrieval_dedupe(query, hits, cache)

            if dedupe_meta.get("skipped_all_duplicate"):
                excerpts = (
                    "Documentation for this query was already injected as [PreRetrievedDocs] "
                    "at the start of this turn. Prefer that context; call search_docs again "
                    "only with a narrower follow-up query if something is still missing."
                )
                enrichment = enrich_retrieval_payload([], query=query)
                guarded = build_guarded_context([], max_chars=budget)
            else:
                guarded = build_guarded_context(
                    hits,
                    max_chars=budget,
                    require_citations=settings.private_rag_require_citations,
                )
                excerpts = guarded.markdown
                hits = guarded.chunks
                enrichment = enrich_retrieval_payload(hits, query=query)
            raw = {
                "excerpts_markdown": excerpts,
                "sources": list({c.source for c in hits}) or list(cache.get("sources") or []) if cache else [],
                "suggested_api_paths": enrichment["suggested_api_paths"],
                "api_field_hints": enrichment["api_field_hints"],
                "preretrieval_dedupe": dedupe_meta,
                "context_guard": guarded.audit_payload(),
            }

            duration_ms = int((time.perf_counter() - t0) * 1000)

            model = RagSearchAdapter.to_tool_result(raw, duration_ms=duration_ms)

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
                        policy_result=policy_result,
                        context_guard=guarded.audit_payload(),

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
                    "blocked_count": policy_result.blocked_count,
                    "policy_trace_id": policy_result.policy_trace_id,
                    "call_id": call_id,
                    "suggested_api_paths": len(enrichment.get("suggested_api_paths") or []),
                    "preretrieval_deduped": int(dedupe_meta.get("deduped_count") or 0),
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

            stored = self._cookies.get_cookie(conversation_id, required_scopes=("http:read",)) if conversation_id else None
            if conversation_id and not self._cookies.authorize_scopes(("http:read",)):
                self._emit_credential_audit(
                    thread_id=conversation_id,
                    run_id=str(ctx.get("run_id") or ""),
                    action="credential_read_denied",
                    tool_name="http_get",
                    required_scopes=("http:read",),
                    reason="credential_scope_denied",
                    user_id=self._memory.resolve_user_id(conversation_id),
                )

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

            stored = self._cookies.get_cookie(conversation_id, required_scopes=("http:write",)) if conversation_id else None
            if conversation_id and not self._cookies.authorize_scopes(("http:write",)):
                self._emit_credential_audit(
                    thread_id=conversation_id,
                    run_id=str(ctx.get("run_id") or ""),
                    action="credential_read_denied",
                    tool_name="http_post",
                    required_scopes=("http:write",),
                    reason="credential_scope_denied",
                    user_id=self._memory.resolve_user_id(conversation_id),
                )

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

                pair = extract_session_cookie_from_set_cookie_headers(
                    raw_list,
                    cookie_name=getattr(self._http, "_session_cookie_name", ""),
                )

                if pair:

                    self._cookies.set_cookie(
                        conversation_id,
                        user_id=self._memory.resolve_user_id(conversation_id),
                        cookie_header=pair,
                    )

                    self._emit_credential_audit(
                        thread_id=conversation_id,
                        run_id=str(ctx.get("run_id") or ""),
                        action="credential_set",
                        reason="login_set_cookie",
                        user_id=self._memory.resolve_user_id(conversation_id),
                    )

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


