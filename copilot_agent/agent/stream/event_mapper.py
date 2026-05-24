from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from copilot_agent.agent.final_answer import build_final_answer
from copilot_agent.agent.tool_call_context import clear_tool_call_context, set_tool_call_context
from copilot_agent.agent.message_utils import (
    approval_tool_call_ids,
    extract_blocked_message_text,
    extract_call_id,
    extract_interrupt_payload,
    extract_reasoning_content,
    extract_reasoning_content_from_chat_output,
    extract_text_from_chat_output,
    extract_text_from_chunk,
)
from copilot_agent.contracts.base import RuntimeEvent
from copilot_agent.memory import MemoryManager
from copilot_agent.observability import (
    end_generation_span,
    resolve_observability_trace_id,
    start_generation_span,
)
from copilot_agent.rag.context_guard import detect_sensitive_output
from copilot_agent.runtime.checkpoint_reader import CheckpointReader
from copilot_agent.runtime.event_schema import (
    EVENT_CHECKPOINT_SYNC_FAILED,
    EVENT_LLM_GENERATION,
    EVENT_OUTPUT_GUARD_CHECKED,
    EVENT_RUN_COMPLETED_META,
    EVENT_TOOL_SIDE_EFFECT_RECORDED,
)
from copilot_agent.runtime.execution_engine import CheckpointSyncFailed, GraphInterrupted
from copilot_agent.settings import settings
from copilot_agent.tools.audit import (
    build_tool_end_payload,
    build_tool_side_effect_payload,
    build_tool_start_payload,
)
from copilot_agent.tools.registry import ToolRegistry


@dataclass
class _ToolTracker:
    started_at: dict[str, float] = field(default_factory=dict)
    start_names: dict[str, str] = field(default_factory=dict)
    start_meta: dict[str, dict[str, Any]] = field(default_factory=dict)
    start_payloads: dict[str, dict[str, Any]] = field(default_factory=dict)
    end_emitted: set[str] = field(default_factory=set)


@dataclass
class _LlmUsageTracker:
    llm_rounds: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost: float | None = None

    def add(self, usage: dict[str, int], *, estimated_cost: float | None = None) -> None:
        self.llm_rounds += 1
        self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(usage.get("completion_tokens") or 0)
        self.total_tokens += int(usage.get("total_tokens") or 0)
        if estimated_cost is not None:
            self.estimated_cost = (self.estimated_cost or 0.0) + float(estimated_cost)

    def as_dict(self) -> dict[str, int | float | None]:
        out: dict[str, int | float | None] = {
            "llm_rounds": self.llm_rounds,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }
        out["estimated_cost"] = round(self.estimated_cost, 8) if self.estimated_cost is not None else None
        return out


