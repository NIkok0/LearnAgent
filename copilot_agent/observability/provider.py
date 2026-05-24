from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from copilot_agent.observability.langfuse_tracer import (
    end_chat_trace,
    end_generation_span,
    end_tool_span,
    flush_langfuse,
    resolve_trace_id,
    sanitize_observability_payload,
    start_chat_trace,
    start_generation_span,
    start_tool_span,
)
from copilot_agent.settings import settings

log = logging.getLogger(__name__)


@dataclass
class TraceHandle:
    provider: str
    raw: Any | None = None
    trace_id: str | None = None
    external_trace_url: str | None = None


@dataclass
class SpanHandle:
    provider: str
    raw: Any | None = None


class ObservabilityProvider(Protocol):
    name: str

    def start_run_trace(
        self,
        *,
        conversation_id: str,
        run_id: str | None,
        messages: list[dict[str, Any]],
        confirm_dangerous: bool,
        model: str,
    ) -> TraceHandle:
        ...

    def end_run_trace(self, trace: TraceHandle | None, *, output_preview: str = "", error: str = "") -> None:
        ...

    def start_generation(
        self,
        trace: TraceHandle | None,
        *,
        model: str,
        round_index: int,
        messages_count: int,
    ) -> SpanHandle | None:
        ...

    def end_generation(
        self,
        span: SpanHandle | None,
        *,
        output_preview: str,
        finish_reason: str | None,
        tool_names: list[str],
    ) -> None:
        ...

    def start_tool(self, trace: TraceHandle | None, *, name: str, args: dict[str, Any]) -> SpanHandle | None:
        ...

    def end_tool(self, span: SpanHandle | None, *, result: Any, error: str = "") -> None:
        ...

    def flush(self) -> None:
        ...


class NoopProvider:
    name = "none"

    def start_run_trace(
        self,
        *,
        conversation_id: str,
        run_id: str | None,
        messages: list[dict[str, Any]],
        confirm_dangerous: bool,
        model: str,
    ) -> TraceHandle:
        return TraceHandle(
            provider=self.name,
            trace_id=resolve_trace_id(None, thread_id=conversation_id, run_id=run_id),
        )

    def end_run_trace(self, trace: TraceHandle | None, *, output_preview: str = "", error: str = "") -> None:
        return None

    def start_generation(
        self,
        trace: TraceHandle | None,
        *,
        model: str,
        round_index: int,
        messages_count: int,
    ) -> SpanHandle | None:
        return None

    def end_generation(
        self,
        span: SpanHandle | None,
        *,
        output_preview: str,
        finish_reason: str | None,
        tool_names: list[str],
    ) -> None:
        return None

    def start_tool(self, trace: TraceHandle | None, *, name: str, args: dict[str, Any]) -> SpanHandle | None:
        return None

    def end_tool(self, span: SpanHandle | None, *, result: Any, error: str = "") -> None:
        return None

    def flush(self) -> None:
        return None


class LangfuseProvider(NoopProvider):
    name = "langfuse"

    def start_run_trace(
        self,
        *,
        conversation_id: str,
        run_id: str | None,
        messages: list[dict[str, Any]],
        confirm_dangerous: bool,
        model: str,
    ) -> TraceHandle:
        raw = start_chat_trace(
            conversation_id=conversation_id,
            messages=messages,
            confirm_dangerous=confirm_dangerous,
            model=model,
        )
        return TraceHandle(
            provider=self.name,
            raw=raw,
            trace_id=resolve_trace_id(raw, thread_id=conversation_id, run_id=run_id),
        )

    def end_run_trace(self, trace: TraceHandle | None, *, output_preview: str = "", error: str = "") -> None:
        end_chat_trace(trace.raw if trace else None, output_preview=output_preview, error=error)

    def start_generation(
        self,
        trace: TraceHandle | None,
        *,
        model: str,
        round_index: int,
        messages_count: int,
    ) -> SpanHandle | None:
        raw = start_generation_span(
            trace.raw if trace else None,
            model=model,
            round_index=round_index,
            messages_count=messages_count,
        )
        return SpanHandle(provider=self.name, raw=raw) if raw is not None else None

    def end_generation(
        self,
        span: SpanHandle | None,
        *,
        output_preview: str,
        finish_reason: str | None,
        tool_names: list[str],
    ) -> None:
        end_generation_span(
            span.raw if span else None,
            output_preview=output_preview,
            finish_reason=finish_reason,
            tool_names=tool_names,
        )

    def start_tool(self, trace: TraceHandle | None, *, name: str, args: dict[str, Any]) -> SpanHandle | None:
        raw = start_tool_span(trace.raw if trace else None, name=name, args=args)
        return SpanHandle(provider=self.name, raw=raw) if raw is not None else None

    def end_tool(self, span: SpanHandle | None, *, result: Any, error: str = "") -> None:
        end_tool_span(span.raw if span else None, result=result, error=error)

    def flush(self) -> None:
        flush_langfuse()


