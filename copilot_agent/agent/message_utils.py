from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage


def extract_text_from_chunk(chunk: Any) -> str:
    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text = ""
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text += str(part.get("text", ""))
        return text
    return ""


def extract_text_from_chat_output(output: Any) -> str:
    if isinstance(output, BaseMessage):
        return extract_text_from_chunk(output)
    if isinstance(output, dict):
        for key in ("message", "generations"):
            text = extract_text_from_chat_output(output.get(key))
            if text:
                return text
        return extract_text_from_chunk(output)
    if isinstance(output, list):
        return "".join(extract_text_from_chat_output(item) for item in output)
    message = getattr(output, "message", None)
    if message is not None:
        text = extract_text_from_chat_output(message)
        if text:
            return text
    return extract_text_from_chunk(output)


def extract_reasoning_content_from_chat_output(output: Any) -> str:
    if isinstance(output, BaseMessage):
        return extract_reasoning_content(output)
    if isinstance(output, dict):
        for key in ("message", "generations"):
            text = extract_reasoning_content_from_chat_output(output.get(key))
            if text:
                return text
        return ""
    if isinstance(output, list):
        return "".join(extract_reasoning_content_from_chat_output(item) for item in output)
    message = getattr(output, "message", None)
    if message is not None:
        return extract_reasoning_content_from_chat_output(message)
    return ""


def extract_reasoning_content(message: Any) -> str:
    kwargs = getattr(message, "additional_kwargs", None)
    if isinstance(kwargs, dict):
        value = kwargs.get("reasoning_content")
        if value:
            return str(value)
    response_metadata = getattr(message, "response_metadata", None)
    if isinstance(response_metadata, dict):
        value = response_metadata.get("reasoning_content")
        if value:
            return str(value)
    return ""


def extract_blocked_message_text(event: dict[str, Any]) -> str:
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


def extract_call_id(event: dict[str, Any]) -> str:
    data = event.get("data") or {}
    if isinstance(data, dict):
        nested = data.get("input")
        if isinstance(nested, dict):
            for key in ("id", "tool_call_id"):
                value = nested.get(key)
                if value:
                    return str(value)
        output = data.get("output")
        if isinstance(output, dict):
            for key in ("id", "tool_call_id"):
                value = output.get(key)
                if value:
                    return str(value)
        for key in ("id", "tool_call_id"):
            value = data.get(key)
            if value:
                return str(value)
    return ""


def extract_interrupt_payload(event: dict[str, Any]) -> dict[str, Any] | None:
    if str(event.get("event", "")) != "on_chain_stream":
        return None
    chunk = (event.get("data") or {}).get("chunk")
    if not isinstance(chunk, dict) or "__interrupt__" not in chunk:
        return None
    interrupts = chunk.get("__interrupt__")
    if not interrupts:
        return {"required": True, "reason": "dangerous_tool"}
    first = interrupts[0] if isinstance(interrupts, (list, tuple)) else interrupts
    value = getattr(first, "value", None)
    if isinstance(value, dict):
        return value
    return {"required": True, "reason": "dangerous_tool", "message": str(value)}


def last_user_content(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if str(message.get("role", "")).lower() == "user":
            return str(message.get("content", ""))
    return ""


def current_turn_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only the current-turn user message.

    Client ``messages[]`` may still carry full history for backward compatibility;
    LangGraph checkpoint state is the source of truth for prior turns.
    """
    if not messages:
        return []
    for message in reversed(messages):
        if str(message.get("role", "")).lower() == "user":
            return [message]
    return [messages[-1]]


def approval_tool_call_ids(events: list[dict[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for event in events:
        if event.get("type") != "approval_required":
            continue
        payload = event.get("payload") or {}
        for call in payload.get("tool_calls") or []:
            if isinstance(call, dict) and call.get("name") and call.get("id"):
                out[str(call["name"])] = str(call["id"])
    return out