class GraphEventMapper:
    def __init__(
        self,
        *,
        memory: MemoryManager,
        tool_registry: ToolRegistry,
        checkpoint_reader: CheckpointReader | None = None,
    ) -> None:
        self._memory = memory
        self._tool_registry = tool_registry
        self._checkpoint_reader = checkpoint_reader

    async def map(
        self,
        *,
        graph: Any,
        graph_input: Any,
        graph_config: dict[str, Any],
        thread_id: str,
        run_id: str | None,
    ) -> AsyncIterator[RuntimeEvent]:
        tracker = _ToolTracker()
        usage = _LlmUsageTracker()
        configurable = graph_config.get("configurable") if isinstance(graph_config.get("configurable"), dict) else {}
        trace = configurable.get("trace")
        trace_id = resolve_observability_trace_id(
            configurable.get("trace"),
            thread_id=thread_id,
            run_id=run_id,
        )
        provider_name = str(configurable.get("observability_provider") or "none")
        external_trace_url = configurable.get("external_trace_url")
        pending_tool_call_ids = approval_tool_call_ids(
            self._memory.get_thread_events(thread_id, run_id=run_id)
        )
        last_assistant_output = ""
        last_reasoning_content = ""
        generation_started_at: dict[str, float] = {}
        generation_spans: dict[str, Any] = {}
        generation_outputs: dict[str, str] = {}
        generation_tool_names: dict[str, set[str]] = {}

        async for event in graph.astream_events(graph_input, config=graph_config, version="v2"):
            kind = str(event.get("event", ""))
            interrupt_payload = extract_interrupt_payload(event)
            if interrupt_payload is not None:
                for call in interrupt_payload.get("tool_calls") or []:
                    if isinstance(call, dict) and call.get("name") and call.get("id"):
                        pending_tool_call_ids[str(call["name"])] = str(call["id"])
                yield _runtime_event(
                    "approval_required",
                    interrupt_payload,
                    thread_id=thread_id,
                    run_id=run_id,
                    trace_id=trace_id,
                )
                raise GraphInterrupted(interrupt_payload)

            if kind == "on_chat_model_start":
                run_key = _event_run_key(event)
                if run_key:
                    generation_started_at[run_key] = time.perf_counter()
                    if run_key not in generation_spans:
                        generation_spans[run_key] = start_generation_span(
                            trace,
                            model=settings.openai_model,
                            round_index=usage.llm_rounds + 1,
                            messages_count=_messages_count(event),
                        )
                continue

            if kind == "on_chat_model_stream":
                run_key = _event_run_key(event)
                chunk = (event.get("data") or {}).get("chunk")
                reasoning_delta = extract_reasoning_content(chunk)
                if reasoning_delta:
                    last_reasoning_content += reasoning_delta
                    yield _runtime_event(
                        "assistant_state",
                        {"reasoning_content_delta": reasoning_delta},
                        thread_id=thread_id,
                        run_id=run_id,
                        trace_id=trace_id,
                    )
                text = extract_text_from_chunk(chunk)
                if text:
                    if run_key:
                        generation_outputs[run_key] = generation_outputs.get(run_key, "") + text
                    last_assistant_output += text
                    if settings.private_rag_output_guard_enabled:
                        pass
                    else:
                        yield _runtime_event(
                            "token",
                            {"text": text},
                            thread_id=thread_id,
                            run_id=run_id,
                            trace_id=trace_id,
                        )
                continue

            if kind == "on_chat_model_end":
                run_key = _event_run_key(event)
                output = (event.get("data") or {}).get("output")
                token_usage = _extract_token_usage(output)
                round_index = usage.llm_rounds + 1
                estimated_cost = estimate_llm_cost(
                    provider=settings.openai_provider,
                    model=settings.openai_model,
                    prompt_tokens=int(token_usage.get("prompt_tokens") or 0),
                    completion_tokens=int(token_usage.get("completion_tokens") or 0),
                )
                usage.add(token_usage, estimated_cost=estimated_cost)
                if token_usage or run_key:
                    latency_ms = _latency_ms(generation_started_at.pop(run_key, None)) if run_key else None
                    finish_reason = _extract_finish_reason(output)
                    output_preview = generation_outputs.pop(run_key, "") if run_key else ""
                    tool_names = sorted(generation_tool_names.pop(run_key, set())) if run_key else []
                    span = generation_spans.pop(run_key, None) if run_key else None
                    end_generation_span(
                        span,
                        output_preview=output_preview or extract_text_from_chat_output(output),
                        finish_reason=finish_reason,
                        tool_names=tool_names,
                    )
                    yield _runtime_event(
                        EVENT_LLM_GENERATION,
                        {
                            "trace_id": trace_id,
                            "provider": settings.openai_provider,
                            "model": settings.openai_model,
                            "round_index": round_index,
                            "latency_ms": latency_ms,
                            "prompt_tokens": token_usage.get("prompt_tokens", 0),
                            "completion_tokens": token_usage.get("completion_tokens", 0),
                            "total_tokens": token_usage.get("total_tokens", 0),
                            "estimated_cost": estimated_cost,
                            "finish_reason": finish_reason,
                            "tool_call_count": len(tool_names),
                            "tool_names": tool_names,
                            "observability_provider": provider_name,
                            "external_trace_url": external_trace_url,
                        },
                        thread_id=thread_id,
                        run_id=run_id,
                        trace_id=trace_id,
                    )
                reasoning = extract_reasoning_content_from_chat_output(output)
                if reasoning and reasoning != last_reasoning_content:
                    last_reasoning_content = reasoning
                    yield _runtime_event(
                        "assistant_state",
                        {"reasoning_content": reasoning},
                        thread_id=thread_id,
                        run_id=run_id,
                        trace_id=trace_id,
                    )
                text = extract_text_from_chat_output(output)
                if text and text not in last_assistant_output:
                    last_assistant_output += text
                    if settings.private_rag_output_guard_enabled:
                        pass
                    else:
                        yield _runtime_event(
                            "token",
                            {"text": text},
                            thread_id=thread_id,
                            run_id=run_id,
                            trace_id=trace_id,
                        )
                continue

            blocked_text = extract_blocked_message_text(event)
            if blocked_text:
                if "gated" in blocked_text.lower() and "confirm_dangerous=true" in blocked_text:
                    payload = {"required": True, "reason": "dangerous_tool", "message": blocked_text}
                    yield _runtime_event(
                        "approval_required",
                        payload,
                        thread_id=thread_id,
                        run_id=run_id,
                        trace_id=trace_id,
                    )
                    raise GraphInterrupted(payload)
                last_assistant_output += blocked_text
                if settings.private_rag_output_guard_enabled:
                    pass
                else:
                    yield _runtime_event(
                        "token",
                        {"text": blocked_text},
                        thread_id=thread_id,
                        run_id=run_id,
                        trace_id=trace_id,
                    )
                continue

            if kind == "on_tool_start":
                name = str(event.get("name", ""))
                parent_key = _event_parent_run_key(event)
                if parent_key:
                    generation_tool_names.setdefault(parent_key, set()).add(name)
                call_id = pending_tool_call_ids.get(name) or extract_call_id(event)
                if call_id:
                    tracker.started_at[call_id] = time.perf_counter()
                    tracker.start_names[call_id] = name
                    set_tool_call_context(
                        call_id=str(call_id),
                        tool_name=name,
                        thread_id=thread_id,
                        run_id=str(run_id or ""),
                    )
                args = (event.get("data") or {}).get("input", {})
                spec = self._tool_registry.get_spec(name)
                metadata = _tool_audit_metadata(spec, args if isinstance(args, dict) else {})
                if call_id:
                    tracker.start_meta[call_id] = metadata
                start_payload = build_tool_start_payload(
                    name=name,
                    call_id=call_id,
                    category=str(metadata.get("category", "")),
                    risk_level=str(metadata.get("risk_level", "")),
                    requires_approval=bool(metadata.get("requires_approval", False)),
                    arguments=args,
                    timeout_seconds=metadata.get("timeout_seconds"),
                    max_retries=metadata.get("max_retries"),
                    idempotency_key=metadata.get("idempotency_key"),
                )
                if call_id:
                    tracker.start_payloads[call_id] = start_payload
                yield _runtime_event(
                    "tool_start",
                    start_payload,
                    thread_id=thread_id,
                    run_id=run_id,
                    trace_id=trace_id,
                )
                continue

            if kind == "on_tool_end":
                name = str(event.get("name", ""))
                call_id = self._resolve_tool_call_id(event, name, tracker, pending_tool_call_ids)
                if call_id and call_id in tracker.end_emitted:
                    clear_tool_call_context()
                    continue
                started_at = tracker.started_at.pop(call_id, None) if call_id else None
                if call_id:
                    tracker.start_names.pop(call_id, None)
                    tracker.end_emitted.add(call_id)
                result = (event.get("data") or {}).get("output", {})
                duration_ms = int((time.perf_counter() - started_at) * 1000) if started_at else None
                metadata = tracker.start_meta.pop(call_id, {}) if call_id else {}
                start_payload = tracker.start_payloads.pop(call_id, {}) if call_id else {}
                end_payload = build_tool_end_payload(
                    name=name,
                    call_id=call_id,
                    result=result,
                    duration_ms=duration_ms,
                    retry_count=metadata.get("retry_count"),
                    attempt=metadata.get("attempt"),
                    max_attempts=metadata.get("max_attempts"),
                    timeout_seconds=metadata.get("timeout_seconds"),
                    idempotency_key=metadata.get("idempotency_key"),
                )
                yield _runtime_event(
                    "tool_end",
                    end_payload,
                    thread_id=thread_id,
                    run_id=run_id,
                    trace_id=trace_id,
                    tool_call_id=call_id,
                )
                side_effect_payload = build_tool_side_effect_payload(
                    tool_start_payload=start_payload,
                    tool_end_payload=end_payload,
                )
                if side_effect_payload is not None:
                    yield _runtime_event(
                        EVENT_TOOL_SIDE_EFFECT_RECORDED,
                        side_effect_payload,
                        thread_id=thread_id,
                        run_id=run_id,
                        trace_id=trace_id,
                        tool_call_id=call_id,
                    )
                clear_tool_call_context()
                continue

            if kind == "on_tool_error":
                name = str(event.get("name", ""))
                call_id = self._resolve_tool_call_id(event, name, tracker, pending_tool_call_ids)
                if call_id and call_id in tracker.end_emitted:
                    clear_tool_call_context()
                    continue
                if call_id and not name:
                    name = tracker.start_names.pop(call_id, "")
                started_at = tracker.started_at.pop(call_id, None) if call_id else None
                metadata = tracker.start_meta.pop(call_id, {}) if call_id else {}
                start_payload = tracker.start_payloads.pop(call_id, {}) if call_id else {}
                error_value = (event.get("data") or {}).get("error")
                duration_ms = int((time.perf_counter() - started_at) * 1000) if started_at else None
                if call_id:
                    tracker.end_emitted.add(call_id)
                retry_count = _retry_count_from_error(error_value, metadata)
                result = _tool_error_result(error_value)
                end_payload = build_tool_end_payload(
                    name=name,
                    call_id=call_id,
                    result=result,
                    duration_ms=duration_ms,
                    success=False,
                    error=str(error_value or "tool execution failed"),
                    error_type=type(error_value).__name__ if error_value is not None else "ToolExecutionError",
                    retry_count=retry_count,
                    attempt=getattr(error_value, "attempt", None) or metadata.get("attempt"),
                    max_attempts=getattr(error_value, "max_attempts", None) or metadata.get("max_attempts"),
                    timeout_seconds=metadata.get("timeout_seconds"),
                    idempotency_key=metadata.get("idempotency_key"),
                )
                yield _runtime_event(
                    "tool_end",
                    end_payload,
                    thread_id=thread_id,
                    run_id=run_id,
                    trace_id=trace_id,
                    tool_call_id=call_id,
                )
                side_effect_payload = build_tool_side_effect_payload(
                    tool_start_payload=start_payload,
                    tool_end_payload=end_payload,
                )
                if side_effect_payload is not None:
                    yield _runtime_event(
                        EVENT_TOOL_SIDE_EFFECT_RECORDED,
                        side_effect_payload,
                        thread_id=thread_id,
                        run_id=run_id,
                        trace_id=trace_id,
                        tool_call_id=call_id,
                    )
                clear_tool_call_context()
                continue

        for missing in self._missing_tool_end_events(thread_id, run_id, tracker, trace_id=trace_id):
            yield missing

        checkpoint_payload: dict[str, Any] | None = None
        if self._checkpoint_reader is not None and run_id:
            try:
                snapshot = await self._checkpoint_reader.snapshot(thread_id)
                checkpoint_payload = {
                    "checkpoint_thread_id": snapshot["checkpoint_thread_id"],
                    "message_count": snapshot["message_count"],
                    "has_interrupt": snapshot["has_interrupt"],
                    **usage.as_dict(),
                    "trace_id": trace_id,
                    "observability_provider": provider_name,
                    "external_trace_url": external_trace_url,
                    "tool_count": _count_events(thread_id, run_id, "tool_start", self._memory),
                    "failed_tool_count": _failed_tool_count(thread_id, run_id, self._memory),
                    "retrieval_count": _count_events(thread_id, run_id, "retrieval_completed", self._memory),
                }
            except Exception as exc:
                yield _runtime_event(
                    EVENT_CHECKPOINT_SYNC_FAILED,
                    {"error": str(exc), "phase": "run_completed_meta"},
                    thread_id=thread_id,
                    run_id=run_id,
                    trace_id=trace_id,
                )
                raise CheckpointSyncFailed(str(exc)) from exc
            yield _runtime_event(
                EVENT_RUN_COMPLETED_META,
                checkpoint_payload,
                thread_id=thread_id,
                run_id=run_id,
                trace_id=trace_id,
            )

        final_output, guard_payload = _apply_output_guard(last_assistant_output)
        if settings.private_rag_output_guard_enabled:
            yield _runtime_event(
                EVENT_OUTPUT_GUARD_CHECKED,
                guard_payload,
                thread_id=thread_id,
                run_id=run_id,
                trace_id=trace_id,
            )
            if final_output:
                yield _runtime_event(
                    "token",
                    {"text": final_output},
                    thread_id=thread_id,
                    run_id=run_id,
                    trace_id=trace_id,
                )
        assistant_message = {"content": final_output}
        if last_reasoning_content:
            assistant_message["reasoning_content"] = last_reasoning_content
        done_payload: dict[str, Any] = {"assistant_message": assistant_message}
        if self._checkpoint_reader is not None:
            try:
                values = await self._checkpoint_reader.state_values(thread_id)
            except Exception as exc:
                yield _runtime_event(
                    EVENT_CHECKPOINT_SYNC_FAILED,
                    {"error": str(exc), "phase": "final_answer"},
                    thread_id=thread_id,
                    run_id=run_id,
                    trace_id=trace_id,
                )
                raise CheckpointSyncFailed(str(exc)) from exc
            messages = values.get("messages") if isinstance(values.get("messages"), list) else []
            tool_route = values.get("tool_route") if isinstance(values.get("tool_route"), dict) else {}
            route_kind = str(tool_route.get("kind") or "") or None
            final_answer = build_final_answer(
                answer=final_output,
                messages=messages,
                route_kind=route_kind,
                metadata={
                    "safety_status": "safe" if guard_payload.get("safe") else "blocked",
                    "output_guard_action": guard_payload.get("action"),
                    "output_guard": guard_payload,
                    "citation_required": settings.private_rag_require_citations,
                    "trace_id": trace_id,
                    "run_id": run_id,
                },
            )
            done_payload["final_answer"] = final_answer.model_dump()
        yield _runtime_event(
            "done",
            done_payload,
            thread_id=thread_id,
            run_id=run_id,
            trace_id=trace_id,
        )

    def _resolve_tool_call_id(
        self,
        event: dict[str, Any],
        name: str,
        tracker: _ToolTracker,
        pending_tool_call_ids: dict[str, str],
    ) -> str:
        call_id = extract_call_id(event)
        if call_id and call_id in tracker.started_at:
            return call_id
        if name in pending_tool_call_ids and pending_tool_call_ids[name] in tracker.started_at:
            return pending_tool_call_ids[name]
        for cid, tool_name in tracker.start_names.items():
            if tool_name == name and cid not in tracker.end_emitted:
                return cid
        if len(tracker.started_at) == 1:
            return next(iter(tracker.started_at))
        return call_id

    def _missing_tool_end_events(
        self,
        thread_id: str,
        run_id: str | None,
        tracker: _ToolTracker,
        *,
        trace_id: str | None = None,
    ) -> list[RuntimeEvent]:
        if not run_id:
            return []
        events = self._memory.get_thread_events(thread_id, run_id=run_id)
        tool_starts = [event for event in events if event.get("type") == "tool_start"]
        tool_ends = [event for event in events if event.get("type") == "tool_end"]
        if len(tool_ends) >= len(tool_starts):
            return []
        existing_end_ids = {
            str((event.get("payload") or {}).get("call_id") or "")
            for event in tool_ends
        }
        out: list[RuntimeEvent] = []
        for event in tool_starts:
            payload = event.get("payload") or {}
            call_id = str(payload.get("call_id") or "")
            if not call_id or call_id in existing_end_ids or call_id in tracker.end_emitted:
                continue
            name = str(payload.get("name") or tracker.start_names.get(call_id, ""))
            started_at = tracker.started_at.pop(call_id, None)
            duration_ms = int((time.perf_counter() - started_at) * 1000) if started_at else None
            metadata = tracker.start_meta.pop(call_id, {})
            start_payload = tracker.start_payloads.pop(call_id, {})
            end_payload = build_tool_end_payload(
                name=name,
                call_id=call_id,
                result={},
                duration_ms=duration_ms,
                success=False,
                error="tool execution did not produce a result event before graph completed",
                retry_count=metadata.get("retry_count"),
                attempt=metadata.get("attempt"),
                max_attempts=metadata.get("max_attempts"),
                timeout_seconds=metadata.get("timeout_seconds"),
                idempotency_key=metadata.get("idempotency_key"),
            )
            tracker.start_names.pop(call_id, None)
            tracker.end_emitted.add(call_id)
            out.append(
                _runtime_event(
                    "tool_end",
                    end_payload,
                    thread_id=thread_id,
                    run_id=run_id,
                    trace_id=trace_id,
                    tool_call_id=call_id,
                )
            )
            side_effect_payload = build_tool_side_effect_payload(
                tool_start_payload=start_payload,
                tool_end_payload=end_payload,
            )
            if side_effect_payload is not None:
                out.append(
                    _runtime_event(
                        EVENT_TOOL_SIDE_EFFECT_RECORDED,
                        side_effect_payload,
                        thread_id=thread_id,
                        run_id=run_id,
                        trace_id=trace_id,
                        tool_call_id=call_id,
                    )
                )
        return out