class LangSmithProvider(NoopProvider):
    name = "langsmith"

    def __init__(self) -> None:
        self._configured = _langsmith_configured()
        self._client: Any | None = None

    def start_run_trace(
        self,
        *,
        conversation_id: str,
        run_id: str | None,
        messages: list[dict[str, Any]],
        confirm_dangerous: bool,
        model: str,
    ) -> TraceHandle:
        fallback_trace_id = resolve_trace_id(None, thread_id=conversation_id, run_id=run_id)
        if not self._configured:
            log.info("LangSmith provider selected but not configured; using local trace_id only")
            return TraceHandle(provider=self.name, trace_id=fallback_trace_id)
        try:
            from langsmith.run_trees import RunTree  # type: ignore
        except Exception:
            log.exception("LangSmith SDK import failed")
            return TraceHandle(provider=self.name, trace_id=fallback_trace_id)

        last_user = ""
        for message in reversed(messages):
            if str(message.get("role", "")).lower() == "user":
                last_user = str(message.get("content", ""))
                break
        trace_uuid = uuid.uuid4()
        try:
            run_tree = RunTree(
                id=trace_uuid,
                name="learnagent.chat_turn",
                run_type="chain",
                inputs={"last_user_message": _truncate(last_user, 500)},
                tags=["learnagent", settings.scenario],
                extra={
                    "metadata": {
                        "thread_id": conversation_id,
                        "run_id": run_id,
                        "confirm_dangerous": confirm_dangerous,
                        "model": model,
                        "messages_count": len(messages),
                    }
                },
                project_name=os.getenv("LANGSMITH_PROJECT") or None,
            )
            _safe_call(run_tree.post)
            trace_id = str(getattr(run_tree, "trace_id", None) or trace_uuid)
            return TraceHandle(
                provider=self.name,
                raw=run_tree,
                trace_id=trace_id,
                external_trace_url=_langsmith_trace_url(trace_id),
            )
        except Exception:
            log.exception("LangSmith run trace creation failed")
            return TraceHandle(provider=self.name, trace_id=fallback_trace_id)

    def end_run_trace(self, trace: TraceHandle | None, *, output_preview: str = "", error: str = "") -> None:
        run_tree = trace.raw if trace else None
        if run_tree is None:
            return
        outputs = {"assistant_preview": _truncate(output_preview, 1500)} if output_preview else None
        _safe_call(run_tree.end, outputs=outputs, error=_truncate(error, 400) if error else None)
        _safe_call(run_tree.patch)

    def start_generation(
        self,
        trace: TraceHandle | None,
        *,
        model: str,
        round_index: int,
        messages_count: int,
    ) -> SpanHandle | None:
        run_tree = trace.raw if trace else None
        if run_tree is None:
            return None
        child = _safe_call(
            run_tree.create_child,
            name="openai_chat_completion",
            run_type="llm",
            inputs={"messages_count": messages_count},
            extra={"metadata": {"model": model, "round_index": round_index}},
        )
        if child is None:
            return None
        _safe_call(child.post)
        return SpanHandle(provider=self.name, raw=child)

    def end_generation(
        self,
        span: SpanHandle | None,
        *,
        output_preview: str,
        finish_reason: str | None,
        tool_names: list[str],
    ) -> None:
        child = span.raw if span else None
        if child is None:
            return
        _safe_call(
            child.end,
            outputs={"assistant_preview": _truncate(output_preview, 1500)},
            metadata={"finish_reason": finish_reason or "", "tool_names": tool_names},
        )
        _safe_call(child.patch)

    def start_tool(self, trace: TraceHandle | None, *, name: str, args: dict[str, Any]) -> SpanHandle | None:
        run_tree = trace.raw if trace else None
        if run_tree is None:
            return None
        child = _safe_call(
            run_tree.create_child,
            name=f"tool:{name}",
            run_type="tool",
            inputs=sanitize_observability_payload(args),
        )
        if child is None:
            return None
        _safe_call(child.post)
        return SpanHandle(provider=self.name, raw=child)

    def end_tool(self, span: SpanHandle | None, *, result: Any, error: str = "") -> None:
        child = span.raw if span else None
        if child is None:
            return
        _safe_call(
            child.end,
            outputs=sanitize_observability_payload(result),
            error=_truncate(error, 400) if error else None,
        )
        _safe_call(child.patch)

    def flush(self) -> None:
        client = self._client
        if client is not None:
            flusher = getattr(client, "flush", None)
            if callable(flusher):
                _safe_call(flusher)


