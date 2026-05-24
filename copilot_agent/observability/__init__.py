from typing import Any

from copilot_agent.observability.langfuse_tracer import sanitize_observability_payload
from copilot_agent.observability.provider import (
    SpanHandle,
    TraceHandle,
    end_generation,
    end_run_trace,
    end_tool,
    flush_observability,
    get_observability_provider,
    observability_trace_metadata,
    provider_configured,
    reset_observability_provider,
    resolve_observability_trace_id,
    start_generation,
    start_run_trace,
    start_tool,
)


def start_chat_trace(
    *,
    conversation_id: str,
    messages: list[dict[str, Any]],
    confirm_dangerous: bool,
    model: str,
    run_id: str | None = None,
) -> TraceHandle:
    return start_run_trace(
        conversation_id=conversation_id,
        run_id=run_id,
        messages=messages,
        confirm_dangerous=confirm_dangerous,
        model=model,
    )


def end_chat_trace(trace: TraceHandle | None, *, output_preview: str = "", error: str = "") -> None:
    end_run_trace(trace, output_preview=output_preview, error=error)


def start_generation_span(
    trace: TraceHandle | None,
    *,
    model: str,
    round_index: int,
    messages_count: int,
) -> SpanHandle | None:
    return start_generation(
        trace,
        model=model,
        round_index=round_index,
        messages_count=messages_count,
    )


def end_generation_span(
    generation: SpanHandle | None,
    *,
    output_preview: str,
    finish_reason: str | None,
    tool_names: list[str],
) -> None:
    end_generation(
        generation,
        output_preview=output_preview,
        finish_reason=finish_reason,
        tool_names=tool_names,
    )


def start_tool_span(trace: TraceHandle | None, *, name: str, args: dict[str, Any]) -> SpanHandle | None:
    return start_tool(trace, name=name, args=args)


def end_tool_span(span: SpanHandle | None, *, result: Any, error: str = "") -> None:
    end_tool(span, result=result, error=error)


def flush_langfuse() -> None:
    flush_observability()

__all__ = [
    "SpanHandle",
    "TraceHandle",
    "end_chat_trace",
    "end_generation_span",
    "end_tool_span",
    "flush_observability",
    "flush_langfuse",
    "get_observability_provider",
    "observability_trace_metadata",
    "provider_configured",
    "reset_observability_provider",
    "resolve_observability_trace_id",
    "sanitize_observability_payload",
    "start_chat_trace",
    "start_generation_span",
    "start_tool_span",
]