def _runtime_event(
    kind: str,
    payload: dict[str, Any],
    *,
    thread_id: str,
    run_id: str | None,
    trace_id: str | None = None,
    tool_call_id: str | None = None,
) -> RuntimeEvent:
    return RuntimeEvent.from_payload(
        kind,
        payload,
        thread_id=thread_id,
        run_id=run_id,
        trace_id=trace_id,
        tool_call_id=tool_call_id,
    )


def _extract_token_usage(output: Any) -> dict[str, int]:
    if output is None:
        return {}
    meta = getattr(output, "response_metadata", None)
    if not isinstance(meta, dict):
        meta = {}
    usage = meta.get("token_usage") or meta.get("usage") or getattr(output, "usage_metadata", None) or {}
    if not isinstance(usage, dict):
        return {}
    prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    total = int(usage.get("total_tokens") or prompt + completion)
    if not any((prompt, completion, total)):
        return {}
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }


def _extract_finish_reason(output: Any) -> str | None:
    if output is None:
        return None
    meta = getattr(output, "response_metadata", None)
    if isinstance(meta, dict):
        value = meta.get("finish_reason") or meta.get("stop_reason")
        if value:
            return str(value)
    generation_info = getattr(output, "generation_info", None)
    if isinstance(generation_info, dict):
        value = generation_info.get("finish_reason")
        if value:
            return str(value)
    return None


