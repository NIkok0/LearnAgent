from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from copilot_agent.settings import settings
from copilot_agent.tools.sanitize import redact_cookie_header

log = logging.getLogger(__name__)

_client: Any | None = None

_REDACT_KEYS = {
    "cookie_header",
    "set_cookie",
    "set_cookie_redacted",
    "_raw_set_cookie_for_store_only",
    "authorization",
    "password",
}


def _truncate(value: str, limit: int = 1500) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "...(truncated)"


def sanitize_observability_payload(obj: Any) -> Any:
    if isinstance(obj, Mapping):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if lk in _REDACT_KEYS:
                if lk == "cookie_header":
                    out[str(k)] = redact_cookie_header(str(v) if v else "")
                else:
                    out[str(k)] = "***REDACTED***"
                continue
            out[str(k)] = sanitize_observability_payload(v)
        return out
    if isinstance(obj, list):
        return [sanitize_observability_payload(x) for x in obj]
    if isinstance(obj, tuple):
        return tuple(sanitize_observability_payload(x) for x in obj)
    if isinstance(obj, str):
        return _truncate(obj)
    return obj


def _is_langfuse_configured() -> bool:
    return bool(
        settings.langfuse_enabled
        and settings.langfuse_public_key.strip()
        and settings.langfuse_secret_key.strip()
    )


def _safe_call(fn: Any, *args: Any, **kwargs: Any) -> Any | None:
    try:
        return fn(*args, **kwargs)
    except Exception:
        log.exception("Langfuse operation failed")
        return None


def _get_client() -> Any | None:
    if not _is_langfuse_configured():
        return None
    global _client
    if _client is not None:
        return _client
    try:
        from langfuse import Langfuse  # type: ignore
    except Exception:
        log.exception("Langfuse SDK import failed")
        return None
    _client = _safe_call(
        Langfuse,
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_host,
    )
    return _client


def resolve_trace_id(trace: Any | None, *, thread_id: str, run_id: str | None) -> str:
    if trace is not None:
        for attr in ("id", "trace_id"):
            value = getattr(trace, attr, None)
            if value:
                return str(value)
    suffix = run_id or "unknown"
    return f"local-{thread_id}-{suffix}"


def start_chat_trace(
    *,
    conversation_id: str,
    messages: list[dict[str, Any]],
    confirm_dangerous: bool,
    model: str,
) -> Any | None:
    client = _get_client()
    if client is None:
        return None
    last_user = ""
    for m in reversed(messages):
        if str(m.get("role", "")).lower() == "user":
            last_user = str(m.get("content", ""))
            break
    return _safe_call(
        client.trace,
        name="wm_chat_turn",
        session_id=conversation_id,
        metadata={
            "confirm_dangerous": confirm_dangerous,
            "model": model,
            "messages_count": len(messages),
        },
        input={"last_user_message": _truncate(last_user, limit=500)},
    )


def end_chat_trace(trace: Any | None, *, output_preview: str = "", error: str = "") -> None:
    if trace is None:
        return
    payload: dict[str, Any] = {}
    if output_preview:
        payload["output"] = {"assistant_preview": _truncate(output_preview, limit=1500)}
    if error:
        payload["metadata"] = {"error": _truncate(error, limit=400)}
    if not payload:
        return
    updater = getattr(trace, "update", None)
    if updater is not None:
        _safe_call(updater, **payload)


def start_generation_span(
    trace: Any | None,
    *,
    model: str,
    round_index: int,
    messages_count: int,
) -> Any | None:
    if trace is None:
        return None
    creator = getattr(trace, "generation", None)
    if creator is None:
        return None
    return _safe_call(
        creator,
        name="openai_chat_completion",
        model=model,
        metadata={"round_index": round_index, "messages_count": messages_count},
    )


def end_generation_span(
    generation: Any | None,
    *,
    output_preview: str,
    finish_reason: str | None,
    tool_names: list[str],
) -> None:
    if generation is None:
        return
    payload = {
        "output": {"assistant_preview": _truncate(output_preview, limit=1500)},
        "metadata": {
            "finish_reason": finish_reason or "",
            "tool_names": tool_names,
        },
    }
    ender = getattr(generation, "end", None)
    if ender is not None:
        _safe_call(ender, **payload)
        return
    updater = getattr(generation, "update", None)
    if updater is not None:
        _safe_call(updater, **payload)


def start_tool_span(trace: Any | None, *, name: str, args: dict[str, Any]) -> Any | None:
    if trace is None:
        return None
    creator = getattr(trace, "span", None)
    if creator is None:
        return None
    return _safe_call(
        creator,
        name=f"tool:{name}",
        input=sanitize_observability_payload(args),
    )


def end_tool_span(
    span: Any | None,
    *,
    result: Any,
    error: str = "",
) -> None:
    if span is None:
        return
    payload: dict[str, Any] = {"output": sanitize_observability_payload(result)}
    if error:
        payload["metadata"] = {"error": _truncate(error, limit=400)}
    ender = getattr(span, "end", None)
    if ender is not None:
        _safe_call(ender, **payload)
        return
    updater = getattr(span, "update", None)
    if updater is not None:
        _safe_call(updater, **payload)


def flush_langfuse() -> None:
    client = _get_client()
    if client is None:
        return
    flusher = getattr(client, "flush", None)
    if flusher is not None:
        _safe_call(flusher)
