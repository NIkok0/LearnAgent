from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, TypedDict

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
from copilot_agent.memory import MemoryManager
from copilot_agent.runtime.checkpoint_reader import CheckpointReader
from copilot_agent.runtime.event_schema import EVENT_RUN_COMPLETED_META
from copilot_agent.runtime.execution_engine import GraphInterrupted
from copilot_agent.tools.audit import build_tool_end_payload, build_tool_start_payload
from copilot_agent.tools.registry import ToolRegistry


class DomainEvent(TypedDict):
    type: str
    payload: dict[str, Any]


@dataclass
class _ToolTracker:
    started_at: dict[str, float] = field(default_factory=dict)
    start_names: dict[str, str] = field(default_factory=dict)
    end_emitted: set[str] = field(default_factory=set)


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
    ) -> AsyncIterator[DomainEvent]:
        tracker = _ToolTracker()
        pending_tool_call_ids = approval_tool_call_ids(
            self._memory.get_thread_events(thread_id, run_id=run_id)
        )
        last_assistant_output = ""
        last_reasoning_content = ""

        async for event in graph.astream_events(graph_input, config=graph_config, version="v2"):
            kind = str(event.get("event", ""))
            interrupt_payload = extract_interrupt_payload(event)
            if interrupt_payload is not None:
                for call in interrupt_payload.get("tool_calls") or []:
                    if isinstance(call, dict) and call.get("name") and call.get("id"):
                        pending_tool_call_ids[str(call["name"])] = str(call["id"])
                yield {"type": "approval_required", "payload": interrupt_payload}
                raise GraphInterrupted(interrupt_payload)

            if kind == "on_chat_model_stream":
                chunk = (event.get("data") or {}).get("chunk")
                reasoning_delta = extract_reasoning_content(chunk)
                if reasoning_delta:
                    last_reasoning_content += reasoning_delta
                    yield {
                        "type": "assistant_state",
                        "payload": {"reasoning_content_delta": reasoning_delta},
                    }
                text = extract_text_from_chunk(chunk)
                if text:
                    last_assistant_output += text
                    yield {"type": "token", "payload": {"text": text}}
                continue

            if kind == "on_chat_model_end":
                output = (event.get("data") or {}).get("output")
                reasoning = extract_reasoning_content_from_chat_output(output)
                if reasoning and reasoning != last_reasoning_content:
                    last_reasoning_content = reasoning
                    yield {
                        "type": "assistant_state",
                        "payload": {"reasoning_content": reasoning},
                    }
                text = extract_text_from_chat_output(output)
                if text and text not in last_assistant_output:
                    last_assistant_output += text
                    yield {"type": "token", "payload": {"text": text}}
                continue

            blocked_text = extract_blocked_message_text(event)
            if blocked_text:
                if "gated" in blocked_text.lower() and "confirm_dangerous=true" in blocked_text:
                    payload = {"required": True, "reason": "dangerous_tool", "message": blocked_text}
                    yield {"type": "approval_required", "payload": payload}
                    raise GraphInterrupted(payload)
                last_assistant_output += blocked_text
                yield {"type": "token", "payload": {"text": blocked_text}}
                continue

            if kind == "on_tool_start":
                name = str(event.get("name", ""))
                call_id = pending_tool_call_ids.get(name) or extract_call_id(event)
                if call_id:
                    tracker.started_at[call_id] = time.perf_counter()
                    tracker.start_names[call_id] = name
                args = (event.get("data") or {}).get("input", {})
                spec = self._tool_registry.get_spec(name)
                yield {
                    "type": "tool_start",
                    "payload": build_tool_start_payload(
                        name=name,
                        call_id=call_id,
                        **_tool_audit_metadata(spec, args if isinstance(args, dict) else {}),
                        arguments=args,
                    ),
                }
                continue

            if kind == "on_tool_end":
                name = str(event.get("name", ""))
                call_id = self._resolve_tool_call_id(event, name, tracker, pending_tool_call_ids)
                started_at = tracker.started_at.pop(call_id, None) if call_id else None
                if call_id:
                    tracker.start_names.pop(call_id, None)
                    tracker.end_emitted.add(call_id)
                result = (event.get("data") or {}).get("output", {})
                duration_ms = int((time.perf_counter() - started_at) * 1000) if started_at else None
                yield {
                    "type": "tool_end",
                    "payload": build_tool_end_payload(
                        name=name,
                        call_id=call_id,
                        result=result,
                        duration_ms=duration_ms,
                    ),
                }
                continue

            if kind == "on_tool_error":
                name = str(event.get("name", ""))
                call_id = self._resolve_tool_call_id(event, name, tracker, pending_tool_call_ids)
                if call_id and not name:
                    name = tracker.start_names.pop(call_id, "")
                started_at = tracker.started_at.pop(call_id, None) if call_id else None
                error_value = (event.get("data") or {}).get("error")
                duration_ms = int((time.perf_counter() - started_at) * 1000) if started_at else None
                if call_id:
                    tracker.end_emitted.add(call_id)
                yield {
                    "type": "tool_end",
                    "payload": build_tool_end_payload(
                        name=name,
                        call_id=call_id,
                        result={},
                        duration_ms=duration_ms,
                        success=False,
                        error=str(error_value or "tool execution failed"),
                    ),
                }
                continue

        for domain_event in self._missing_tool_end_events(thread_id, run_id, tracker):
            yield domain_event

        if self._checkpoint_reader is not None and run_id:
            snapshot = await self._checkpoint_reader.snapshot(thread_id)
            yield {
                "type": EVENT_RUN_COMPLETED_META,
                "payload": {
                    "checkpoint_thread_id": snapshot["checkpoint_thread_id"],
                    "message_count": snapshot["message_count"],
                    "has_interrupt": snapshot["has_interrupt"],
                },
            }

        assistant_message = {"content": last_assistant_output}
        if last_reasoning_content:
            assistant_message["reasoning_content"] = last_reasoning_content
        yield {"type": "done", "payload": {"assistant_message": assistant_message}}

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
    ) -> list[DomainEvent]:
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
        out: list[DomainEvent] = []
        for event in tool_starts:
            payload = event.get("payload") or {}
            call_id = str(payload.get("call_id") or "")
            if not call_id or call_id in existing_end_ids or call_id in tracker.end_emitted:
                continue
            name = str(payload.get("name") or tracker.start_names.get(call_id, ""))
            started_at = tracker.started_at.pop(call_id, None)
            duration_ms = int((time.perf_counter() - started_at) * 1000) if started_at else None
            end_payload = build_tool_end_payload(
                name=name,
                call_id=call_id,
                result={},
                duration_ms=duration_ms,
                success=False,
                error="tool execution did not produce a result event before graph completed",
            )
            tracker.start_names.pop(call_id, None)
            tracker.end_emitted.add(call_id)
            out.append({"type": "tool_end", "payload": end_payload})
        return out


def _tool_audit_metadata(spec, args: dict[str, Any]) -> dict[str, Any]:
    if spec is None:
        return {
            "category": "",
            "risk_level": "",
            "requires_approval": False,
        }
    return {
        "category": spec.category,
        "risk_level": spec.risk_level,
        "requires_approval": spec.requires_approval_for(args),
    }