def estimate_llm_cost(*, provider: str, model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    key = _price_key(provider=provider, model=model)
    price = _PRICE_PER_MILLION_TOKENS.get(key)
    if price is None:
        return None
    prompt_price, completion_price = price
    cost = (prompt_tokens / 1_000_000) * prompt_price + (completion_tokens / 1_000_000) * completion_price
    return round(cost, 8)


def _price_key(*, provider: str, model: str) -> str:
    provider_text = (provider or "").strip().lower()
    model_text = (model or "").strip().lower()
    if "deepseek" in provider_text or model_text.startswith("deepseek"):
        if "chat" in model_text:
            return "deepseek-chat"
        if "reasoner" in model_text:
            return "deepseek-reasoner"
        return model_text
    return model_text


_PRICE_PER_MILLION_TOKENS: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
}


def _event_run_key(event: dict[str, Any]) -> str:
    raw = event.get("run_id") or event.get("run_name") or ""
    return str(raw)


def _event_parent_run_key(event: dict[str, Any]) -> str:
    parents = event.get("parent_ids")
    if isinstance(parents, list) and parents:
        return str(parents[-1])
    return ""


def _messages_count(event: dict[str, Any]) -> int:
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    input_value = data.get("input") if isinstance(data, dict) else None
    if isinstance(input_value, dict):
        messages = input_value.get("messages")
        if isinstance(messages, list):
            return len(messages)
    if isinstance(input_value, list):
        return len(input_value)
    return 0