_provider: ObservabilityProvider | None = None


def get_observability_provider() -> ObservabilityProvider:
    global _provider
    selected = _normalize_provider_name(settings.observability_provider)
    if _provider is not None and getattr(_provider, "name", None) == selected:
        return _provider
    if selected == "langfuse":
        _provider = LangfuseProvider()
    elif selected == "langsmith":
        _provider = LangSmithProvider()
    else:
        _provider = NoopProvider()
    return _provider


def reset_observability_provider() -> None:
    global _provider
    _provider = None


def provider_configured() -> bool:
    selected = _normalize_provider_name(settings.observability_provider)
    if selected == "langfuse":
        return settings.langfuse_configured
    if selected == "langsmith":
        return _langsmith_configured()
    return False


def start_run_trace(
    *,
    conversation_id: str,
    run_id: str | None,
    messages: list[dict[str, Any]],
    confirm_dangerous: bool,
    model: str,
) -> TraceHandle:
    return get_observability_provider().start_run_trace(
        conversation_id=conversation_id,
        run_id=run_id,
        messages=messages,
        confirm_dangerous=confirm_dangerous,
        model=model,
    )


def end_run_trace(trace: TraceHandle | None, *, output_preview: str = "", error: str = "") -> None:
    get_observability_provider().end_run_trace(trace, output_preview=output_preview, error=error)


def start_generation(
    trace: TraceHandle | None,
    *,
    model: str,
    round_index: int,
    messages_count: int,
) -> SpanHandle | None:
    return get_observability_provider().start_generation(
        trace,
        model=model,
        round_index=round_index,
        messages_count=messages_count,
    )


def end_generation(
    span: SpanHandle | None,
    *,
    output_preview: str,
    finish_reason: str | None,
    tool_names: list[str],
) -> None:
    get_observability_provider().end_generation(
        span,
        output_preview=output_preview,
        finish_reason=finish_reason,
        tool_names=tool_names,
    )


def start_tool(trace: TraceHandle | None, *, name: str, args: dict[str, Any]) -> SpanHandle | None:
    return get_observability_provider().start_tool(trace, name=name, args=args)


def end_tool(span: SpanHandle | None, *, result: Any, error: str = "") -> None:
    get_observability_provider().end_tool(span, result=result, error=error)


def flush_observability() -> None:
    get_observability_provider().flush()


def resolve_observability_trace_id(trace: Any | None, *, thread_id: str, run_id: str | None) -> str:
    if isinstance(trace, TraceHandle):
        return trace.trace_id or resolve_trace_id(trace.raw, thread_id=thread_id, run_id=run_id)
    return resolve_trace_id(trace, thread_id=thread_id, run_id=run_id)


def observability_trace_metadata(trace: Any | None) -> dict[str, Any]:
    if not isinstance(trace, TraceHandle):
        return {}
    out: dict[str, Any] = {"observability_provider": trace.provider}
    if trace.external_trace_url:
        out["external_trace_url"] = trace.external_trace_url
    return out


def _normalize_provider_name(value: str | None) -> str:
    selected = (value or "").strip().lower()
    if selected in {"langfuse", "langsmith"}:
        return selected
    return "none"


def _langsmith_configured() -> bool:
    tracing = (os.getenv("LANGSMITH_TRACING") or "").strip().lower()
    return bool(os.getenv("LANGSMITH_API_KEY") and tracing in {"true", "1", "yes"})


def _langsmith_trace_url(trace_id: str) -> str | None:
    project = os.getenv("LANGSMITH_PROJECT") or ""
    base = (os.getenv("LANGSMITH_ENDPOINT") or "https://smith.langchain.com").rstrip("/")
    if not trace_id:
        return None
    if project:
        return f"{base}/o/default/projects/p/{project}/r/{trace_id}"
    return f"{base}/public/{trace_id}/r"


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "...(truncated)"


def _safe_call(fn: Any, *args: Any, **kwargs: Any) -> Any | None:
    try:
        return fn(*args, **kwargs)
    except Exception:
        log.exception("Observability provider operation failed")
        return None
