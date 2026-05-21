from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass

_current: ContextVar[ToolCallContext | None] = ContextVar("tool_call_context", default=None)


@dataclass(frozen=True)
class ToolCallContext:
    call_id: str
    tool_name: str
    thread_id: str = ""
    run_id: str = ""


def set_tool_call_context(
    *,
    call_id: str,
    tool_name: str,
    thread_id: str = "",
    run_id: str = "",
) -> Token:
    return _current.set(
        ToolCallContext(
            call_id=str(call_id),
            tool_name=str(tool_name),
            thread_id=str(thread_id),
            run_id=str(run_id),
        )
    )


def reset_tool_call_context(token: Token) -> None:
    _current.reset(token)


def get_tool_call_context() -> ToolCallContext | None:
    return _current.get()


def clear_tool_call_context() -> None:
    _current.set(None)


def get_current_call_id() -> str | None:
    ctx = _current.get()
    return ctx.call_id if ctx else None