def _latency_ms(started_at: float | None) -> int | None:
    if started_at is None:
        return None
    return int((time.perf_counter() - started_at) * 1000)


def _count_events(thread_id: str, run_id: str | None, event_type: str, memory: MemoryManager) -> int:
    if not run_id:
        return 0
    return sum(1 for event in memory.get_thread_events(thread_id, run_id=run_id) if event.get("type") == event_type)


def _failed_tool_count(thread_id: str, run_id: str | None, memory: MemoryManager) -> int:
    if not run_id:
        return 0
    count = 0
    for event in memory.get_thread_events(thread_id, run_id=run_id):
        if event.get("type") != "tool_end":
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if payload.get("success") is False:
            count += 1
    return count


def _apply_output_guard(text: str) -> tuple[str, dict[str, Any]]:
    if not settings.private_rag_output_guard_enabled:
        return text, {
            "guard": "private_rag_output_v1",
            "safe": True,
            "action": "disabled",
            "finding_count": 0,
            "findings": [],
            "original_chars": len(text),
            "emitted_chars": len(text),
        }
    verdict = detect_sensitive_output(text)
    safe = bool(verdict.get("safe"))
    findings = [str(item) for item in verdict.get("findings") or []]
    if safe or not settings.private_rag_output_guard_block:
        action = "allow" if safe else "audit_only"
        output = text
    else:
        action = "degrade"
        output = (
            "I cannot return that answer because it appears to contain sensitive information. "
            "Please narrow the request or use an authorized retrieval path."
        )
    payload = {
        "guard": "private_rag_output_v1",
        "safe": safe,
        "action": action,
        "finding_count": int(verdict.get("finding_count") or len(findings)),
        "findings": findings,
        "original_chars": len(text),
        "emitted_chars": len(output),
    }
    return output, payload


def _tool_audit_metadata(spec, args: dict[str, Any]) -> dict[str, Any]:
    if spec is None:
        return {
            "category": "",
            "risk_level": "",
            "requires_approval": False,
            "timeout_seconds": None,
            "max_retries": None,
            "retry_count": None,
            "attempt": None,
            "max_attempts": None,
            "idempotency_key": None,
        }
    return {
        "category": spec.category,
        "risk_level": spec.risk_level,
        "requires_approval": spec.requires_approval_for(args),
        "timeout_seconds": spec.timeout_seconds,
        "max_retries": spec.max_retries,
        "retry_count": 0,
        "attempt": 1,
        "max_attempts": max(1, int(spec.max_retries) + 1),
        "idempotency_key": _idempotency_key_for(spec, args),
    }


def _idempotency_key_for(spec, args: dict[str, Any]) -> str | None:
    field = getattr(spec, "idempotency_key_field", None)
    if not field:
        return None
    value = args.get(str(field))
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _retry_count_from_error(error: Any, metadata: dict[str, Any]) -> int | None:
    attempt = getattr(error, "attempt", None)
    if attempt is not None:
        try:
            return max(0, int(attempt) - 1)
        except (TypeError, ValueError):
            return metadata.get("retry_count")
    return metadata.get("retry_count")


def _tool_error_result(error: Any) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for key in ("tool_name", "attempt", "max_attempts", "timeout_seconds"):
        value = getattr(error, key, None)
        if value is not None:
            metadata[key] = value
    if metadata:
        return {"success": False, "error": str(error or "tool execution failed"), "metadata": metadata}
    return {}
